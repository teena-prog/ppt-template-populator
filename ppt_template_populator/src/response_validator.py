from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any
from pydantic import ValidationError
from .response_models import MissingTargetRecoveryResponse, OrderedTargetRecoveryResponse, PresentationContent, RecoveredTargetContent, SlideChunkResponse, SlideContent, TargetContent
from .target_metadata import required_targets_by_slide


class ResponseValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class TargetAnalysis:
    slides: list[SlideContent]
    recovery_targets: list[dict[str, Any]]
    warnings: list[str]
    structural_errors: list[str]


@dataclass
class RecoveryAnalysis:
    recovered: list[Any]
    unresolved: list[dict[str, Any]]
    errors: list[str]


def format_pydantic_errors(exc: ValidationError) -> list[str]:
    errors = []
    for item in exc.errors():
        path = ".".join(str(part) for part in item["loc"])
        errors.append(f"{path}: {item['msg']}" if path else item["msg"])
    return errors


def extract_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced: candidate = fenced.group(1).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        start = candidate.find("{")
        if start < 0: raise ResponseValidationError(["The model response did not contain a JSON object."])
        try: value, _ = decoder.raw_decode(candidate[start:])
        except json.JSONDecodeError as exc: raise ResponseValidationError([f"Invalid JSON: {exc.msg}."]) from exc
    if not isinstance(value, dict): raise ResponseValidationError(["The JSON root must be an object."])
    return value


def safe_response_structure(raw: str) -> dict[str, Any]:
    """Describe response keys without retaining or exposing generated content."""
    try: data = extract_json(raw)
    except ResponseValidationError: return {"json_object": False, "markdown_fence": raw.strip().startswith("```")}
    slides = data.get("slides")
    summary: dict[str, Any] = {"json_object": True, "markdown_fence": raw.strip().startswith("```"), "top_level_keys": sorted(data), "slides_type": type(slides).__name__}
    if isinstance(slides, list):
        summary["slide_count"] = len(slides)
        summary["slides"] = [{"keys": sorted(slide), "target_container": "targets" if "targets" in slide else "placeholders" if "placeholders" in slide else None, "target_keys": sorted({key for target in slide.get("targets", slide.get("placeholders", [])) if isinstance(target, dict) for key in target if key != "content"})} for slide in slides if isinstance(slide, dict)]
    return summary


def _allowed(template: dict[str, Any]) -> dict[int, dict[tuple[str, int], dict[str, Any]]]:
    return required_targets_by_slide(template)


def _fill_trusted_layout_names(data: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    copied = dict(data); layouts = {slide["slide_number"]: slide["layout_name"] for slide in template["slides"]}; slides = []
    for raw_slide in copied.get("slides", []):
        item = dict(raw_slide) if isinstance(raw_slide, dict) else raw_slide
        if isinstance(item, dict) and "layout_name" not in item and item.get("slide_number") in layouts: item["layout_name"] = layouts[item["slide_number"]]
        slides.append(item)
    if "slides" in copied: copied["slides"] = slides
    return copied


def _validate_semantics(content: PresentationContent, template: dict[str, Any]) -> tuple[PresentationContent, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    allowed = _allowed(template)
    seen_slides: set[int] = set()
    normalized_contents: list[str] = []
    for slide in content.slides:
        if slide.slide_number not in allowed:
            errors.append(f"Unknown slide number {slide.slide_number}."); continue
        if slide.slide_number in seen_slides: errors.append(f"Duplicate slide number {slide.slide_number}.")
        seen_slides.add(slide.slide_number)
        expected_layout = next(s["layout_name"] for s in template["slides"] if s["slide_number"] == slide.slide_number)
        if slide.layout_name != expected_layout: warnings.append(f"Slide {slide.slide_number} layout label differs from the template.")
        seen_targets: set[tuple[str, int]] = set()
        for item in slide.targets:
            key = (item.target_kind, item.target_id); label = "placeholder" if item.target_kind == "placeholder" else "shape"
            if key not in allowed[slide.slide_number]: errors.append(f"Unknown {label} {item.target_id} on slide {slide.slide_number}."); continue
            if key in seen_targets: errors.append(f"Duplicate {label} {item.target_id} on slide {slide.slide_number}.")
            seen_targets.add(key)
            limit = allowed[slide.slide_number][key]["approximate_max_characters"]
            length = len(item.content)
            if length > limit * 1.5:
                errors.append(f"Slide {slide.slide_number}, {label} {item.target_id} content is {length} characters; maximum is {limit} (extreme overflow).")
            elif length > limit * 1.15:
                errors.append(f"Slide {slide.slide_number}, {label} {item.target_id} content is {length} characters; maximum is {limit} (significant overflow).")
            elif length > limit:
                warnings.append(f"Slide {slide.slide_number}, {label} {item.target_id} has minor overflow: {length} characters for an approximate {limit}-character capacity.")
            normalized_contents.append(re.sub(r"\s+", " ", item.content.lower()).strip())
        missing = set(allowed[slide.slide_number]) - seen_targets
        if missing:
            errors.extend(f"Slide {slide.slide_number}, {kind} {target_id}: required content was not generated." for kind, target_id in sorted(missing))
    missing_slides = {number for number, targets in allowed.items() if targets} - seen_slides
    if missing_slides: errors.append(f"Missing required slides: {sorted(missing_slides)}.")
    repeats = [text for text, count in Counter(normalized_contents).items() if text and count > 1]
    if repeats: warnings.append(f"Duplicate content detected in {len(repeats)} target value(s).")
    if errors: raise ResponseValidationError(errors)
    return content, warnings


def validate_response(raw: str | dict[str, Any], template: dict[str, Any]) -> tuple[PresentationContent, list[str]]:
    data = extract_json(raw) if isinstance(raw, str) else raw
    data = _fill_trusted_layout_names(data, template)
    try: content = PresentationContent.model_validate(data)
    except ValidationError as exc: raise ResponseValidationError(format_pydantic_errors(exc)) from exc
    return _validate_semantics(content, template)


def validate_chunk_response(raw: str | dict[str, Any], template: dict[str, Any]) -> tuple[SlideChunkResponse, list[str]]:
    chunk = parse_chunk_response(raw, template)
    wrapped = PresentationContent(presentation_title="Chunk", slides=chunk.slides)
    _, warnings = _validate_semantics(wrapped, template)
    return chunk, warnings


def parse_chunk_response(raw: str | dict[str, Any], template: dict[str, Any]) -> SlideChunkResponse:
    data = extract_json(raw) if isinstance(raw, str) else raw
    data = _fill_trusted_layout_names(data, template)
    try: chunk = SlideChunkResponse.model_validate(data)
    except ValidationError as exc: raise ResponseValidationError(format_pydantic_errors(exc)) from exc
    return chunk


def missing_required_targets(slides: list[Any], template: dict[str, Any]) -> list[dict[str, Any]]:
    required = required_targets_by_slide(template)
    present = {(slide.slide_number, item.target_kind, item.target_id) for slide in slides for item in slide.targets}
    return [metadata for slide_number, targets in required.items() for (kind, target_id), metadata in targets.items() if (slide_number, kind, target_id) not in present]


def analyze_chunk_targets(chunk: SlideChunkResponse, template: dict[str, Any]) -> TargetAnalysis:
    required = required_targets_by_slide(template); layouts = {slide["slide_number"]: slide["layout_name"] for slide in template["slides"]}
    analyzed: list[SlideContent] = []; recovery: dict[tuple[int, str, int], dict[str, Any]] = {}; warnings = []; errors = []
    seen_slides: set[int] = set()
    by_number: dict[int, SlideContent] = {}
    for slide in chunk.slides:
        if slide.slide_number not in required: errors.append(f"Unknown slide number {slide.slide_number}."); continue
        if slide.slide_number in seen_slides: errors.append(f"Duplicate slide number {slide.slide_number}."); continue
        seen_slides.add(slide.slide_number); by_number[slide.slide_number] = slide
    for number, expected in required.items():
        source = by_number.get(number); valid = []; seen: set[tuple[str, int]] = set()
        if source is not None:
            for item in source.targets:
                key = (item.target_kind, item.target_id)
                if key in seen:
                    warnings.append(
                        f"Discarded duplicate {item.target_kind} {item.target_id} "
                        f"returned for slide {number}."
                    )
                    continue
                seen.add(key)
                metadata = expected.get(key)
                if metadata is None:
                    warnings.append(
                        f"Discarded unknown or non-required {item.target_kind} "
                        f"{item.target_id} returned for slide {number}."
                    )
                    continue
                limit = metadata["approximate_max_characters"]; length = len(item.content)
                if length > limit * 1.15: recovery[(number, *key)] = metadata
                else:
                    valid.append(TargetContent(target_kind=item.target_kind, target_id=item.target_id, content=item.content))
                    if length > limit: warnings.append(f"Slide {number}, {item.target_kind} {item.target_id} has minor overflow: {length} characters for an approximate {limit}-character capacity.")
        for key, metadata in expected.items():
            if key not in seen: recovery[(number, *key)] = metadata
        analyzed.append(SlideContent(slide_number=number, layout_name=layouts[number], targets=valid, speaker_notes=source.speaker_notes if source else None))
    return TargetAnalysis(analyzed, list(recovery.values()), warnings, errors)


def salvage_recoverable_chunk(
    raw: str | dict[str, Any],
    template: dict[str, Any],
) -> SlideChunkResponse:
    """
    Preserve valid generated targets and remove missing/overflow targets so
    they can be requested later through focused target recovery.
    """
    chunk = parse_chunk_response(raw, template)
    allowed = required_targets_by_slide(template)

    salvaged_slides = []

    for slide in chunk.slides:
        if slide.slide_number not in allowed:
            raise ResponseValidationError(
                [f"Unknown slide number {slide.slide_number}."]
            )

        valid_targets = []
        seen: set[tuple[str, int]] = set()

        for target in slide.targets:
            key = (target.target_kind, target.target_id)

            if key not in allowed[slide.slide_number]:
                raise ResponseValidationError(
                    [
                        f"Unknown {target.target_kind} {target.target_id} "
                        f"on slide {slide.slide_number}."
                    ]
                )

            if key in seen:
                raise ResponseValidationError(
                    [
                        f"Duplicate {target.target_kind} {target.target_id} "
                        f"on slide {slide.slide_number}."
                    ]
                )

            seen.add(key)

            limit = int(
                allowed[slide.slide_number][key][
                    "approximate_max_characters"
                ]
            )

            # Remove significant/extreme overflow from the partial response.
            # It will be regenerated by focused recovery.
            if len(target.content) > limit * 1.15:
                continue

            valid_targets.append(target)

        salvaged_slides.append(
            slide.model_copy(update={"targets": valid_targets})
        )

    return SlideChunkResponse(slides=salvaged_slides)

def validate_missing_target_recovery(
    raw: str | dict[str, Any],
    requested: list[dict[str, Any]],
) -> MissingTargetRecoveryResponse:
    data = extract_json(raw) if isinstance(raw, str) else raw

    try:
        response = MissingTargetRecoveryResponse.model_validate(data)
    except ValidationError as exc:
        raise ResponseValidationError(
            format_pydantic_errors(exc)
        ) from exc

    requested_by_key = {
        (
            item["slide_number"],
            item["target_kind"],
            item["target_id"],
        ): item
        for item in requested
    }

    expected = set(requested_by_key)

    actual = [
        (
            item.slide_number,
            item.target_kind,
            item.target_id,
        )
        for item in response.targets
    ]

    duplicates = sorted(
        {key for key in actual if actual.count(key) > 1}
    )
    unresolved = sorted(expected - set(actual))
    invented = sorted(set(actual) - expected)

    errors = []

    if duplicates:
        errors.append(
            f"Duplicate recovered target keys: {duplicates}."
        )

    if invented:
        errors.append(
            f"Unexpected recovered target keys: {invented}."
        )

    if unresolved:
        errors.append(
            f"Unresolved required target keys: {unresolved}."
        )

    for item in response.targets:
        key = (
            item.slide_number,
            item.target_kind,
            item.target_id,
        )

        if key not in requested_by_key:
            continue

        limit = int(
            requested_by_key[key].get("maximum_characters", 0)
        )

        if limit > 0 and len(item.content) > limit:
            errors.append(
                f"Recovered slide {item.slide_number}, "
                f"{item.target_kind} {item.target_id} content is "
                f"{len(item.content)} characters; maximum is {limit}."
            )

    if errors:
        raise ResponseValidationError(errors)

    return response


def analyze_missing_target_recovery(
    raw: str | dict[str, Any],
    requested: list[dict[str, Any]],
) -> RecoveryAnalysis:
    """Salvage valid requested targets from a partial recovery response."""
    requested_by_key = {
        (item["slide_number"], item["target_kind"], item["target_id"]): item
        for item in requested
    }

    try:
        data = extract_json(raw) if isinstance(raw, str) else raw
        response = MissingTargetRecoveryResponse.model_validate(data)
    except ValidationError as exc:
        return RecoveryAnalysis([], list(requested), format_pydantic_errors(exc))
    except ResponseValidationError as exc:
        return RecoveryAnalysis([], list(requested), list(exc.errors))

    counts = Counter(
        (item.slide_number, item.target_kind, item.target_id)
        for item in response.targets
    )
    recovered = []
    recovered_keys: set[tuple[int, str, int]] = set()
    errors: list[str] = []

    for item in response.targets:
        key = (item.slide_number, item.target_kind, item.target_id)
        metadata = requested_by_key.get(key)
        if metadata is None:
            errors.append(f"Unexpected recovered target key: {key}.")
            continue
        if counts[key] > 1:
            errors.append(f"Duplicate recovered target key: {key}.")
            continue

        limit = int(metadata.get("maximum_characters", 0) or 0)
        if limit and len(item.content) > limit:
            errors.append(
                f"Recovered slide {item.slide_number}, {item.target_kind} "
                f"{item.target_id} content is {len(item.content)} characters; "
                f"maximum is {limit}."
            )
            continue

        recovered.append(item)
        recovered_keys.add(key)

    unresolved = [
        item for key, item in requested_by_key.items() if key not in recovered_keys
    ]
    return RecoveryAnalysis(recovered, unresolved, errors)


def analyze_ordered_target_recovery(
    raw: str | dict[str, Any],
    requested: list[dict[str, Any]],
) -> RecoveryAnalysis:
    """Map ordered model content to trusted target IDs supplied by Python."""
    try:
        data = extract_json(raw) if isinstance(raw, str) else raw
        response = OrderedTargetRecoveryResponse.model_validate(data)
    except ValidationError as exc:
        return RecoveryAnalysis([], list(requested), format_pydantic_errors(exc))
    except ResponseValidationError as exc:
        return RecoveryAnalysis([], list(requested), list(exc.errors))

    recovered = []
    unresolved = []
    errors = []
    if len(response.contents) != len(requested):
        errors.append(
            f"Ordered recovery returned {len(response.contents)} content values; "
            f"expected {len(requested)}."
        )

    for index, metadata in enumerate(requested):
        if index >= len(response.contents):
            unresolved.append(metadata)
            continue
        content = response.contents[index]
        limit = int(metadata.get("maximum_characters", 0) or 0)
        if limit and len(content) > limit:
            errors.append(
                f"Ordered recovery slot {index + 1} contains {len(content)} "
                f"characters; maximum is {limit}."
            )
            unresolved.append(metadata)
            continue
        recovered.append(
            RecoveredTargetContent(
                slide_number=metadata["slide_number"],
                target_kind=metadata["target_kind"],
                target_id=metadata["target_id"],
                content=content,
            )
        )

    if len(response.contents) > len(requested):
        errors.append(
            f"Discarded {len(response.contents) - len(requested)} extra "
            "ordered recovery content value(s)."
        )
    return RecoveryAnalysis(recovered, unresolved, errors)
