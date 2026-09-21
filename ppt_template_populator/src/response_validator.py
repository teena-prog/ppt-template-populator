from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any
from pydantic import ValidationError
from .response_models import MissingTargetRecoveryResponse, OrderedTargetRecoveryResponse, PresentationContent, RecoveredTargetContent, SlideChunkResponse, SlideContent, TargetContent
from .target_metadata import expected_content_type, replaceable_targets_by_slide, required_targets_by_slide
from .content_normalizer import BulletNormalizationError, normalize_bullets


@dataclass(frozen=True)
class ValidationFailure:
    stage: str
    slide_number: int | None
    target_kind: str | None
    target_id: int | None
    field_path: str
    reason: str
    repairable: bool


class ResponseValidationError(ValueError):
    def __init__(self, errors: list[str], failures: list[ValidationFailure] | None = None):
        self.errors = errors
        self.failures = failures or []
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


def format_pydantic_errors(
    exc: ValidationError,
    *,
    payload: Any | None = None,
    slide_number: int | None = None,
    target_id: int | None = None,
) -> list[str]:
    """Render Pydantic failures without URLs, input values, or source content."""
    errors = []
    for item in exc.errors():
        path = ".".join(str(part) for part in item["loc"])
        resolved_slide = slide_number
        resolved_target = target_id
        node = payload
        try:
            for part in item["loc"]:
                if isinstance(node, dict):
                    if resolved_slide is None and isinstance(node.get("slide_number"), int):
                        resolved_slide = node["slide_number"]
                    if resolved_target is None and isinstance(node.get("target_id"), int):
                        resolved_target = node["target_id"]
                    node = node.get(part)
                elif isinstance(node, list) and isinstance(part, int):
                    node = node[part]
                else:
                    break
            if isinstance(node, dict):
                if resolved_slide is None and isinstance(node.get("slide_number"), int):
                    resolved_slide = node["slide_number"]
                if resolved_target is None and isinstance(node.get("target_id"), int):
                    resolved_target = node["target_id"]
        except (IndexError, KeyError, TypeError):
            pass
        context = []
        if resolved_slide is not None:
            context.append(f"slide {resolved_slide}")
        if resolved_target is not None:
            context.append(f"target {resolved_target}")
        base = f"{path}: {item['msg']}" if path else item["msg"]
        # Preserve the canonical field-path entry consumed by existing repair
        # code, then add a contextual entry suitable for diagnostics/UI.
        errors.append(base)
        if context:
            errors.append(f"{', '.join(context)}: {base}")
    return errors


def _model_validate_or_response_error(
    model: Any,
    payload: Any,
    *,
    slide_number: int | None = None,
    target_id: int | None = None,
) -> Any:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        messages = format_pydantic_errors(
            exc, payload=payload, slide_number=slide_number, target_id=target_id
        )
        failures: list[ValidationFailure] = []
        for item in exc.errors():
            resolved_slide, resolved_kind, resolved_target = slide_number, None, target_id
            node = payload
            try:
                for part in item["loc"]:
                    if isinstance(node, dict):
                        if resolved_slide is None and isinstance(node.get("slide_number"), int):
                            resolved_slide = node["slide_number"]
                        if resolved_kind is None and node.get("target_kind") in {"shape", "placeholder"}:
                            resolved_kind = node["target_kind"]
                        if resolved_target is None and isinstance(node.get("target_id"), int):
                            resolved_target = node["target_id"]
                        node = node.get(part)
                    elif isinstance(node, list) and isinstance(part, int):
                        node = node[part]
                    else:
                        break
                if isinstance(node, dict):
                    if resolved_slide is None and isinstance(node.get("slide_number"), int):
                        resolved_slide = node["slide_number"]
                    if resolved_kind is None and node.get("target_kind") in {"shape", "placeholder"}:
                        resolved_kind = node["target_kind"]
                    if resolved_target is None and isinstance(node.get("target_id"), int):
                        resolved_target = node["target_id"]
            except (IndexError, KeyError, TypeError):
                pass
            path = ".".join(str(part) for part in item["loc"])
            reason = str(item["msg"])
            unsafe_identity = any(field in path for field in ("slide_number", "target_kind", "target_id"))
            repairable = str(item.get("type", "")).casefold() == "missing" or not unsafe_identity
            failures.append(ValidationFailure(
                stage="schema_validation", slide_number=resolved_slide,
                target_kind=resolved_kind, target_id=resolved_target,
                field_path=path, reason=reason, repairable=repairable,
            ))
        raise ResponseValidationError(messages, failures) from exc


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


def _merge_slide_fragments(data: dict[str, Any]) -> dict[str, Any]:
    slides = data.get("slides")
    if not isinstance(slides, list):
        return data
    merged: list[Any] = []
    by_number: dict[int, dict[str, Any]] = {}
    target_maps: dict[int, dict[tuple[str, int], dict[str, Any]]] = {}
    for slide_index, raw_slide in enumerate(slides):
        if not isinstance(raw_slide, dict) or not isinstance(raw_slide.get("slide_number"), int):
            merged.append(raw_slide)
            continue
        number = raw_slide["slide_number"]
        incoming = dict(raw_slide)
        incoming_targets = incoming.get("targets", [])
        if not isinstance(incoming_targets, list):
            merged.append(incoming)
            continue
        new_slide = number not in by_number
        if new_slide:
            if "targets" in incoming:
                incoming["targets"] = []
            by_number[number] = incoming
            target_maps[number] = {}
            merged.append(incoming)
        else:
            current = by_number[number]
            for key, value in incoming.items():
                if key == "targets":
                    continue
                if key in current and current[key] != value:
                    raise ResponseValidationError([
                        f"slides.{slide_index}: conflicting fragments for slide {number} field '{key}'."
                    ])
                current.setdefault(key, value)
        if "targets" not in incoming:
            continue
        current_targets = by_number[number].setdefault("targets", [])
        keyed = target_maps[number]
        for target_index, raw_target in enumerate(incoming_targets):
            if not isinstance(raw_target, dict):
                current_targets.append(raw_target)
                continue
            kind, target_id = raw_target.get("target_kind"), raw_target.get("target_id")
            if kind not in {"placeholder", "shape"} or not isinstance(target_id, int):
                current_targets.append(dict(raw_target))
                continue
            key = (kind, target_id)
            existing = keyed.get(key)
            if new_slide:
                copied = dict(raw_target)
                keyed.setdefault(key, copied)
                current_targets.append(copied)
            elif existing is None:
                copied = dict(raw_target)
                keyed[key] = copied
                current_targets.append(copied)
            elif existing != raw_target:
                raise ResponseValidationError([
                    f"Slide {number}, {kind} {target_id}: conflicting duplicate target content."
                ])
            # An byte-for-byte identical target fragment is idempotent.
    copied = dict(data)
    copied["slides"] = merged
    return copied


_SLIDE_ONLY_KEYS = {"slide_number", "layout_name", "section_type", "speaker_notes", "targets"}
_TARGET_KEYS = {"target_kind", "target_id", "content_type", "text", "bullets", "content"}


def _normalize_misnested_slide_content(data: dict[str, Any]) -> dict[str, Any]:
    """Lift only unmistakable slide objects and relocate unambiguous notes.

    This is deliberately structural rather than permissive: mixed slide/target
    objects and conflicting notes remain errors, and no identifier is created.
    """
    raw_slides = data.get("slides")
    if not isinstance(raw_slides, list):
        return data
    queue = list(raw_slides)
    normalized: list[Any] = []
    position = 0
    while position < len(queue):
        raw_slide = queue[position]
        position += 1
        if not isinstance(raw_slide, dict) or not isinstance(raw_slide.get("targets"), list):
            normalized.append(raw_slide)
            continue
        slide = dict(raw_slide)
        targets: list[Any] = []
        lifted: list[dict[str, Any]] = []
        notes = slide.get("speaker_notes")
        for target_index, raw_target in enumerate(slide["targets"]):
            if not isinstance(raw_target, dict):
                targets.append(raw_target)
                continue
            keys = set(raw_target)
            is_slide = (
                isinstance(raw_target.get("slide_number"), int)
                and isinstance(raw_target.get("layout_name"), str)
                and isinstance(raw_target.get("section_type"), str)
                and isinstance(raw_target.get("targets"), list)
            )
            has_target_identity = bool(keys & {"target_kind", "target_id", "content_type", "text", "bullets", "content"})
            if is_slide and not has_target_identity and keys.issubset(_SLIDE_ONLY_KEYS):
                lifted.append(dict(raw_target))
                continue
            if is_slide:
                raise ResponseValidationError([
                    f"Slide {slide.get('slide_number')}, targets.{target_index}: ambiguous object mixes slide and target fields."
                ])
            target = dict(raw_target)
            if "speaker_notes" in target:
                misplaced = target.pop("speaker_notes")
                if notes not in (None, ""):
                    raise ResponseValidationError([
                        f"Slide {slide.get('slide_number')}, targets.{target_index}: speaker_notes conflicts with parent slide notes."
                    ])
                if misplaced is not None and not isinstance(misplaced, str):
                    raise ResponseValidationError([
                        f"Slide {slide.get('slide_number')}, targets.{target_index}: speaker_notes has an invalid type."
                    ])
                notes = misplaced
            targets.append(target)
        slide["targets"] = targets
        if notes is not None or "speaker_notes" in slide:
            slide["speaker_notes"] = notes
        normalized.append(slide)
        queue[position:position] = lifted
    copied = dict(data)
    copied["slides"] = normalized
    return copied


def normalize_model_response(
    raw: str | dict[str, Any], *, expected_root: str
) -> dict[str, Any]:
    """Normalize only approved model wrappers and harmless top-level labels.

    Nested slide/target data is deliberately untouched so strict Pydantic and
    semantic validation continue to reject malformed or invented content.
    """
    data = extract_json(raw) if isinstance(raw, str) else dict(raw)
    wrappers = {"result", "response", "output", "data"}
    harmless = {"title"}
    if expected_root not in data:
        wrapper_keys = [key for key in wrappers if isinstance(data.get(key), dict)]
        if len(wrapper_keys) == 1 and set(data).issubset({wrapper_keys[0], *harmless}):
            nested = data[wrapper_keys[0]]
            if expected_root in nested:
                data = dict(nested)
    if expected_root in data:
        data = {key: value for key, value in data.items() if key not in harmless}
    if expected_root == "slides":
        data = _normalize_misnested_slide_content(data)
        data = _merge_slide_fragments(data)
    return data


def safe_response_structure(raw: str | dict[str, Any]) -> dict[str, Any]:
    """Describe response keys without retaining or exposing generated content."""
    try: data = extract_json(raw) if isinstance(raw, str) else dict(raw)
    except (ResponseValidationError, TypeError, ValueError): return {"json_object": False, "markdown_fence": isinstance(raw, str) and raw.strip().startswith("```")}
    slides = data.get("slides")
    summary: dict[str, Any] = {"json_object": True, "markdown_fence": isinstance(raw, str) and raw.strip().startswith("```"), "top_level_keys": sorted(data), "slides_type": type(slides).__name__}
    if isinstance(slides, list):
        numbers = [slide.get("slide_number") for slide in slides if isinstance(slide, dict)]
        summary["slide_count"] = len(slides)
        summary["slide_numbers"] = numbers
        summary["duplicate_slide_numbers"] = sorted(number for number, count in Counter(numbers).items() if count > 1 and isinstance(number, int))
        summary["slides"] = [{"slide_number": slide.get("slide_number"), "keys": sorted(slide), "target_count": len(slide.get("targets", [])) if isinstance(slide.get("targets"), list) else None, "target_container": "targets" if "targets" in slide else "placeholders" if "placeholders" in slide else None, "target_keys": sorted({(target.get("target_kind"), target.get("target_id")) for target in slide.get("targets", slide.get("placeholders", [])) if isinstance(target, dict) and ("target_kind" in target or "target_id" in target)}, key=str), "content_types": [target.get("content_type") for target in slide.get("targets", slide.get("placeholders", [])) if isinstance(target, dict)]} for slide in slides if isinstance(slide, dict)]
    return summary


def _allowed(template: dict[str, Any]) -> dict[int, dict[tuple[str, int], dict[str, Any]]]:
    return replaceable_targets_by_slide(template)


_SECTION_LABELS = {
    "problem statement", "objectives", "proposed solution", "methodology",
    "workflow", "novelty", "key findings", "conclusion",
}
_GROUNDING_STOPWORDS = {"about", "after", "again", "also", "and", "are", "for", "from", "into", "that", "the", "their", "this", "with"}


def _bullet_lines(content: str) -> list[str]:
    return [re.sub(r"^(?:[-*\u2022]|\d+[.)])\s*", "", line).strip() for line in content.splitlines() if line.strip()]


def _planned_target_quality(item: TargetContent, metadata: dict[str, Any], section_type: str | None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not section_type:
        return errors, warnings
    role = str(metadata.get("role", "unknown"))
    if role == "title":
        if item.content_type != "title": errors.append("title target requires content_type 'title'")
        elif "\n" in item.content or len(item.content.split()) > 14: warnings.append("title is longer than the preferred concise heading")
    if role == "body" and section_type not in {"title", "qa", "conclusion_qa"}:
        if item.content_type != "bullets":
            errors.append("body target requires content_type 'bullets'")
            return errors, warnings
        bullets = item.bullets or []
        capacity = int(metadata.get("approximate_max_characters", 0) or 0)
        if capacity >= 140 and not 3 <= len(bullets) <= 5:
            warnings.append(f"normal body target prefers 3-5 bullets; received {len(bullets)}")
        token_sets = [set(re.findall(r"[a-z0-9]+", bullet.casefold())) for bullet in bullets]
        if any(left and right and len(left & right) / max(1, len(left | right)) >= .8 for index, left in enumerate(token_sets) for right in token_sets[index + 1:]):
            errors.append("body contains substantially duplicated bullets")
        # Source assignment is authoritative only after slide planning. Legacy
        # direct-template generation has no per-target excerpt metadata.
        if "source_excerpt" not in metadata:
            return errors, warnings
        excerpt = str(metadata.get("source_excerpt", "")).strip()
        if not excerpt:
            errors.append("body target has no assigned factual source excerpt")
            return errors, warnings
        source_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?%?\b", excerpt))
        source_terms = {term[:6] for term in re.findall(r"[a-z][a-z0-9]{3,}", excerpt.casefold()) if term not in _GROUNDING_STOPWORDS}
        for index, bullet in enumerate(bullets, start=1):
            bullet_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?%?\b", bullet))
            if bullet_numbers - source_numbers:
                errors.append(f"bullet {index} contains source-unverified numbers {sorted(bullet_numbers - source_numbers)}")
            bullet_terms = {term[:6] for term in re.findall(r"[a-z][a-z0-9]{3,}", bullet.casefold()) if term not in _GROUNDING_STOPWORDS}
            if source_terms and not (bullet_terms & source_terms):
                errors.append(f"bullet {index} has low grounding confidence for the assigned source excerpt")
    return errors, warnings


def _fill_trusted_layout_names(data: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    copied = dict(data); layouts = {slide["slide_number"]: slide["layout_name"] for slide in template["slides"]}; sections = {slide["slide_number"]: slide.get("section_type") for slide in template["slides"]}; slides = []
    for raw_slide in copied.get("slides", []):
        item = dict(raw_slide) if isinstance(raw_slide, dict) else raw_slide
        if isinstance(item, dict) and "layout_name" not in item and item.get("slide_number") in layouts: item["layout_name"] = layouts[item["slide_number"]]
        if isinstance(item, dict) and item.get("slide_number") in sections and sections[item["slide_number"]]:
            supplied = item.get("section_type")
            if supplied is not None and supplied != sections[item["slide_number"]]:
                raise ResponseValidationError([f"Slide {item['slide_number']} returned section_type '{supplied}'; expected '{sections[item['slide_number']]}'."])
            item["section_type"] = sections[item["slide_number"]]
        slides.append(item)
    if "slides" in copied: copied["slides"] = slides
    return copied


def _normalize_redundant_target_metadata(data: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    """Normalize model-controlled fields only through an authoritative target key."""
    copied = dict(data)
    allowed = _allowed(template)
    slides: list[Any] = []
    for slide_index, raw_slide in enumerate(copied.get("slides", [])):
        if not isinstance(raw_slide, dict):
            slides.append(raw_slide)
            continue
        slide = dict(raw_slide)
        slide_number = slide.get("slide_number")
        expected = allowed.get(slide_number, {})
        used = {
            (target.get("target_kind"), target.get("target_id"))
            for target in slide.get("targets", []) if isinstance(target, dict)
            and (target.get("target_kind"), target.get("target_id")) in expected
        }
        normalized_targets: list[Any] = []
        for target_index, raw_target in enumerate(slide.get("targets", [])):
            if not isinstance(raw_target, dict):
                normalized_targets.append(raw_target)
                continue
            target = dict(raw_target)
            inference_keys = {"target_kind", "content", "content_type", "text", "bullets"}
            if ("target_id" not in target
                    and set(target).issubset(inference_keys)
                    and target.get("target_kind") in {"placeholder", "shape"}):
                candidates = [key for key in expected if key[0] == target["target_kind"] and key not in used]
                if len(candidates) == 1:
                    target["target_id"] = candidates[0][1]
                    used.add(candidates[0])
            metadata = expected.get((target.get("target_kind"), target.get("target_id")))
            output_type = expected_content_type(metadata) if metadata is not None else None
            if output_type is not None:
                # The indexed target role is authoritative. This safely handles
                # aliases, omissions, and unfamiliar model-generated labels.
                target["content_type"] = output_type
                legacy = target.pop("content", None)
                if output_type == "title":
                    if "text" not in target and legacy is not None:
                        target["text"] = str(legacy).strip()
                else:
                    paragraph = target.pop("text", None)
                    if "bullets" not in target and (legacy is not None or paragraph is not None):
                        content = legacy if legacy is not None else paragraph
                        try:
                            target["bullets"] = normalize_bullets(content, role=str(metadata.get("role", "body")))
                        except BulletNormalizationError:
                            target["bullets"] = content
            normalized_targets.append(target)
        if "targets" in slide:
            slide["targets"] = normalized_targets
        slides.append(slide)
    if "slides" in copied:
        copied["slides"] = slides
    return copied


def preserve_valid_targets_during_repair(
    original: dict[str, Any], repaired: SlideChunkResponse, template: dict[str, Any]
) -> SlideChunkResponse:
    """Keep valid original values while accepting repaired targets by stable key."""
    allowed = _allowed(template)
    preserved: dict[tuple[int, str, int], TargetContent] = {}
    counts: Counter[tuple[int, str, int]] = Counter()
    for slide in original.get("slides", []) if isinstance(original, dict) else []:
        if not isinstance(slide, dict) or slide.get("slide_number") not in allowed:
            continue
        number = slide["slide_number"]
        for raw_target in slide.get("targets", []):
            if not isinstance(raw_target, dict) or "target_id" not in raw_target:
                continue
            try:
                normalized = _normalize_redundant_target_metadata(
                    {"slides": [{"slide_number": number, "targets": [raw_target]}]}, template
                )["slides"][0]["targets"][0]
                target = _model_validate_or_response_error(
                    TargetContent, normalized, slide_number=number,
                    target_id=normalized.get("target_id") if isinstance(normalized, dict) else None,
                )
            except (ResponseValidationError, KeyError):
                continue
            key = (number, target.target_kind, target.target_id)
            counts[key] += 1
            metadata = allowed[number].get((target.target_kind, target.target_id))
            if metadata and len(target.content) <= metadata["approximate_max_characters"] * 1.15:
                preserved[key] = target
    preserved = {key: value for key, value in preserved.items() if counts[key] == 1}
    slides = []
    for slide in repaired.slides:
        targets = [preserved.get((slide.slide_number, item.target_kind, item.target_id), item)
                   for item in slide.targets]
        slides.append(slide.model_copy(update={"targets": targets}))
    return repaired.model_copy(update={"slides": slides})


def merge_slide_contents(existing: list[SlideContent], incoming: list[SlideContent]) -> list[SlideContent]:
    """Merge validated slides by number and targets by stable key."""
    merged = {slide.slide_number: slide.model_copy(deep=True) for slide in existing}
    for slide in incoming:
        current = merged.get(slide.slide_number)
        if current is None:
            merged[slide.slide_number] = slide.model_copy(deep=True)
            continue
        if current.layout_name != slide.layout_name:
            raise ResponseValidationError([f"Slide {slide.slide_number} has conflicting layout names during chunk merge."])
        if current.section_type != slide.section_type:
            raise ResponseValidationError([f"Slide {slide.slide_number} has conflicting section types during chunk merge."])
        targets = {(item.target_kind, item.target_id): item for item in current.targets}
        for item in slide.targets:
            key = (item.target_kind, item.target_id)
            prior = targets.get(key)
            if prior is not None and prior.content != item.content:
                raise ResponseValidationError([
                    f"Slide {slide.slide_number}, {item.target_kind} {item.target_id}: conflicting content during chunk merge."
                ])
            if prior is None:
                current.targets.append(item)
                targets[key] = item
    return sorted(merged.values(), key=lambda item: item.slide_number)


def merge_recovered_targets(
    slides: list[SlideContent], recovered: list[Any], requested_keys: set[tuple[int, str, int]]
) -> list[SlideContent]:
    """Replace or add only explicitly requested recovery keys."""
    merged = {slide.slide_number: slide.model_copy(deep=True) for slide in slides}
    recovered_keys: set[tuple[int, str, int]] = set()
    for item in recovered:
        key = (item.slide_number, item.target_kind, item.target_id)
        if key not in requested_keys:
            raise ResponseValidationError([f"Recovery returned unrequested target key {key}."])
        if key in recovered_keys:
            raise ResponseValidationError([f"Recovery returned duplicate target key {key}."])
        recovered_keys.add(key)
        slide = merged.get(item.slide_number)
        if slide is None:
            raise ResponseValidationError([f"Recovery returned unknown slide number {item.slide_number}."])
        replacement = _model_validate_or_response_error(
            TargetContent,
            {"target_kind": item.target_kind, "target_id": item.target_id,
             "content_type": item.content_type, "text": item.text, "bullets": item.bullets},
            slide_number=item.slide_number, target_id=item.target_id,
        )
        positions = [index for index, target in enumerate(slide.targets)
                     if (target.target_kind, target.target_id) == key[1:]]
        if len(positions) > 1:
            raise ResponseValidationError([f"Slide {item.slide_number} already contains duplicate target key {key[1:]}."])
        if positions:
            slide.targets[positions[0]] = replacement
        else:
            slide.targets.append(replacement)
    return sorted(merged.values(), key=lambda item: item.slide_number)


def _validate_semantics(content: PresentationContent, template: dict[str, Any], *, recover_quality: bool = False) -> tuple[PresentationContent, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    allowed = _allowed(template)
    required = required_targets_by_slide(template)
    seen_slides: set[int] = set()
    normalized_contents: list[str] = []
    planned_sections = [slide.get("section_type") for slide in template.get("slides", [])]
    planned_deck = any(planned_sections)
    if planned_deck:
        actual_order = [slide.slide_number for slide in content.slides]
        expected_order = [slide["slide_number"] for slide in template["slides"]]
        if len(content.slides) != len(expected_order): errors.append(f"A planned presentation must contain exactly {len(expected_order)} slides; received {len(content.slides)}.")
        if actual_order != expected_order: errors.append(f"Slides must follow the planned order {expected_order}; received {actual_order}.")
    for slide in content.slides:
        if slide.slide_number not in allowed:
            errors.append(f"Unknown slide number {slide.slide_number}."); continue
        if slide.slide_number in seen_slides: errors.append(f"Duplicate slide number {slide.slide_number}.")
        seen_slides.add(slide.slide_number)
        expected_layout = next(s["layout_name"] for s in template["slides"] if s["slide_number"] == slide.slide_number)
        if slide.layout_name != expected_layout: warnings.append(f"Slide {slide.slide_number} layout label differs from the template.")
        expected_section = next(s.get("section_type") for s in template["slides"] if s["slide_number"] == slide.slide_number)
        if expected_section and slide.section_type != expected_section: errors.append(f"Slide {slide.slide_number} must use section_type '{expected_section}'.")
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
            quality_errors, quality_warnings = _planned_target_quality(item, allowed[slide.slide_number][key], slide.section_type)
            quality_messages = [f"Slide {slide.slide_number}, {label} {item.target_id}: {error}." for error in quality_errors]
            (errors if recover_quality else warnings).extend(quality_messages)
            warnings.extend(f"Slide {slide.slide_number}, {label} {item.target_id}: {warning}." for warning in quality_warnings)
            normalized_contents.append(re.sub(r"\s+", " ", item.content.lower()).strip())
        missing = set(required[slide.slide_number]) - seen_targets
        if missing:
            errors.extend(f"Slide {slide.slide_number}, {kind} {target_id}: required content was not generated." for kind, target_id in sorted(missing))
    missing_slides = set(allowed) - seen_slides
    if missing_slides: errors.append(f"Missing required slides: {sorted(missing_slides)}.")
    repeats = [text for text, count in Counter(normalized_contents).items() if text and count > 1]
    if repeats:
        message = f"Duplicate content detected in {len(repeats)} target value(s)."
        warnings.append(message)
    if errors: raise ResponseValidationError(errors)
    return content, warnings


def validate_response(raw: str | dict[str, Any], template: dict[str, Any], presentation_title: str | None = None) -> tuple[PresentationContent, list[str]]:
    data = normalize_model_response(raw, expected_root="slides")
    data = _fill_trusted_layout_names(data, template)
    data = _normalize_redundant_target_metadata(data, template)
    data = {"presentation_title": presentation_title or template.get("template_name") or "Presentation", **data}
    content = _model_validate_or_response_error(PresentationContent, data)
    return _validate_semantics(content, template)


def validate_chunk_response(raw: str | dict[str, Any], template: dict[str, Any]) -> tuple[SlideChunkResponse, list[str]]:
    chunk = parse_chunk_response(raw, template)
    wrapped = PresentationContent(presentation_title="Chunk", slides=chunk.slides)
    _, warnings = _validate_semantics(wrapped, template, recover_quality=True)
    return chunk, warnings


def parse_chunk_response(raw: str | dict[str, Any], template: dict[str, Any]) -> SlideChunkResponse:
    data = normalize_model_response(raw, expected_root="slides")
    data = _fill_trusted_layout_names(data, template)
    data = _normalize_redundant_target_metadata(data, template)
    chunk = _model_validate_or_response_error(SlideChunkResponse, data)
    return chunk


def missing_required_targets(slides: list[Any], template: dict[str, Any]) -> list[dict[str, Any]]:
    required = required_targets_by_slide(template)
    present = {(slide.slide_number, item.target_kind, item.target_id) for slide in slides for item in slide.targets}
    return [metadata for slide_number, targets in required.items() for (kind, target_id), metadata in targets.items() if (slide_number, kind, target_id) not in present]


def analyze_chunk_targets(chunk: SlideChunkResponse, template: dict[str, Any]) -> TargetAnalysis:
    required = required_targets_by_slide(template); allowed = replaceable_targets_by_slide(template); layouts = {slide["slide_number"]: slide["layout_name"] for slide in template["slides"]}
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
                    errors.append(f"Duplicate {item.target_kind} {item.target_id} returned for slide {number}.")
                    continue
                seen.add(key)
                metadata = allowed[number].get(key)
                if metadata is None:
                    errors.append(f"Unknown or non-required {item.target_kind} {item.target_id} returned for slide {number}.")
                    continue
                limit = metadata["approximate_max_characters"]; length = len(item.content)
                quality_errors, quality_warnings = _planned_target_quality(item, metadata, next((slide.get("section_type") for slide in template["slides"] if slide["slide_number"] == number), None))
                if length > limit * 1.15 or quality_errors: recovery[(number, *key)] = metadata
                else:
                    valid.append(item.model_copy(deep=True))
                    if length > limit: warnings.append(f"Slide {number}, {item.target_kind} {item.target_id} has minor overflow: {length} characters for an approximate {limit}-character capacity.")
                    warnings.extend(f"Slide {number}, {item.target_kind} {item.target_id}: {warning}." for warning in quality_warnings)
        for key, metadata in expected.items():
            if key not in seen: recovery[(number, *key)] = metadata
        section_type = next((slide.get("section_type") for slide in template["slides"] if slide["slide_number"] == number), None)
        analyzed.append(SlideContent(slide_number=number, layout_name=layouts[number], section_type=section_type, targets=valid, speaker_notes=source.speaker_notes if source else None))
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
    data = normalize_model_response(raw, expected_root="targets")

    try:
        response = _model_validate_or_response_error(MissingTargetRecoveryResponse, data)
    except ResponseValidationError:
        raise

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
        data = normalize_model_response(raw, expected_root="targets")
        response = _model_validate_or_response_error(MissingTargetRecoveryResponse, data)
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
        data = normalize_model_response(raw, expected_root="contents")
        response = _model_validate_or_response_error(OrderedTargetRecoveryResponse, data)
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
        if metadata.get("role") == "body":
            try:
                points = normalize_bullets(content, role=str(metadata.get("role", "body")))
            except BulletNormalizationError as exc:
                errors.append(f"Ordered recovery slot {index + 1}: {exc}.")
                unresolved.append(metadata)
                continue
            candidate = RecoveredTargetContent(slide_number=metadata["slide_number"], target_kind=metadata["target_kind"], target_id=metadata["target_id"], content_type="bullets", bullets=points)
        else:
            candidate = RecoveredTargetContent(slide_number=metadata["slide_number"], target_kind=metadata["target_kind"], target_id=metadata["target_id"], content_type="title", text=content)
        quality_errors, _ = _planned_target_quality(candidate, {**metadata, "approximate_max_characters": limit}, metadata.get("section_type"))
        if quality_errors:
            errors.extend(f"Ordered recovery slot {index + 1}: {error}." for error in quality_errors)
            unresolved.append(metadata)
            continue
        recovered.append(candidate)

    if len(response.contents) > len(requested):
        errors.append(
            f"Discarded {len(response.contents) - len(requested)} extra "
            "ordered recovery content value(s)."
        )
    return RecoveryAnalysis(recovered, unresolved, errors)
