from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4
from pptx import Presentation
from .security import validate_pptx
from .target_metadata import ROLE_MIN_CAPACITY, normalize_template_targets

EMU_PER_INCH = 914400
EXCLUDED_PLACEHOLDER_TYPES = {"DATE", "FOOTER", "SLIDE_NUMBER"}
DECORATIVE_NAME_HINTS = ("logo", "icon", "decoration", "decorative", "background", "ornament")


def _placeholder_type(shape: Any) -> str | None:
    if not getattr(shape, "is_placeholder", False): return None
    try:
        value = shape.placeholder_format.type
        return getattr(value, "name", str(value))
    except (AttributeError, ValueError): return "UNKNOWN"


def _font_size(shape: Any) -> float | None:
    if not getattr(shape, "has_text_frame", False): return None
    sizes = []
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            if run.font.size is not None: sizes.append(float(run.font.size.pt))
    return max(sizes) if sizes else None


def _max_content_length(shape: Any, role: str = "unknown", font_size: float | None = None, existing_text: str = "") -> int:
    width = max(float(shape.width) / EMU_PER_INCH, .25)
    height = max(float(shape.height) / EMU_PER_INCH, .15)
    margins = getattr(shape, "margins", {})
    width = max(.2, width - (margins.get("left", 0) + margins.get("right", 0)) / EMU_PER_INCH)
    height = max(.12, height - (margins.get("top", 0) + margins.get("bottom", 0)) / EMU_PER_INCH)
    points = font_size or 18
    chars_per_line = max(4, width * 72 / max(points * .52, 1))
    lines = max(1, height * 72 / max(points * 1.2, 1), existing_text.count("\n") + 1)
    return max(ROLE_MIN_CAPACITY.get(role, 30), min(3000, int(chars_per_line * lines * .9)))


def _margins(shape: Any) -> dict[str, int]:
    frame = getattr(shape, "text_frame", None)
    return {name: int(getattr(frame, f"margin_{name}", 0) or 0) for name in ("left", "right", "top", "bottom")} if frame else {}


def _walk_shapes(shapes: Iterable[Any], group_path: tuple[int, ...] = ()) -> Iterable[tuple[Any, tuple[int, ...]]]:
    for shape in shapes:
        path = (*group_path, int(shape.shape_id))
        yield shape, path
        children = getattr(shape, "shapes", None)
        if children is not None: yield from _walk_shapes(children, path)


def _explicitly_replaceable(shape: dict[str, Any]) -> bool:
    name = shape["shape_name"].lower()
    return "replaceable" in name or name.startswith("content_") or name.startswith("content ")


def _classify(shape: dict[str, Any], slide_width: int, slide_height: int, largest_font: float) -> str:
    text = shape["existing_text"].strip(); placeholder_type = (shape["placeholder_type"] or "").upper()
    if placeholder_type in {"TITLE", "CENTER_TITLE", "VERTICAL_TITLE"}: return "title"
    if placeholder_type in {"SUBTITLE"}: return "subtitle"
    if placeholder_type in {"BODY", "OBJECT", "VERTICAL_BODY"}: return "body"
    if placeholder_type == "SLIDE_NUMBER": return "page number"
    if placeholder_type in {"DATE", "FOOTER"}: return "footer"
    name = shape["shape_name"].lower()
    if any(hint in name for hint in DECORATIVE_NAME_HINTS): return "decorative"
    if re.fullmatch(r"\s*(?:\d+|\d+\s*/\s*\d+|page\s+\d+)\s*", text, re.IGNORECASE): return "page number"
    top = shape["position"]["top"]; bottom = top + shape["position"]["height"]
    font = shape["font_size"] or 0
    if bottom >= slide_height * .92 and font <= 14: return "footer"
    if font >= max(28, largest_font * .9) and top <= slide_height * .45: return "title"
    if font >= 22 and len(text) <= 120: return "heading"
    if font >= 16 and top <= slide_height * .55 and len(text) <= 160: return "subtitle"
    if font and font <= 12 and (shape["position"]["width"] <= slide_width * .55 or len(text) <= 100): return "caption"
    if len(text) >= 20 or shape["position"]["height"] >= slide_height * .12: return "body"
    return "unknown"


def _writable(shape: dict[str, Any]) -> bool:
    if not shape["has_text_frame"]: return False
    explicit = _explicitly_replaceable(shape)
    if shape["role"] in {"decorative", "footer", "page number"} and not explicit: return False
    if shape["is_placeholder"]:
        return shape["placeholder_idx"] is not None and ((shape["placeholder_type"] or "").upper() not in EXCLUDED_PLACEHOLDER_TYPES or explicit)
    text = shape["existing_text"].strip()
    return bool(text and re.search(r"[A-Za-z0-9]", text))


def _classification_confidence(shape: dict[str, Any]) -> float:
    if shape["is_placeholder"] or shape["role"] in {"title", "body", "footer", "page number", "decorative"}: return .95
    if shape["role"] in {"subtitle", "heading", "caption"}: return .75
    return .35


def parse_template(pptx_path: Path, *, file_path: str, template_name: str | None = None, description: str = "", category: str = "General", template_id: str | None = None, max_bytes: int = 25 * 1024 * 1024) -> dict[str, Any]:
    validate_pptx(pptx_path, max_bytes); presentation = Presentation(str(pptx_path)); slides = []
    for number, slide in enumerate(presentation.slides, start=1):
        shapes = []
        for shape, group_path in _walk_shapes(slide.shapes):
            placeholder_idx = None
            if shape.is_placeholder:
                try: placeholder_idx = int(shape.placeholder_format.idx)
                except (AttributeError, ValueError): pass
            has_text = bool(getattr(shape, "has_text_frame", False)); font_size = _font_size(shape)
            shapes.append({"slide_number": number, "shape_id": int(shape.shape_id), "shape_name": shape.name, "group_path": list(group_path[:-1]), "is_placeholder": bool(shape.is_placeholder), "placeholder_idx": placeholder_idx, "placeholder_type": _placeholder_type(shape), "existing_text": shape.text if has_text else "", "has_text_frame": has_text, "font_size": font_size, "max_content_length": 0, "text_box_margins": _margins(shape), "position": {"left": int(shape.left), "top": int(shape.top), "width": int(shape.width), "height": int(shape.height)}})
        largest_font = max((item["font_size"] or 0 for item in shapes), default=0)
        targets = []
        for item in shapes:
            item["role"] = _classify(item, int(presentation.slide_width), int(presentation.slide_height), largest_font)
            item["classification_confidence"] = _classification_confidence(item)
            item["max_content_length"] = _max_content_length(SimpleShape(item), item["role"], item["font_size"], item["existing_text"]) if item["has_text_frame"] else 0
            item["is_writable_target"] = _writable(item)
            if item["is_writable_target"]:
                targets.append({"slide_number": number, "target_kind": "placeholder" if item["is_placeholder"] else "shape", "target_id": item["placeholder_idx"] if item["is_placeholder"] else item["shape_id"], "role": item["role"], "existing_text": item["existing_text"], "approximate_max_characters": item["max_content_length"], "max_content_length": item["max_content_length"], "required": bool(item["is_placeholder"]), "replaceable": True, "classification_confidence": item["classification_confidence"], "shape_name": item["shape_name"], "font_size": item["font_size"], "position": item["position"]})
        placeholder_types = {(item["placeholder_type"] or "").upper() for item in shapes}
        slides.append({"slide_number": number, "layout_name": slide.slide_layout.name or "Unnamed layout", "has_image_placeholders": bool(placeholder_types & {"BITMAP", "PICTURE", "MEDIA_CLIP"}), "has_chart_placeholders": bool(placeholder_types & {"CHART", "OBJECT"}), "shapes": shapes, "targets": targets})
    usable_placeholders = sum(target["target_kind"] == "placeholder" for slide in slides for target in slide["targets"])
    usable_shapes = sum(target["target_kind"] == "shape" for slide in slides for target in slide["targets"])
    slides_without = sum(not slide["targets"] for slide in slides); now = datetime.now(timezone.utc).isoformat()
    return normalize_template_targets({"template_id": template_id or uuid4().hex, "template_name": (template_name or pptx_path.stem).strip(), "description": description.strip(), "category": category.strip() or "General", "file_path": file_path, "slide_count": len(slides), "slides": slides, "has_image_placeholders": any(slide["has_image_placeholders"] for slide in slides), "has_chart_placeholders": any(slide["has_chart_placeholders"] for slide in slides), "usable_placeholder_count": usable_placeholders, "usable_shape_target_count": usable_shapes, "slides_without_writable_targets": slides_without, "safe_for_automatic_population": bool(usable_placeholders + usable_shapes) and slides_without == 0, "created_at": now, "updated_at": now})


class SimpleShape:
    def __init__(self, metadata: dict[str, Any]):
        self.width = metadata["position"]["width"]
        self.height = metadata["position"]["height"]
        self.margins = metadata.get("text_box_margins", {})
