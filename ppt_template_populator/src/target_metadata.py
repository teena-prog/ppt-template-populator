from __future__ import annotations

from collections import defaultdict
from typing import Any

ROLE_MIN_CAPACITY = {"title": 60, "subtitle": 100, "short_label": 12, "body": 250, "bullets": 250, "caption": 80, "metric": 20}
SEMANTIC_TARGET_ROLES = {"title", "subtitle", "short_label", "body", "bullets", "metric", "caption", "footer", "slide_number", "decorative", "static", "non_writable"}
NON_REPLACEABLE_ROLES = {"short_label", "metric", "caption", "footer", "slide_number", "decorative", "static", "non_writable"}
REQUIRED_CONTENT_ROLES = {"title", "body", "bullets"}
TITLE_CONTENT_ROLES = {"title", "subtitle"}
BULLET_CONTENT_ROLES = {"body", "bullets"}


def target_capacity_limits(target: dict[str, Any]) -> dict[str, int | float]:
    """Return replacement-state capacity derived from indexed geometry.

    Existing text is intentionally not subtracted because writable target text
    is cleared before population. Static/decorative shapes are never passed to
    this function as writable allocation regions.
    """
    capacity = target.get("capacity") or {}
    maximum_characters = int(
        capacity.get("maximum_characters")
        or target.get("approximate_max_characters") or 0
    )
    maximum_lines = int(capacity.get("maximum_lines") or 0)
    maximum_words = int(capacity.get("maximum_words") or max(0, maximum_characters // 7))
    characters_per_line = int(capacity.get("characters_per_line") or 0)
    if maximum_lines <= 0 and maximum_characters > 0:
        maximum_lines = max(1, maximum_characters // max(24, characters_per_line or 48))
    words_per_bullet = max(4, min(18, maximum_words // max(1, min(5, maximum_lines))))
    requested_bullets = min(5, maximum_lines, maximum_words // max(4, words_per_bullet))
    if maximum_characters >= 32 and maximum_lines >= 1:
        requested_bullets = max(1, requested_bullets)
    return {
        "maximum_characters": maximum_characters,
        "maximum_words": maximum_words,
        "maximum_lines": maximum_lines,
        "characters_per_line": characters_per_line,
        "requested_bullet_count": requested_bullets,
        "maximum_words_per_bullet": words_per_bullet,
        "line_spacing": float(capacity.get("line_spacing") or 1.2),
    }


def usable_body_target(target: dict[str, Any]) -> bool:
    if str(target.get("role")) not in BULLET_CONTENT_ROLES or not target.get("replaceable"):
        return False
    limits = target_capacity_limits(target)
    return bool(
        limits["maximum_characters"] >= 32
        and limits["maximum_words"] >= 4
        and limits["maximum_lines"] >= 1
        and (not limits["characters_per_line"] or limits["characters_per_line"] >= 12)
    )


def expected_content_type(target: dict[str, Any]) -> str | None:
    """Derive output structure from trusted metadata, never model labels."""
    role = str(target.get("role", "unknown")).strip().lower()
    if role in TITLE_CONTENT_ROLES:
        return "title"
    if role in BULLET_CONTENT_ROLES:
        return "bullets"
    return None


def _explicitly_replaceable(target: dict[str, Any]) -> bool:
    name = str(target.get("shape_name", "")).lower().strip()
    return "replaceable" in name or name.startswith("content_") or name.startswith("content ")


def normalized_target(target: dict[str, Any], slide_number: int) -> dict[str, Any]:
    result = dict(target)
    result["slide_number"] = slide_number
    result["approximate_max_characters"] = int(
        result.get("approximate_max_characters", result.get("max_content_length", 0)) or 0
    )
    result["max_content_length"] = result["approximate_max_characters"]
    original_role = str(result.get("role", "non_writable")).lower()
    role = original_role.replace("page number", "slide_number")
    role = {"heading": "title", "content": "body", "card": "body", "label": "short_label", "unknown": "non_writable"}.get(role, role)
    if role not in SEMANTIC_TARGET_ROLES: role = "non_writable"
    result["role"] = role
    if original_role == "card": result["multi_content_region"] = True
    result["replaceable"] = bool(result.get("replaceable", role not in NON_REPLACEABLE_ROLES))
    result["required"] = bool(result.get("required", False))
    if result["replaceable"]:
        result.setdefault("replacement_mode", "replace")
        result.setdefault("clear_before_write", True)
    return result


def normalize_template_targets(template: dict[str, Any]) -> dict[str, Any]:
    """Enrich current and legacy template metadata without mutating the source.

    Required/replaceable classification (in particular "repeated chrome"
    detection, e.g. a logo or footer repeated across many slides) depends on
    seeing every slide in the deck at once. Once a target has been classified
    this way, that verdict is locked in (via _target_classified) so that
    re-running this function against a smaller slide subset -- as pipeline.py
    does per generation chunk -- can never reclassify it differently. Without
    this, a target correctly marked "decorative" against the full deck could
    look required within a 2-4 slide subset (its repeats fall outside the
    subset), and the model would be prompted for content the final,
    full-template validation then rejects as an unknown target.
    """
    normalized = dict(template)
    occurrences: dict[str, set[int]] = defaultdict(set)
    for slide in template.get("slides", []):
        for target in slide.get("targets", []):
            text = " ".join(str(target.get("existing_text", "")).lower().split())
            if text and len(text) <= 80: occurrences[text].add(slide["slide_number"])
    slides = []
    for slide in template.get("slides", []):
        copied = dict(slide)
        targets = [normalized_target(item, slide["slide_number"]) for item in slide.get("targets", [])]
        for item in targets:
            if item.get("_target_classified"): continue
            if item.get("text_category") in {
                "instructional_placeholder", "editable_sample", "sample_content"
            }:
                item["replaceable"] = item["role"] not in NON_REPLACEABLE_ROLES
                item["required"] = bool(item["replaceable"] and item["role"] in REQUIRED_CONTENT_ROLES)
                item["replacement_mode"] = "replace"
                item["clear_before_write"] = True
                item["_target_classified"] = True
                continue
            explicit = _explicitly_replaceable(item)
            text = " ".join(str(item.get("existing_text", "")).lower().split())
            repeated_chrome = bool(text and len(text) <= 80 and len(occurrences[text]) > 1)
            if item["role"] in NON_REPLACEABLE_ROLES or repeated_chrome:
                item["replaceable"] = explicit
                item["required"] = bool(explicit and item.get("required"))
            elif item["target_kind"] == "placeholder":
                item["required"] = bool(item["replaceable"])
            elif item["role"] in REQUIRED_CONTENT_ROLES:
                item["required"] = bool(item["replaceable"])
            else:
                item["required"] = bool(explicit and item.get("required"))
            item["_target_classified"] = True
        copied["targets"] = targets
        slides.append(copied)
    normalized["slides"] = slides
    return normalized


def required_targets_by_slide(template: dict[str, Any]) -> dict[int, dict[tuple[str, int], dict[str, Any]]]:
    normalized = normalize_template_targets(template)
    return {
        slide["slide_number"]: {
            (target["target_kind"], target["target_id"]): target
            for target in slide.get("targets", []) if target["replaceable"] and target["required"]
        }
        for slide in normalized.get("slides", [])
    }


def replaceable_targets_by_slide(template: dict[str, Any]) -> dict[int, dict[tuple[str, int], dict[str, Any]]]:
    """Return every approved writable target, including optional targets."""
    normalized = normalize_template_targets(template)
    return {
        slide["slide_number"]: {
            (target["target_kind"], target["target_id"]): target
            for target in slide.get("targets", []) if target["replaceable"]
        }
        for slide in normalized.get("slides", [])
    }
