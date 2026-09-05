from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from .ppt_populator import populate_presentation
from .prompt_builder import build_messages, build_missing_target_recovery_messages, build_repair_messages
from .response_models import PresentationContent, RecoveredTargetContent, TargetContent
from .response_validator import (
    ResponseValidationError,
    analyze_chunk_targets,
    analyze_ordered_target_recovery,
    extract_json,
    parse_chunk_response,
    safe_response_structure,
    validate_chunk_response,
    validate_response,
)
from .security import confined_path
from .template_binary import temporary_pptx, verify_checksum
from .template_selector import extract_selected_template_id, select_template
from .target_metadata import normalize_template_targets, required_targets_by_slide


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
        existing = str(metadata.get("existing_text") or "").strip()
        if existing:
            return existing[:limit]
        role = str(metadata.get("role", "")).lower()
        generic = {
            "title": "Overview", "subtitle": "Key details", "heading": "Highlights",
            "body": "Content for this section was not generated automatically; please review and edit.",
            "caption": "Details",
        }.get(role, "Content pending manual review.")
        return generic[:limit]

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
        data = extract_json(raw)
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
                recovered_by_key[key] = RecoveredTargetContent(
                    slide_number=item["slide_number"],
                    target_kind=item["target_kind"],
                    target_id=item["target_id"],
                    content=self._fallback_target_content(item),
                )
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
        chunks = self._plan_chunks(template)
        merged_slides = []; recovery_targets = []; warnings: list[str] = []; calls = 0; title = message_args.get("topic", "Presentation")
        for chunk in chunks:
            chunk_set = set(chunk); subset = self._template_subset(template, chunk_set)
            messages = build_messages(template=template, slide_numbers=chunk_set, **message_args)
            response = self.watsonx.chat(model_id, messages, max_tokens=4096, temperature=.1); calls += 1
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
            except ResponseValidationError as initial_error:
                initial_errors = list(initial_error.errors)
                response_payload = {"slides": []}
                logging.getLogger(__name__).warning("Chunk response was not valid JSON: %s; safe structure=%s", initial_error.errors, safe_response_structure(response.text))
            else:
                if ignored_slides:
                    warnings.append(f"Ignored out-of-chunk slides returned by the model: {ignored_slides}.")
                if malformed_slides:
                    warnings.append(f"Discarded {malformed_slides} malformed slide entr{'y' if malformed_slides == 1 else 'ies'} returned by the model; affected required targets will be recovered.")
                try:
                    content, chunk_warnings = validate_chunk_response(response_payload, subset)
                    merged_slides.extend(content.slides); warnings.extend(chunk_warnings)
                    continue
                except ResponseValidationError as initial_error:
                    initial_errors = list(initial_error.errors)
                    logging.getLogger(__name__).warning("Chunk response validation failed: %s; safe structure=%s", initial_error.errors, safe_response_structure(response.text))
            try:
                parsed = parse_chunk_response(response_payload, subset)
                analysis = analyze_chunk_targets(parsed, subset)
            except ResponseValidationError:
                analysis = None
            if analysis is None or analysis.structural_errors:
                repair = self.watsonx.chat(model_id, build_repair_messages(messages, response.text, analysis.structural_errors if analysis else initial_errors, chunk=True), max_tokens=4096, temperature=0); calls += 1
                try:
                    repair_payload, ignored_repair_slides, malformed_repair_slides = self._response_for_chunk(repair.text, chunk_set)
                except ResponseValidationError as exc: raise ResponseValidationError(["Slide content failed schema validation after one controlled repair request.", *exc.errors]) from exc
                if ignored_repair_slides:
                    warnings.append(f"Ignored out-of-chunk slides returned during repair: {ignored_repair_slides}.")
                if malformed_repair_slides:
                    warnings.append(f"Discarded {malformed_repair_slides} malformed slide entr{'y' if malformed_repair_slides == 1 else 'ies'} returned during repair; affected required targets will be recovered.")
                try: parsed = parse_chunk_response(repair_payload, subset)
                except ResponseValidationError as exc: raise ResponseValidationError(["Slide content failed schema validation after one controlled repair request.", *exc.errors]) from exc
                analysis = analyze_chunk_targets(parsed, subset)
                if analysis.structural_errors: raise ResponseValidationError(["Slide content remained structurally unsafe after one controlled repair request.", *analysis.structural_errors])
                warnings.append(f"Slides {chunk} required one controlled structural repair request.")
            merged_slides.extend(analysis.slides); recovery_targets.extend(analysis.recovery_targets); warnings.extend(analysis.warnings)
        if recovery_targets:
            unique = {(item["slide_number"], item["target_kind"], item["target_id"]): item for item in recovery_targets}
            request = [{"slide_number": item["slide_number"], "target_kind": item["target_kind"], "target_id": item["target_id"], "role": item["role"], "maximum_characters": item["approximate_max_characters"], "existing_text": item.get("existing_text", "")} for item in unique.values()]
            recovered, recovery_warnings, recovery_calls = self._recover_targets(
                model_id=model_id,
                request=request,
                message_args=message_args,
            )
            calls += recovery_calls
            slides_by_number = {slide.slide_number: slide for slide in merged_slides}
            for item in recovered:
                slides_by_number[item.slide_number].targets.append(TargetContent(target_kind=item.target_kind, target_id=item.target_id, content=item.content))
            warnings.extend(recovery_warnings); warnings.append(f"Focused recovery supplied {len(recovered)} missing or invalid required target(s).")
        merged = PresentationContent(presentation_title=title, slides=sorted(merged_slides, key=lambda slide: slide.slide_number))
        final, final_warnings = validate_response(merged.model_dump(), template); warnings.extend(final_warnings)
        return final, warnings, calls

    def run_automatic(self, *, model_id: str, embedding_client: Any, topic: str, complete_demand: str, source_content: str, audience: str, tone: str, desired_slide_count: int, required_sections: list[str], visual_preferences: str, additional_instructions: str = "", confidence_threshold: float = .60, embedding_configuration: dict[str, Any] | None = None) -> PipelineResult:
        timings: dict[str, float] = {}; stages: list[str] = []; warnings: list[str] = []; started = time.perf_counter()
        if not topic.strip() or not complete_demand.strip(): raise ValueError("Presentation topic and complete demand are required.")
        requirements = {"topic": topic, "complete_demand": complete_demand, "source_content": source_content, "audience": audience, "tone": tone, "desired_slide_count": desired_slide_count, "required_sections": required_sections, "visual_preferences": visual_preferences, "additional_instructions": additional_instructions}
        self._stage(stages, "User demand received")
        retrieval_query = " | ".join([topic, complete_demand, audience, tone, ", ".join(required_sections), visual_preferences, additional_instructions])
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
        template = self.retriever.get_full(selected_template_id)
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
        template = self.retriever.get_full(template_id)
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
        self._stage(stages, "Content prompt constructed")
        generation_started = time.perf_counter(); content, validation_warnings, _ = self._generate_validated(model_id=model_id, template=template, message_args=message_args)
        timings["content_generation_seconds"] = time.perf_counter() - generation_started; self._stage(stages, "Slide content generated")
        warnings.extend(validation_warnings); self._stage(stages, "Model output validated")
        with temporary_pptx(data, template["checksum_sha256"]) as source_path:
            output_path, population_warnings = populate_presentation(source_path, content, self.generated_dir, template)
            warnings.extend(population_warnings); self._stage(stages, "PowerPoint populated"); self._stage(stages, "Output saved"); self._stage(stages, "Download ready")
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
            content, warnings = validate_response(response.text, template)
        except ResponseValidationError as first_error:
            repair_messages = build_repair_messages(messages, response.text, first_error.errors)
            repaired = self.watsonx.chat(model_id, repair_messages, max_tokens=4096, temperature=0)
            try: content, warnings = validate_response(repaired.text, template)
            except ResponseValidationError as second_error: raise ResponseValidationError(["The initial response and one controlled repair attempt failed validation.", *second_error.errors]) from second_error
            warnings.insert(0, "The initial model response required one repair request.")
        self._stage(stages, "JSON parsed"); self._stage(stages, "Response validated")
        source_path = confined_path(self.project_root, template["file_path"])
        populate_start = time.perf_counter(); output_path, population_warnings = populate_presentation(source_path, content, self.generated_dir, template)
        timings["ppt_population_seconds"] = time.perf_counter() - populate_start
        self._stage(stages, "PPT populated"); self._stage(stages, "Output saved")
        timings["total_seconds"] = time.perf_counter() - started
        return PipelineResult(content, output_path, warnings + population_warnings, timings, stages)
