from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any


@dataclass(frozen=True)
class StyleProfile:
    aspect_ratio: float
    horizontal_margin_ratio: float
    vertical_margin_ratio: float
    title_body_size_ratio: float
    title_size_pt: float
    body_size_pt: float
    minimum_body_size_pt: float
    card_gap_ratio: float
    footer_top_ratio: float
    max_columns: int
    body_alignment: str = "left"
    line_spacing: float = 1.08


def _median(values: list[float], fallback: float) -> float:
    return float(median(values)) if values else fallback


def build_style_profile(template: dict[str, Any]) -> StyleProfile:
    """Infer a proportional style system from any parsed PPTX template."""
    width = float(template.get("slide_width") or 16)
    height = float(template.get("slide_height") or 9)
    targets = [target for slide in template.get("slides", []) for target in slide.get("targets", [])]
    title_sizes = [float(target["font_size"]) for target in targets if target.get("role") in {"title", "heading"} and target.get("font_size")]
    body_sizes = [float(target["font_size"]) for target in targets if target.get("role") in {"body", "subtitle", "caption"} and target.get("font_size")]
    title_size = min(44.0, max(28.0, _median(title_sizes, 34.0)))
    body_size = min(24.0, max(16.0, _median(body_sizes, 18.0)))
    left_edges = [float((target.get("position") or {}).get("left", 0)) / width for target in targets if (target.get("position") or {}).get("left", 0) > 0]
    margin = min(.08, max(.035, _median(left_edges, .045))) if width else .045
    return StyleProfile(
        aspect_ratio=width / height if height else 16 / 9,
        horizontal_margin_ratio=margin,
        vertical_margin_ratio=.055,
        title_body_size_ratio=title_size / body_size,
        title_size_pt=title_size,
        body_size_pt=body_size,
        minimum_body_size_pt=14.0,
        card_gap_ratio=.018,
        footer_top_ratio=.94,
        max_columns=5,
    )

