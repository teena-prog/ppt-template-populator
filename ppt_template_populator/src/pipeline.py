from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from .ppt_populator import populate_presentation
from .prompt_builder import build_messages, build_missing_target_recovery_messages, build_repair_messages, build_semantic_messages, build_semantic_repair_messages
from .response_models import PresentationContent, RecoveredTargetContent, SemanticSlideResponse, SlideChunkResponse, TargetContent
from .response_validator import (
    ResponseValidationError,
    analyze_chunk_targets,
    analyze_ordered_target_recovery,
    extract_json,
    normalize_model_response,
    merge_recovered_targets,
    merge_slide_contents,
    parse_chunk_response,
    safe_response_structure,
    preserve_valid_targets_during_repair,
    validate_chunk_response,
    validate_response,
)
from .security import confined_path
from .template_binary import temporary_pptx, verify_checksum
from .template_selector import extract_selected_template_id, select_template
from .template_retriever import build_retrieval_query
from .target_metadata import normalize_template_targets, required_targets_by_slide
from .slide_planner import build_factual_summary, build_slide_plan
from .semantic_content import map_semantic_to_targets, semantic_fallback, validate_semantic_response
from .visual_validator import validate_presentation_layout
from .template_profile import canonical_template_profile

logger = logging.getLogger(__name__)


def _is_bullet_validation_failure(errors: list[str]) -> bool:
    text = " ".join(errors).casefold()
    return any(marker in text for marker in (
        "single-character", "isolated letter", "character-by-character",
        "bullet content", "bullets must", "complete, valid bullet",
    ))


def _friendly_bullet_failure(errors: list[str], fallback_slide: int | None = None) -> ResponseValidationError:
    slide = fallback_slide
    if slide is None:
        match = next((match for error in errors
                      if (match := re.search(r"(?:slide\s+|slides\.)(\d+)", error, re.IGNORECASE))), None)
        if match:
            slide = int(match.group(1))
    label = str(slide) if slide is not None else "the affected slide"
    return ResponseValidationError([
        f"The model could not generate complete, valid bullet content for slide {label}.",
        *errors,
    ])


def _log_structure(boundary: str, payload: str | dict[str, Any]) -> None:
    logger.info("PPT generation boundary=%s structure=%s", boundary, safe_response_structure(payload))


@dataclass
class PipelineResult:
    content: PresentationContent
    output_path: Path
    warnings: list[str]
    timings: dict[str, float]
    stages: list[str] = field(default_factory=list)
    selection: Any | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    generation_model_id: str | None = None
    embedding_model_id: str | None = None


class GenerationPipeline:
    STAGES = ["Input received", "Template retrieved", "Template structure loaded", "Prompt constructed", "watsonx.ai request sent", "Response received", "JSON parsed", "Response validated", "PPT populated", "Output saved"]

    def __init__(self, retriever: Any, watsonx: Any, project_root: Path, generated_dir: Path, stage_callback: Callable[[str], None] | None = None, max_required_targets_per_chunk: int = 12, max_recovery_targets_per_batch: int = 6, max_recovery_attempts: int = 2):
        self.retriever, self.watsonx, self.project_root, self.generated_dir = retriever, watsonx, project_root, generated_dir
        self.stage_callback = stage_callback or (lambda stage: None)
        self.max_required_targets_per_chunk = max_required_targets_per_chunk
        self.max_recovery_targets_per_batch = max(1, max_recovery_targets_per_batch)
        self.max_recovery_attempts = max(1, max_recovery_attempts)

    def _stage(self, stages: list[str], name: str) -> None:
        stages.append(name); self.stage_callback(name)

    def _full_template(self, template_id: str, *, selection_mode: str) -> dict[str, Any]:
        try:
            raw = self.retriever.get_full(template_id)
            return canonical_template_profile(raw).model_dump()
        except Exception as exc:
            logger.error(
                "Template retrieval failed stage=full_template_retrieval selection_mode=%s exception_type=%s",
                selection_mode, type(exc).__name__,
            )
            raise

    @staticmethod
    def _template_subset(template: dict[str, Any], numbers: set[int]) -> dict[str, Any]:
        # Classify required/replaceable targets against the FULL deck first (see
        # target_metadata.normalize_template_targets), then slice to this chunk's
        # slides. Slicing before classifying would let "repeated chrome" targets
        # (e.g. a logo repeated across the deck) look required within a small
        # subset, which the final full-template validation would then reject.
        full = normalize_template_targets(template)
        subset = dict(full); subset["slides"] = [slide for slide in full["slides"] if slide["slide_number"] in numbers]
        subset["slide_count"] = len(subset["slides"]); return subset

    @staticmethod
    def _fallback_target_content(metadata: dict[str, Any]) -> str:
        """Deterministic, safe filler used only when the model still omits a
        required target after every recovery attempt, so the pipeline can
        finish instead of failing the whole presentation."""
        limit = max(1, int(metadata.get("maximum_characters") or metadata.get("approximate_max_characters") or 0) or 40)
        def bounded(value: str) -> str:
            value = " ".join(value.split())
            if len(value) <= limit:
                return value
            clipped = value[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
            return clipped or value[:limit]

        source = str(metadata.get("source_excerpt") or "").strip()
        if source:
            first_statement = source.replace("\n", " ").split(".", 1)[0].strip()
            if first_statement:
                return bounded(first_statement)
        existing = str(metadata.get("existing_text") or "").strip()
        if existing:
            return bounded(existing)
        role = str(metadata.get("role", "")).lower()
        section = str(metadata.get("section_type") or "this section").replace("_", " ").strip()
        generic = {
            "title": section.title(), "subtitle": f"Key details for {section}",
            "heading": section.title(),
            "body": f"Review the source material for {section} before presenting.",
            "caption": section.title(),
        }.get(role, f"Review source content for {section}.")
        return bounded(generic)

    @staticmethod
    def _response_for_chunk(raw: str, numbers: set[int]) -> tuple[dict[str, Any], list[int], int]:
        """Keep only the well-formed slides requested for this generation chunk.

        A "slide" entry that isn't a JSON object (e.g. a stray string) is
        dropped rather than passed through: some models lose track of the
        schema partway through a multi-slide response and emit a shorthand
        value for a later slide instead of an object, which would otherwise
        crash pydantic validation outright with an opaque "Input should be a
        valid dictionary" error. Dropping it here means that slide's required
        targets are simply missing from this response, which the existing
        analyze_chunk_targets / focused-recovery path already fills in --
        the same graceful handling as a slide the model omitted entirely.
        """
        data = normalize_model_response(raw, expected_root="slides")
        slides = data.get("slides")
        if not isinstance(slides, list):
            return data, [], 0

        kept: list[Any] = []
        ignored: list[int] = []
        malformed = 0
        for slide in slides:
            if not isinstance(slide, dict):
                malformed += 1
                continue
            number = slide.get("slide_number")
            if isinstance(number, int) and number not in numbers:
                ignored.append(number)
                continue
            kept.append(slide)

        filtered = dict(data)
        filtered["slides"] = kept
        return filtered, sorted(set(ignored)), malformed

    def _plan_chunks(self, template: dict[str, Any]) -> list[list[int]]:
        required = required_targets_by_slide(template); chunks = []; current = []; target_count = 0
        for slide in template["slides"]:
            number = slide["slide_number"]; count = len(required.get(number, {}))
            if current and (len(current) >= 4 or target_count + count > self.max_required_targets_per_chunk):
                chunks.append(current); current = []; target_count = 0
            current.append(number); target_count += count
            if count > self.max_required_targets_per_chunk:
                chunks.append(current); current = []; target_count = 0
        if current: chunks.append(current)
        planned = [number for chunk in chunks for number in chunk]
        if len(planned) != len(set(planned)):
            raise ValueError("Template slide numbers are duplicated; non-overlapping chunks cannot be constructed.")
        return chunks

    def _recover_targets(
        self,
        *,
        model_id: str,
        request: list[dict[str, Any]],
        message_args: dict[str, Any],
    ) -> tuple[list[Any], list[str], int]:
        ordered = sorted(
            request,
            key=lambda item: (
                item["slide_number"],
                item["target_kind"],
                item["target_id"],
            ),
        )
        batches = [
            ordered[index:index + self.max_recovery_targets_per_batch]
            for index in range(0, len(ordered), self.max_recovery_targets_per_batch)
        ]
        recovered_by_key: dict[tuple[int, str, int], Any] = {}
        unresolved_after_retries: list[dict[str, Any]] = []
        warnings: list[str] = []
        calls = 0

        for batch_number, batch in enumerate(batches, start=1):
            pending = batch
            for attempt in range(1, self.max_recovery_attempts + 1):
                response = self.watsonx.chat(
                    model_id,
                    build_missing_target_recovery_messages(pending, message_args),
                    max_tokens=2048,
                    temperature=0,
                )
                calls += 1
                _log_structure("raw_focused_repair", response.text)
                try:
                    normalized_recovery = normalize_model_response(response.text, expected_root="contents")
                except ResponseValidationError:
                    normalized_recovery = {}
                _log_structure("normalized_focused_repair", normalized_recovery)
                analysis = analyze_ordered_target_recovery(response.text, pending)

                for item in analysis.recovered:
                    key = (item.slide_number, item.target_kind, item.target_id)
                    recovered_by_key[key] = item

                if analysis.errors:
                    warnings.append(
                        f"Recovery batch {batch_number}, attempt {attempt} "
                        f"discarded {len(analysis.errors)} invalid response item(s)."
                    )

                pending = analysis.unresolved
                if not pending:
                    break

            if pending:
                unresolved_after_retries.extend(pending)

        if unresolved_after_retries:
            unresolved_keys = sorted(
                (
                    item["slide_number"],
                    item["target_kind"],
                    item["target_id"],
                )
                for item in unresolved_after_retries
            )
            for item in unresolved_after_retries:
                key = (item["slide_number"], item["target_kind"], item["target_id"])
                fallback = self._fallback_target_content(item)
                if item.get("role") == "body":
                    recovered_by_key[key] = RecoveredTargetContent(slide_number=item["slide_number"], target_kind=item["target_kind"], target_id=item["target_id"], content_type="bullets", bullets=[fallback])
                else:
                    recovered_by_key[key] = RecoveredTargetContent(slide_number=item["slide_number"], target_kind=item["target_kind"], target_id=item["target_id"], content_type="title", text=fallback)
            warnings.append(
                f"{len(unresolved_after_retries)} required target(s) could not be generated by the "
                f"model after retries; placeholder content was inserted for review: {unresolved_keys}."
            )

        if len(batches) > 1:
            warnings.append(
                f"Focused recovery was split into {len(batches)} bounded batches."
            )
        return list(recovered_by_key.values()), warnings, calls

    def _generate_validated(self, *, model_id: str, template: dict[str, Any], message_args: dict[str, Any]) -> tuple[PresentationContent, list[str], int]:
        if template.get("slide_plan"):
            return self._generate_semantic_validated(model_id=model_id, template=template, message_args=message_args)
        chunks = self._plan_chunks(template)
        merged_slides = []; recovery_targets = []; warnings: list[str] = []; calls = 0; title = message_args.get("topic", "Presentation")
        for chunk in chunks:
            chunk_set = set(chunk); subset = self._template_subset(template, chunk_set)
            messages = build_messages(template=template, slide_numbers=chunk_set, **message_args)
            response = self.watsonx.chat(model_id, messages, max_tokens=4096, temperature=.1, response_schema=SlideChunkResponse.model_json_schema()); calls += 1
            _log_structure("raw_generation", response.text)
            initial_errors: list[str] = []
            try:
                # A truncated/malformed raw response (e.g. the model was cut off
                # mid-string at the max_tokens limit) must be treated exactly like
                # any other invalid chunk response -- routed into the repair path
                # below -- instead of raising uncaught out of the pipeline. Only
                # this parse failure resets response_payload to empty; a response
                # that parses fine but fails semantic validation keeps its parsed
                # payload so analyze_chunk_targets can still salvage valid targets.
                response_payload, ignored_slides, malformed_slides = self._response_for_chunk(response.text, chunk_set)
                _log_structure("normalized_generation", response_payload)
            except ResponseValidationError as initial_error:
                initial_errors = list(initial_error.errors)
                response_payload = {"slides": []}
                logger.warning("Chunk response was not valid JSON: %s; safe structure=%s", initial_error.errors, safe_response_structure(response.text))
            else:
                if ignored_slides:
                    warnings.append(f"Ignored out-of-chunk slides returned by the model: {ignored_slides}.")
                if malformed_slides:
                    warnings.append(f"Discarded {malformed_slides} malformed slide entr{'y' if malformed_slides == 1 else 'ies'} returned by the model; affected required targets will be recovered.")
                try:
                    content, chunk_warnings = validate_chunk_response(response_payload, subset)
                    merged_slides = merge_slide_contents(merged_slides, content.slides); warnings.extend(chunk_warnings)
                    _log_structure("chunk_merge", {"slides": [slide.model_dump() for slide in merged_slides]})
                    continue
                except ResponseValidationError as initial_error:
                    initial_errors = list(initial_error.errors)
                    logger.warning("Chunk response validation failed: %s; safe structure=%s", initial_error.errors, safe_response_structure(response.text))
            try:
                parsed = parse_chunk_response(response_payload, subset)
                analysis = analyze_chunk_targets(parsed, subset)
            except ResponseValidationError:
                analysis = None
            if analysis is None or analysis.structural_errors:
                repair = self.watsonx.chat(model_id, build_repair_messages(messages, response.text, analysis.structural_errors if analysis else initial_errors, chunk=True), max_tokens=4096, temperature=0, response_schema=SlideChunkResponse.model_json_schema()); calls += 1
                _log_structure("raw_repair", repair.text)
                try:
                    repair_payload, ignored_repair_slides, malformed_repair_slides = self._response_for_chunk(repair.text, chunk_set)
                    _log_structure("normalized_repair", repair_payload)
                except ResponseValidationError as exc:
                    if _is_bullet_validation_failure([*initial_errors, *exc.errors]):
                        raise _friendly_bullet_failure(exc.errors, chunk[0] if len(chunk) == 1 else None) from exc
                    raise ResponseValidationError(["Slide content failed schema validation after one controlled repair request.", *exc.errors]) from exc
                if ignored_repair_slides:
                    warnings.append(f"Ignored out-of-chunk slides returned during repair: {ignored_repair_slides}.")
                if malformed_repair_slides:
                    warnings.append(f"Discarded {malformed_repair_slides} malformed slide entr{'y' if malformed_repair_slides == 1 else 'ies'} returned during repair; affected required targets will be recovered.")
                try: parsed = parse_chunk_response(repair_payload, subset)
                except ResponseValidationError as exc:
                    if _is_bullet_validation_failure([*initial_errors, *exc.errors]):
                        raise _friendly_bullet_failure(exc.errors, chunk[0] if len(chunk) == 1 else None) from exc
                    raise ResponseValidationError(["Slide content failed schema validation after one controlled repair request.", *exc.errors]) from exc
                parsed = preserve_valid_targets_during_repair(response_payload, parsed, subset)
                analysis = analyze_chunk_targets(parsed, subset)
                if analysis.structural_errors: raise ResponseValidationError(["Slide content remained structurally unsafe after one controlled repair request.", *analysis.structural_errors])
                warnings.append(f"Slides {chunk} required one controlled structural repair request.")
            merged_slides = merge_slide_contents(merged_slides, analysis.slides); recovery_targets.extend(analysis.recovery_targets); warnings.extend(analysis.warnings)
            _log_structure("chunk_merge", {"slides": [slide.model_dump() for slide in merged_slides]})
        if recovery_targets:
            unique = {(item["slide_number"], item["target_kind"], item["target_id"]): item for item in recovery_targets}
            request = [{"slide_number": item["slide_number"], "target_kind": item["target_kind"], "target_id": item["target_id"], "role": item["role"], "section_type": item.get("section_type"), "maximum_characters": item["approximate_max_characters"], "existing_text": item.get("existing_text", ""), "source_excerpt": item.get("source_excerpt", "")} for item in unique.values()]
            recovered, recovery_warnings, recovery_calls = self._recover_targets(
                model_id=model_id,
                request=request,
                message_args=message_args,
            )
            calls += recovery_calls
            merged_slides = merge_recovered_targets(merged_slides, recovered, set(unique))
            warnings.extend(recovery_warnings); warnings.append(f"Focused recovery supplied {len(recovered)} missing or invalid required target(s).")
        merged = PresentationContent(presentation_title=title, slides=sorted(merged_slides, key=lambda slide: slide.slide_number))
        _log_structure("final_validation", {"slides": [slide.model_dump() for slide in merged.slides]})
        final, final_warnings = validate_response({"slides": [slide.model_dump() for slide in merged.slides]}, template, presentation_title=title); warnings.extend(final_warnings)
        return final, warnings, calls

    def _generate_semantic_validated(self, *, model_id: str, template: dict[str, Any], message_args: dict[str, Any]) -> tuple[PresentationContent, list[str], int]:
        """Generate semantic content one slide at a time, then map IDs in Python."""
        plan_numbers = [int(slide["slide_number"]) for slide in template["slides"]]
        if len(plan_numbers) != len(set(plan_numbers)) or sorted(plan_numbers) != list(range(1, len(plan_numbers) + 1)):
            raise ResponseValidationError(["The slide plan contains duplicate, missing, or out-of-range slide numbers."])
        semantic_by_number = {}
        warnings: list[str] = []
        calls = 0
        schema = SemanticSlideResponse.model_json_schema()
        for slide in template["slides"]:
            expected = {int(slide["slide_number"]): str(slide["section_type"])}
            messages = build_semantic_messages(slide=slide, **message_args)
            response = self.watsonx.chat(model_id, messages, max_tokens=1800, temperature=.1, response_schema=schema)
            calls += 1
            _log_structure("raw_semantic_generation", response.text)
            try:
                parsed = validate_semantic_response(response.text, expected)
            except ResponseValidationError as initial_error:
                repair = self.watsonx.chat(
                    model_id,
                    build_semantic_repair_messages(messages, response.text, initial_error.errors),
                    max_tokens=1800,
                    temperature=0,
                    response_schema=schema,
                )
                calls += 1
                _log_structure("raw_semantic_repair", repair.text)
                try:
                    parsed = validate_semantic_response(repair.text, expected)
                    warnings.append(f"Slide {slide['slide_number']} required one focused semantic repair.")
                except ResponseValidationError as repair_error:
                    if _is_bullet_validation_failure([*initial_error.errors, *repair_error.errors]):
                        raise _friendly_bullet_failure(repair_error.errors, int(slide["slide_number"])) from repair_error
                    parsed = SemanticSlideResponse(slides=[semantic_fallback(slide, message_args.get("topic", "Presentation"))])
                    warnings.append(f"Slide {slide['slide_number']} used deterministic source-grounded fallback content after semantic repair failed.")
            item = parsed.slides[0]
            if item.slide_number in semantic_by_number:
                raise ResponseValidationError([f"Duplicate generated semantic slide number {item.slide_number}."])
            semantic_by_number[item.slide_number] = item
        if set(semantic_by_number) != set(plan_numbers):
            raise ResponseValidationError(["Generated semantic slides do not exactly match the complete slide plan."])
        semantic = SemanticSlideResponse(slides=[semantic_by_number[number] for number in plan_numbers])
        content, mapping_warnings = map_semantic_to_targets(semantic, template, message_args.get("topic", "Presentation"))
        warnings.extend(mapping_warnings)
        return content, warnings, calls

    def run_automatic(self, *, model_id: str, embedding_client: Any, topic: str, complete_demand: str, source_content: str, audience: str, tone: str, desired_slide_count: int, required_sections: list[str], visual_preferences: str, additional_instructions: str = "", confidence_threshold: float = .60, embedding_configuration: dict[str, Any] | None = None) -> PipelineResult:
        timings: dict[str, float] = {}; stages: list[str] = []; warnings: list[str] = []; started = time.perf_counter()
        if not topic.strip() or not complete_demand.strip(): raise ValueError("Presentation topic and complete demand are required.")
        requirements = {"topic": topic, "complete_demand": complete_demand,
                        "source_content": source_content[:6000], "audience": audience,
                        "tone": tone, "desired_slide_count": desired_slide_count,
                        "required_sections": required_sections,
                        "visual_preferences": visual_preferences,
                        "additional_instructions": additional_instructions}
        self._stage(stages, "User demand received")
        # Include a bounded source excerpt so automatic selection reflects the
        # uploaded document instead of repeatedly ranking on generic UI fields.
        retrieval_query = build_retrieval_query(
            topic=topic, complete_demand=complete_demand,
            source_content=source_content, audience=audience, tone=tone,
            required_sections=required_sections,
            visual_preferences=visual_preferences,
            additional_instructions=additional_instructions,
        )
        self._stage(stages, "Retrieval query prepared")
        configurations = self.retriever.embedding_configurations()
        if not configurations: raise ValueError("No embedded templates are available. An administrator must ingest templates first.")
        if embedding_configuration:
            configuration = next((item for item in configurations if item["model_id"] == embedding_configuration["model_id"] and item["dimensions"] == embedding_configuration["dimensions"]), None)
            if configuration is None: raise ValueError("Indexed templates do not use the validated watsonx.ai embedding model and dimensions. Reingest templates with the configured model; embeddings will not be mixed.")
        else:
            configuration = max(configurations, key=lambda item: item.get("count", 0))
        embedding_started = time.perf_counter(); query_embedding = embedding_client.embed(retrieval_query, configuration["model_id"])
        timings["query_embedding_seconds"] = time.perf_counter() - embedding_started
        if query_embedding.dimensions != configuration["dimensions"]: raise ValueError("The query embedding dimensions do not match the indexed templates.")
        self._stage(stages, "Query embedding generated")
        candidates = self.retriever.hybrid_search(retrieval_query=retrieval_query, query_vector=query_embedding.vector, embedding_model_id=query_embedding.model_id, embedding_dimensions=query_embedding.dimensions, requirements=requirements, size=5)
        if not candidates: raise ValueError("No compatible template was found for the requested presentation.")
        self._stage(stages, "Candidate templates retrieved")
        selection_started = time.perf_counter(); selection, selection_repaired = select_template(self.watsonx, model_id, requirements, candidates)
        timings["template_selection_seconds"] = time.perf_counter() - selection_started
        if selection_repaired: warnings.append("Template selection required repair or deterministic fallback.")
        if selection.confidence < confidence_threshold: warnings.append(f"Template selection confidence {selection.confidence:.0%} is below the configured {confidence_threshold:.0%} threshold.")
        self._stage(stages, "Best template selected")
        selected_template_id = extract_selected_template_id(selection)
        template = self._full_template(selected_template_id, selection_mode="automatic")
        self._stage(stages, "Selected template retrieved")
        message_args = {"topic": topic, "complete_demand": complete_demand, "source_content": source_content, "audience": audience, "tone": tone, "required_sections": required_sections, "visual_preferences": visual_preferences, "desired_slide_count": desired_slide_count, "additional_instructions": additional_instructions}
        content, output_path, generation_warnings = self._generate_and_populate(model_id=model_id, template=template, message_args=message_args, stages=stages, timings=timings)
        warnings.extend(generation_warnings)
        timings["total_seconds"] = time.perf_counter() - started
        return PipelineResult(content, output_path, warnings, timings, stages, selection, candidates, model_id, query_embedding.model_id)

    def run_manual(self, *, template_id: str, model_id: str, topic: str, complete_demand: str, source_content: str, audience: str, tone: str, desired_slide_count: int, required_sections: list[str], visual_preferences: str, additional_instructions: str = "") -> PipelineResult:
        """Generate a presentation for a template the user picked directly, skipping retrieval and AI selection."""
        timings: dict[str, float] = {}; stages: list[str] = []; warnings: list[str] = []; started = time.perf_counter()
        if not topic.strip() or not complete_demand.strip(): raise ValueError("Presentation topic and complete demand are required.")
        self._stage(stages, "User demand received")
        template = self._full_template(template_id, selection_mode="manual")
        self._stage(stages, "Selected template retrieved")
        message_args = {"topic": topic, "complete_demand": complete_demand, "source_content": source_content, "audience": audience, "tone": tone, "required_sections": required_sections, "visual_preferences": visual_preferences, "desired_slide_count": desired_slide_count, "additional_instructions": additional_instructions}
        content, output_path, generation_warnings = self._generate_and_populate(model_id=model_id, template=template, message_args=message_args, stages=stages, timings=timings)
        warnings.extend(generation_warnings)
        timings["total_seconds"] = time.perf_counter() - started
        return PipelineResult(content, output_path, warnings, timings, stages, None, [], model_id, None)

    def _generate_and_populate(self, *, model_id: str, template: dict[str, Any], message_args: dict[str, Any], stages: list[str], timings: dict[str, float]) -> tuple[PresentationContent, Path, list[str]]:
        warnings: list[str] = []
        data = template["pptx_binary_bytes"]
        verify_checksum(data, template.get("checksum_sha256", "")); self._stage(stages, "Checksum verified")
        factual_summary = build_factual_summary(message_args["source_content"])
        self._stage(stages, "Factual summary constructed")
        desired_count = int(message_args.get("desired_slide_count") or 14)
        planned_template = build_slide_plan(template, message_args["source_content"], desired_count)
        planned_template["factual_summary"] = factual_summary
        message_args = dict(message_args)
        message_args["desired_slide_count"] = planned_template["slide_count"]
        self._stage(stages, f"{planned_template['slide_count']}-slide plan constructed")
        self._stage(stages, "Content prompt constructed")
        generation_started = time.perf_counter(); content, validation_warnings, _ = self._generate_validated(model_id=model_id, template=planned_template, message_args=message_args)
        timings["content_generation_seconds"] = time.perf_counter() - generation_started; self._stage(stages, "Slide content generated")
        self._stage(stages, "Layout fitting constraints applied")
        warnings.extend(validation_warnings); self._stage(stages, "Model output validated")
        with temporary_pptx(data, template["checksum_sha256"]) as source_path:
            output_path, population_warnings = populate_presentation(source_path, content, self.generated_dir, planned_template)
            warnings.extend(population_warnings); self._stage(stages, "PowerPoint populated"); self._stage(stages, "Output saved")
            visual_issues = validate_presentation_layout(output_path, planned_template)
            warnings.extend(f"Slide {issue.slide_number} visual validation [{issue.category}]: {issue.message}" for issue in visual_issues)
            critical = [issue for issue in visual_issues if issue.category in {"forbidden_title", "section_order", "slide_count", "empty_required_target", "duplicate_package_member", "overflow", "contrast", "bounds", "collision", "sample_text", "unresolved_instruction", "incomplete_heading", "missing_introduction_heading", "missing_introduction_body", "incorrect_semantic_mapping"}]
            if critical:
                output_path.unlink(missing_ok=True)
                raise ResponseValidationError([f"Slide {issue.slide_number}: {issue.message}" for issue in critical])
            self._stage(stages, "Visual validation completed"); self._stage(stages, "Download ready")
        self._stage(stages, "Temporary files cleaned")
        return content, output_path, warnings

    def run(self, *, template_id: str, model_id: str, topic: str, source_content: str, audience: str, tone: str, additional_instructions: str = "") -> PipelineResult:
        timings: dict[str, float] = {}; stages: list[str] = []; started = time.perf_counter()
        if not topic.strip() or not source_content.strip(): raise ValueError("Topic and source content are required.")
        self._stage(stages, "Input received")
        template = self.retriever.get(template_id)
        if not template: raise ValueError("The selected template was not found.")
        self._stage(stages, "Template retrieved"); self._stage(stages, "Template structure loaded")
        messages = build_messages(topic=topic, source_content=source_content, audience=audience, tone=tone, additional_instructions=additional_instructions, template=template)
        self._stage(stages, "Prompt constructed")
        request_start = time.perf_counter(); self._stage(stages, "watsonx.ai request sent")
        response = self.watsonx.chat(model_id, messages, max_tokens=4096, temperature=.1)
        timings["watsonx_request_seconds"] = time.perf_counter() - request_start; self._stage(stages, "Response received")
        try:
            content, warnings = validate_response(response.text, template, presentation_title=topic)
        except ResponseValidationError as first_error:
            repair_messages = build_repair_messages(messages, response.text, first_error.errors, chunk=True)
            repaired = self.watsonx.chat(model_id, repair_messages, max_tokens=4096, temperature=0)
            try: content, warnings = validate_response(repaired.text, template, presentation_title=topic)
            except ResponseValidationError as second_error: raise ResponseValidationError(["The initial response and one controlled repair attempt failed validation.", *second_error.errors]) from second_error
            warnings.insert(0, "The initial model response required one repair request.")
        self._stage(stages, "JSON parsed"); self._stage(stages, "Response validated")
        source_path = confined_path(self.project_root, template["file_path"])
        populate_start = time.perf_counter(); output_path, population_warnings = populate_presentation(source_path, content, self.generated_dir, template)
        timings["ppt_population_seconds"] = time.perf_counter() - populate_start
        self._stage(stages, "PPT populated"); self._stage(stages, "Output saved")
        timings["total_seconds"] = time.perf_counter() - started
        return PipelineResult(content, output_path, warnings + population_warnings, timings, stages)
