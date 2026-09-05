from __future__ import annotations

from collections import defaultdict
from typing import Any

ROLE_MIN_CAPACITY = {"title": 60, "subtitle": 100, "heading": 50, "body": 250, "caption": 80}
NON_REPLACEABLE_ROLES = {"decorative", "footer", "page number"}
REQUIRED_CONTENT_ROLES = {"title", "subtitle", "heading", "body"}


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
    role = str(result.get("role", "unknown")).lower()
    result["role"] = role
    result["replaceable"] = bool(result.get("replaceable", role not in NON_REPLACEABLE_ROLES))
    result["required"] = bool(result.get("required", False))
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
