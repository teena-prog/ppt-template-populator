from __future__ import annotations

import re
import logging
from typing import Any

from pydantic import ValidationError

from .response_models import PresentationContent, SemanticSlideContent, SemanticSlideResponse, SlideContent, TargetContent
from .response_validator import ResponseValidationError, extract_json, format_pydantic_errors, validate_response
from .target_metadata import replaceable_targets_by_slide, target_capacity_limits, usable_body_target
from .content_normalizer import normalize_bullets

logger = logging.getLogger(__name__)


def normalize_semantic_response(raw: str | dict[str, Any]) -> dict[str, Any]:
    """Normalize harmless content aliases, never template target structures."""
    data = extract_json(raw) if isinstance(raw, str) else dict(raw)
    if "slides" not in data and isinstance(data.get("response"), dict) and set(data).issubset({"response", "title"}):
        data = dict(data["response"])
    slides = data.get("slides")
    if not isinstance(slides, list):
        return data
    normalized = []
    for raw_slide in slides:
        if not isinstance(raw_slide, dict):
            normalized.append(raw_slide)
            continue
        slide = dict(raw_slide)
        if "targets" in slide or any(key in slide for key in ("target_id", "target_kind", "content_type", "layout_name")):
            normalized.append(slide)
            continue
        if "title" not in slide and isinstance(slide.get("heading"), str):
            slide["title"] = slide.pop("heading")
        if "speaker_notes" not in slide and (slide.get("notes") is None or isinstance(slide.get("notes"), str)) and "notes" in slide:
            slide["speaker_notes"] = slide.pop("notes")
        if "bullets" not in slide:
            alias = next((key for key in ("points", "content") if key in slide), None)
            if alias is not None:
                value = slide.pop(alias)
                slide["bullets"] = normalize_bullets(value)
        normalized.append(slide)
    return {**data, "slides": normalized}


def validate_semantic_response(raw: str | dict[str, Any], expected: dict[int, str]) -> SemanticSlideResponse:
    data = normalize_semantic_response(raw)
    try:
        response = SemanticSlideResponse.model_validate(data)
    except ValidationError as exc:
        raise ResponseValidationError(format_pydantic_errors(exc, payload=data)) from exc
    counts: dict[int, int] = {}
    errors = []
    for slide in response.slides:
        counts[slide.slide_number] = counts.get(slide.slide_number, 0) + 1
        if slide.slide_number not in expected:
            errors.append(f"Unknown semantic slide number {slide.slide_number}.")
        elif slide.section_type != expected[slide.slide_number]:
            errors.append(f"Slide {slide.slide_number} section_type '{slide.section_type}' does not match '{expected[slide.slide_number]}'.")
        elif expected[slide.slide_number] == "introduction" and not slide.bullets:
            errors.append(f"Slide {slide.slide_number} mandatory Introduction body content is missing.")
    duplicates = sorted(number for number, count in counts.items() if count > 1)
    missing = sorted(set(expected) - set(counts))
    if duplicates: errors.append(f"Duplicate semantic slide numbers: {duplicates}.")
    if missing: errors.append(f"Missing semantic slide numbers: {missing}.")
    if errors: raise ResponseValidationError(errors)
    return response


def _bounded(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit: return text
    clipped = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return clipped or text[:limit]


def _bounded_bullet(text: str, limit: int) -> str | None:
    """Shorten at word boundaries; never manufacture a character fragment."""
    normalized = " ".join(text.split())
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    words = normalized.split()
    selected: list[str] = []
    for word in words:
        candidate = " ".join([*selected, word])
        if len(candidate) > limit:
            break
        selected.append(word)
    if len(selected) < min(4, len(words)):
        return None
    return " ".join(selected).rstrip(" ,;:-") or None


def shorten_heading(text: str, limit: int, fallback: str) -> str:
    """Fit a heading using whole words while preserving its semantic label."""
    normalized = " ".join(str(text).split()).strip(" ,;:-")
    trusted_fallback = " ".join(str(fallback).split()).strip(" ,;:-") or "Overview"
    filler = {"a", "an", "the", "brief", "detailed", "comprehensive", "overview", "of"}
    candidates: list[str] = []
    if normalized and len(normalized) <= max(1, limit) and len(normalized.split()) <= 8:
        candidates.append(normalized)
    if normalized:
        reduced = " ".join(word for word in normalized.split() if word.casefold() not in filler)
        if reduced:
            candidates.append(reduced)
    candidates.append(trusted_fallback)
    for candidate in candidates:
        words = candidate.split()
        result = " ".join(words[:8]).rstrip(" ,;:-")
        if result and len(result) <= max(1, limit):
            return result
    # A target unable to hold one complete word is not a valid title region.
    raise ResponseValidationError([
        "The selected template does not provide enough space for a complete slide heading."
    ])


def semantic_fallback(slide: dict[str, Any], topic: str) -> SemanticSlideContent:
    number = int(slide["slide_number"]); section = str(slide.get("section_type") or "section")
    title = str(slide.get("planned_section_title") or (topic if number == 1 else section.replace("_", " ").title()))
    excerpt = str(slide.get("source_excerpt") or "").strip()
    bullets = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", excerpt) if part.strip()][:5]
    return SemanticSlideContent(slide_number=number, section_type=section, title=title, bullets=bullets, speaker_notes=None)


def _distribute_bullets(
    bullets: list[str], targets: list[dict[str, Any]], *, slide_number: int
) -> tuple[list[tuple[dict[str, Any], list[str]]], int]:
    """Allocate complete bullets once per target, largest safe region first."""
    if not targets or not bullets:
        return [], len(bullets)
    unique: dict[tuple[int, str, int], dict[str, Any]] = {}
    for target in targets:
        key = (slide_number, target["target_kind"], int(target["target_id"]))
        if key in unique:
            raise ResponseValidationError([f"Duplicate allocation target {key}."])
        if usable_body_target(target):
            unique[key] = target
    ordered = sorted(unique.values(), key=lambda item: (
        int(target_capacity_limits(item)["maximum_characters"]),
        int(target_capacity_limits(item)["maximum_lines"]),
        int((item.get("position") or {}).get("width", 0)) * int((item.get("position") or {}).get("height", 0)),
    ), reverse=True)
    assignments: list[tuple[dict[str, Any], list[str]]] = []
    cursor = 0
    for target in ordered:
        limits = target_capacity_limits(target)
        target_points: list[str] = []
        maximum_count = int(limits["requested_bullet_count"])
        while cursor < len(bullets) and len(target_points) < maximum_count:
            remaining = int(limits["maximum_characters"]) - len("\n".join(target_points))
            candidate = _bounded_bullet(bullets[cursor], max(0, remaining - (1 if target_points else 0)))
            if candidate is None:
                break
            candidate_lines = max(1, (len(candidate) + max(1, int(limits["characters_per_line"]) or 48) - 1)
                                  // max(1, int(limits["characters_per_line"]) or 48))
            used_lines = sum(max(1, (len(point) + max(1, int(limits["characters_per_line"]) or 48) - 1)
                                 // max(1, int(limits["characters_per_line"]) or 48)) for point in target_points)
            if used_lines + candidate_lines > int(limits["maximum_lines"]):
                break
            target_points.append(candidate)
            cursor += 1
        if target_points:
            assignments.append((target, target_points))
        logger.info(
            "Target allocation slide=%s kind=%s id=%s role=%s width=%s height=%s font_size=%s "
            "maximum_lines=%s maximum_words=%s maximum_characters=%s allocated_bullets=%s remaining_characters=%s reason=largest_compatible_body_target",
            slide_number, target.get("target_kind"), target.get("target_id"), target.get("role"),
            (target.get("position") or {}).get("width"), (target.get("position") or {}).get("height"),
            target.get("font_size"), limits["maximum_lines"], limits["maximum_words"],
            limits["maximum_characters"], len(target_points),
            max(0, int(limits["maximum_characters"]) - len("\n".join(target_points))),
        )
    return assignments, len(bullets) - cursor


def map_semantic_to_targets(semantic: SemanticSlideResponse, template: dict[str, Any], presentation_title: str) -> tuple[PresentationContent, list[str]]:
    allowed = replaceable_targets_by_slide(template)
    layouts = {slide["slide_number"]: slide["layout_name"] for slide in template["slides"]}
    slide_metadata = {slide["slide_number"]: slide for slide in template["slides"]}
    mapped: list[SlideContent] = []
    warnings: list[str] = []
    for semantic_slide in sorted(semantic.slides, key=lambda item: item.slide_number):
        targets = list(allowed.get(semantic_slide.slide_number, {}).values())
        raw_targets = [item for item in slide_metadata[semantic_slide.slide_number].get("targets", [])
                       if item.get("replaceable")]
        raw_keys = [(semantic_slide.slide_number, item.get("target_kind"), item.get("target_id"))
                    for item in raw_targets]
        duplicates = sorted({key for key in raw_keys if raw_keys.count(key) > 1}, key=str)
        if duplicates:
            raise ResponseValidationError([f"Duplicate allocation target {key}." for key in duplicates])
        title_targets = [item for item in targets if item.get("role") == "title"]
        body_targets = [item for item in targets if usable_body_target(item)]
        required_body_targets = [item for item in body_targets if item.get("required")]
        if any(item.get("multi_content_region") for item in body_targets):
            pass
        elif required_body_targets:
            body_targets = required_body_targets
        elif body_targets:
            body_targets = [max(body_targets, key=lambda item: (
                int((item.get("capacity") or {}).get("maximum_lines", 0)),
                int((item.get("position") or {}).get("width", 0)) * int((item.get("position") or {}).get("height", 0)),
            ))]
        if not title_targets:
            raise ResponseValidationError([f"Slide {semantic_slide.slide_number} has no authoritative title target."])
        title_target = sorted(title_targets, key=lambda item: (-float(item.get("font_size") or 0), item["target_id"]))[0]
        is_closing = semantic_slide.section_type in {"closing", "qa", "conclusion_qa"}
        planned_title = str(slide_metadata[semantic_slide.slide_number].get("planned_section_title") or "")
        title_text = (
            "Thank You"
            if is_closing
            else presentation_title
            if semantic_slide.section_type == "title"
            else planned_title
            if semantic_slide.section_type == "introduction" and planned_title
            else semantic_slide.title
        )
        bullets = [] if is_closing else semantic_slide.bullets
        try:
            internal = [TargetContent(target_kind=title_target["target_kind"], target_id=title_target["target_id"], content_type="title", text=shorten_heading(title_text, int(title_target["approximate_max_characters"]), str(slide_metadata[semantic_slide.slide_number].get("planned_section_title") or semantic_slide.section_type)))]
        except ValidationError as exc:
            raise ResponseValidationError(format_pydantic_errors(
                exc, slide_number=semantic_slide.slide_number,
                target_id=int(title_target["target_id"]),
            )) from exc
        if semantic_slide.section_type == "title":
            subtitle = next((item for item in targets if item.get("role") == "subtitle"), None)
            if subtitle is not None:
                excerpt = str(slide_metadata[semantic_slide.slide_number].get("source_excerpt") or "").strip()
                subtitle_text = semantic_slide.bullets[0] if semantic_slide.bullets else excerpt
                if subtitle_text:
                    try:
                        internal.append(TargetContent(target_kind=subtitle["target_kind"], target_id=subtitle["target_id"], content_type="title", text=shorten_heading(subtitle_text, int(subtitle["approximate_max_characters"]), "Presentation Overview")))
                    except ValidationError as exc:
                        raise ResponseValidationError(format_pydantic_errors(
                            exc, slide_number=semantic_slide.slide_number,
                            target_id=int(subtitle["target_id"]),
                        )) from exc
        assignments, omitted = _distribute_bullets(
            bullets, body_targets, slide_number=semantic_slide.slide_number
        )
        if bullets and not assignments and not is_closing and semantic_slide.section_type != "title":
            raise ResponseValidationError([
                f"The selected template does not provide enough space for content on slide {semantic_slide.slide_number}. Try a compatible template or reduce this section."
            ])
        if omitted:
            warnings.append(
                f"Slide {semantic_slide.slide_number}: omitted {omitted} bullet(s) that could not fit safely; complete allocated bullets were preserved."
            )
        for target, points in assignments:
            try:
                internal.append(TargetContent(target_kind=target["target_kind"], target_id=target["target_id"], content_type="bullets", bullets=points))
            except ValidationError as exc:
                raise ResponseValidationError(format_pydantic_errors(
                    exc, slide_number=semantic_slide.slide_number,
                    target_id=int(target["target_id"]),
                )) from exc
        mapped.append(SlideContent(slide_number=semantic_slide.slide_number, layout_name=layouts[semantic_slide.slide_number], section_type=semantic_slide.section_type, targets=internal, speaker_notes=semantic_slide.speaker_notes))
    validated, validation_warnings = validate_response(
        {"slides": [slide.model_dump() for slide in mapped]}, template,
        presentation_title=presentation_title,
    )
    return validated, [*warnings, *validation_warnings]
