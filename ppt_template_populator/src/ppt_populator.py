from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from uuid import uuid4
from pptx import Presentation
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.util import Pt
from .response_models import PresentationContent
from .security import confined_path
from typing import Any, Iterable
from .target_metadata import required_targets_by_slide

MIN_FONT_SIZE_BY_ROLE = {"title": 20.0, "subtitle": 14.0, "heading": 16.0, "body": 10.0, "caption": 8.0, "unknown": 9.0}

# House typography standard, enforced on every generated target regardless of
# what the source template originally used, so the deck reads consistently
# from slide to slide. "body_sub" applies to sub-bullet paragraphs (outline
# level >= 1) inside a "body" target; every other role is flat across levels.
STANDARD_FONT_NAME = "Aptos"
TYPOGRAPHY_SPEC: dict[str, dict[str, Any]] = {
    "title": {"size": 30.0, "bold": True},
    "subtitle": {"size": 24.0, "bold": True},
    "heading": {"size": 24.0, "bold": True},
    "body": {"size": 20.0, "bold": False},
    "body_sub": {"size": 18.0, "bold": False},
    "caption": {"size": 16.0, "bold": False},
    "reference": {"size": 13.0, "bold": False},
}
# Floor for the minor-overflow shrink below -- close enough to the nominal
# size that text never reads as "too small" for its role, per slide.
MIN_TYPOGRAPHY_SIZE: dict[str, float] = {
    "title": 24.0, "subtitle": 19.0, "heading": 19.0, "body": 15.0, "body_sub": 14.0, "caption": 12.0, "reference": 11.0,
}
_REFERENCE_KEYWORDS = ("reference", "citation", "bibliography", "footnote", "endnote")


class PopulationError(ValueError): pass


def _typography_role(target: dict[str, Any] | None) -> str:
    """Map a target's semantic role (+ light heuristics) to a typography key."""
    if not target: return "body"
    role = str(target.get("role", "unknown")).lower()
    if role not in TYPOGRAPHY_SPEC:
        role = "body"
    if role == "caption":
        haystack = f"{target.get('shape_name', '')} {target.get('existing_text', '')}".lower()
        if any(keyword in haystack for keyword in _REFERENCE_KEYWORDS):
            return "reference"
    return role


def _replace_preserving_format(text_frame, content: str) -> list[Any]:
    """Replace text_frame's content with `content` (one paragraph per line).

    Each original paragraph's bullet/indentation XML (pPr) is preserved
    positionally -- line N of the new content reuses paragraph N's pPr when
    one existed, else repeats the last known one -- so outline hierarchy
    (and therefore which lines are "sub-bullets") survives even though the
    number of lines can differ from the original placeholder text. Returns
    the list of newly written paragraphs so the caller can apply typography.
    """
    paragraphs = list(text_frame.paragraphs)
    if not paragraphs:
        text_frame.text = content
        return list(text_frame.paragraphs)
    original_pPr = [deepcopy(p._p.pPr) if p._p.pPr is not None else None for p in paragraphs]
    run_properties = deepcopy(paragraphs[0].runs[0]._r.get_or_add_rPr()) if paragraphs[0].runs else None
    text_frame.clear()
    lines = content.splitlines() or [content]
    written = []
    for line_index, line in enumerate(lines):
        target = text_frame.paragraphs[0] if line_index == 0 else text_frame.add_paragraph()
        source_pPr = original_pPr[min(line_index, len(original_pPr) - 1)] if original_pPr else None
        if source_pPr is not None:
            target._p.insert(0, deepcopy(source_pPr))
        run = target.add_run(); run.text = line
        if run_properties is not None:
            existing = run._r.rPr
            if existing is not None: run._r.remove(existing)
            run._r.insert(0, deepcopy(run_properties))
        written.append(target)
    return written


def _apply_standard_typography(paragraphs: list[Any], typo_role: str) -> None:
    """Force the house font, per-role size/weight, and a single consistent
    line spacing onto every paragraph just written -- overriding whatever
    the source template's shape happened to use -- so hierarchy (title vs.
    heading vs. body vs. sub-bullet vs. caption vs. reference) reads
    identically across every slide in the deck."""
    for paragraph in paragraphs:
        level = paragraph.level or 0
        effective_role = "body_sub" if typo_role == "body" and level >= 1 else typo_role
        spec = TYPOGRAPHY_SPEC[effective_role]
        paragraph.line_spacing = 1.0
        for run in paragraph.runs:
            run.font.name = STANDARD_FONT_NAME
            run.font.size = Pt(spec["size"])
            run.font.bold = spec["bold"]


def _walk_shapes(shapes: Iterable[Any]) -> Iterable[Any]:
    for shape in shapes:
        yield shape
        children = getattr(shape, "shapes", None)
        if children is not None: yield from _walk_shapes(children)


def _approved_targets(template: dict[str, Any] | None) -> dict[int, dict[tuple[str, int], dict[str, Any]]] | None:
    if template is None: return None
    return required_targets_by_slide(template)


def _reduce_font_for_minor_overflow(paragraphs: list[Any], content_length: int, target: dict[str, Any], typo_role: str) -> bool:
    limit = int(target.get("approximate_max_characters", target.get("max_content_length", 0)) or 0)
    if not limit or content_length <= limit or content_length > limit * 1.15: return False
    minimum = MIN_TYPOGRAPHY_SIZE.get(typo_role, MIN_TYPOGRAPHY_SIZE["body"])
    changed = False
    factor = max(.87, limit / content_length)
    for paragraph in paragraphs:
        for run in paragraph.runs:
            if run.font.size is not None:
                reduced = max(minimum, run.font.size.pt * factor)
                if reduced < run.font.size.pt:
                    run.font.size = Pt(reduced); changed = True
    return changed


def populate_presentation(source_path: Path, generated: PresentationContent, output_dir: Path, template: dict[str, Any] | None = None) -> tuple[Path, list[str]]:
    presentation = Presentation(str(source_path))
    warnings: list[str] = []
    mappings: set[tuple[int, str, int]] = set(); approved = _approved_targets(template)
    for slide_content in generated.slides:
        if slide_content.slide_number > len(presentation.slides): raise PopulationError(f"Slide {slide_content.slide_number} does not exist.")
        slide = presentation.slides[slide_content.slide_number - 1]
        placeholders = {}; ordinary_shapes = {}
        for shape in _walk_shapes(slide.shapes):
            if shape.is_placeholder:
                try: placeholders[int(shape.placeholder_format.idx)] = shape
                except (AttributeError, ValueError): pass
            else: ordinary_shapes[int(shape.shape_id)] = shape
        for item in slide_content.targets:
            key = (slide_content.slide_number, item.target_kind, item.target_id)
            if key in mappings: raise PopulationError(f"Duplicate mapping for slide {key[0]}, {key[1]} {key[2]}.")
            mappings.add(key)
            target_metadata = approved.get(slide_content.slide_number, {}).get((item.target_kind, item.target_id)) if approved is not None else None
            if approved is not None and target_metadata is None: raise PopulationError(f"Target {item.target_kind} {item.target_id} on slide {slide_content.slide_number} is not approved for replacement.")
            shape = placeholders.get(item.target_id) if item.target_kind == "placeholder" else ordinary_shapes.get(item.target_id)
            if shape is None: raise PopulationError(f"{item.target_kind.title()} {item.target_id} is missing on slide {slide_content.slide_number}.")
            if not shape.has_text_frame: raise PopulationError(f"{item.target_kind.title()} {item.target_id} on slide {slide_content.slide_number} is not text-capable.")
            text_frame = shape.text_frame
            written_paragraphs = _replace_preserving_format(text_frame, item.content)
            typo_role = _typography_role(target_metadata)
            _apply_standard_typography(written_paragraphs, typo_role)
            # Disable PowerPoint's own dynamic shrink-to-fit: it would silently
            # rescale text per-shape at render time, undoing the fixed,
            # consistent sizes just applied above. Wrapping stays on so long
            # words/lines break within the shape instead of spilling out.
            text_frame.word_wrap = True
            text_frame.auto_size = MSO_AUTO_SIZE.NONE
            if target_metadata and _reduce_font_for_minor_overflow(written_paragraphs, len(item.content), target_metadata, typo_role):
                warnings.append(f"Slide {slide_content.slide_number}, {item.target_kind} {item.target_id} font size was reduced within the configured minimum for minor overflow.")
            if len(item.content) > 500: warnings.append(f"Slide {slide_content.slide_number}, {item.target_kind} {item.target_id} may overflow.")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = confined_path(output_dir, f"presentation_{uuid4().hex}.pptx")
    presentation.save(str(output_path))
    return output_path, warnings
