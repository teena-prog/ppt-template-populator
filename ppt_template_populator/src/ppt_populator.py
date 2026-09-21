from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
from uuid import uuid4
from hashlib import sha256
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.enum.text import MSO_AUTO_SIZE, PP_ALIGN
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_COLOR_TYPE
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Pt
from lxml import etree
from .response_models import PresentationContent
from .security import confined_path
from typing import Any, Iterable
from .target_metadata import replaceable_targets_by_slide, required_targets_by_slide
from .style_profile import StyleProfile, build_style_profile
from .template_binary import save_presentation_safely

MIN_FONT_SIZE_BY_ROLE = {"title": 28.0, "subtitle": 18.0, "heading": 18.0, "body": 16.0, "caption": 14.0, "unknown": 14.0}

# House typography standard, enforced on every generated target regardless of
# what the source template originally used, so the deck reads consistently
# from slide to slide. "body_sub" applies to sub-bullet paragraphs (outline
# level >= 1) inside a "body" target; every other role is flat across levels.
STANDARD_FONT_NAME = "Aptos"
GENERATED_DARK_TEXT = RGBColor(0x18, 0x35, 0x50)
TYPOGRAPHY_SPEC: dict[str, dict[str, Any]] = {
    "title": {"size": 36.0, "bold": True},
    "subtitle": {"size": 24.0, "bold": True},
    "heading": {"size": 24.0, "bold": True},
    "body": {"size": 20.0, "bold": False},
    "body_sub": {"size": 18.0, "bold": False},
    "caption": {"size": 16.0, "bold": False},
    "reference": {"size": 14.0, "bold": False},
}
# Floor for the minor-overflow shrink below -- close enough to the nominal
# size that text never reads as "too small" for its role, per slide.
MIN_TYPOGRAPHY_SIZE: dict[str, float] = {
    "title": 28.0, "subtitle": 18.0, "heading": 18.0, "body": 16.0, "body_sub": 14.0, "caption": 14.0, "reference": 14.0,
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


def _text_format_snapshot(text_frame: Any) -> tuple[list[Any], Any]:
    paragraphs = list(text_frame.paragraphs)
    paragraph_properties = [deepcopy(p._p.pPr) if p._p.pPr is not None else None for p in paragraphs]
    run_properties = deepcopy(paragraphs[0].runs[0]._r.get_or_add_rPr()) if paragraphs and paragraphs[0].runs else None
    return paragraph_properties, run_properties


def _replace_preserving_format(text_frame, content: str, format_snapshot: tuple[list[Any], Any] | None = None) -> list[Any]:
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
    original_pPr, run_properties = format_snapshot or _text_format_snapshot(text_frame)
    text_frame.clear()
    lines = content.splitlines() or [content]
    written = []
    for line_index, line in enumerate(lines):
        target = text_frame.paragraphs[0] if line_index == 0 else text_frame.add_paragraph()
        source_pPr = original_pPr[min(line_index, len(original_pPr) - 1)] if original_pPr else None
        if source_pPr is not None:
            existing_pPr = target._p.pPr
            if existing_pPr is not None:
                target._p.remove(existing_pPr)
            target._p.insert(0, deepcopy(source_pPr))
        run = target.add_run(); run.text = line
        if run_properties is not None:
            existing = run._r.rPr
            if existing is not None: run._r.remove(existing)
            run._r.insert(0, deepcopy(run_properties))
        written.append(target)
    return written


def _existing_font_name(text_frame: Any) -> str | None:
    for paragraph in text_frame.paragraphs:
        for run in paragraph.runs:
            if run.font.name: return str(run.font.name)
    return None


def _apply_standard_typography(paragraphs: list[Any], typo_role: str, profile: StyleProfile, font_name: str | None) -> None:
    """Force the house font, per-role size/weight, and a single consistent
    line spacing onto every paragraph just written -- overriding whatever
    the source template's shape happened to use -- so hierarchy (title vs.
    heading vs. body vs. sub-bullet vs. caption vs. reference) reads
    identically across every slide in the deck."""
    for paragraph in paragraphs:
        level = paragraph.level or 0
        effective_role = "body_sub" if typo_role == "body" and level >= 1 else typo_role
        spec = TYPOGRAPHY_SPEC[effective_role]
        if effective_role == "title": size = profile.title_size_pt
        elif effective_role in {"body", "body_sub"}: size = profile.body_size_pt * (.9 if effective_role == "body_sub" else 1)
        elif effective_role == "heading": size = min(profile.title_size_pt * .72, 28)
        elif effective_role == "subtitle": size = min(profile.body_size_pt * 1.2, 24)
        else: size = max(14, profile.body_size_pt * .82)
        paragraph.line_spacing = profile.line_spacing
        if effective_role == "title":
            paragraph.alignment = PP_ALIGN.CENTER
        elif effective_role in {"body", "body_sub", "caption", "reference"}:
            paragraph.alignment = PP_ALIGN.LEFT
        for run in paragraph.runs:
            run.font.name = font_name or STANDARD_FONT_NAME
            run.font.size = Pt(size)
            run.font.bold = spec["bold"]
            # OOXML character spacing is expressed in 1/1000 pt; zero is
            # PowerPoint's Normal setting. python-pptx has no public setter.
            run._r.get_or_add_rPr().set("spc", "0")


def _apply_bullet_formatting(paragraphs: list[Any]) -> None:
    for paragraph in paragraphs:
        p_pr = paragraph._p.get_or_add_pPr()
        for child in list(p_pr):
            if child.tag in {qn("a:buNone"), qn("a:buChar"), qn("a:buAutoNum")}:
                p_pr.remove(child)
        bullet = OxmlElement("a:buChar")
        bullet.set("char", "\u2022")
        trailing = {qn("a:tabLst"), qn("a:defRPr"), qn("a:extLst")}
        insertion_index = next((index for index, child in enumerate(p_pr) if child.tag in trailing), len(p_pr))
        p_pr.insert(insertion_index, bullet)


def _rgb_tuple(color: Any) -> tuple[int, int, int] | None:
    try:
        rgb = color.rgb
        if rgb is not None:
            return tuple(int(component) for component in rgb)
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def _theme_colors(presentation: Any) -> dict[int, tuple[int, int, int]]:
    try:
        theme_part = presentation.slide_master.part.part_related_by(RT.THEME)
        root = etree.fromstring(theme_part.blob)
        scheme = root.find(".//{http://schemas.openxmlformats.org/drawingml/2006/main}clrScheme")
        colors: dict[int, tuple[int, int, int]] = {}
        for index, item in enumerate(list(scheme)[:12], start=1):
            node = next(iter(item), None)
            value = node.get("lastClr") if node is not None and node.tag.endswith("sysClr") else node.get("val") if node is not None else None
            if value and len(value) == 6:
                colors[index] = tuple(int(value[offset:offset + 2], 16) for offset in (0, 2, 4))
        return colors
    except (AttributeError, KeyError, TypeError, ValueError, etree.XMLSyntaxError):
        return {}


def _resolved_rgb(color: Any, theme: dict[int, tuple[int, int, int]]) -> tuple[int, int, int] | None:
    explicit = _rgb_tuple(color)
    if explicit is not None: return explicit
    try:
        result = theme.get(int(color.theme_color))
        brightness = float(color.brightness or 0)
    except (AttributeError, TypeError, ValueError):
        return None
    if result is None: return None
    if brightness >= 0:
        return tuple(round(component + (255 - component) * brightness) for component in result)
    return tuple(round(component * (1 + brightness)) for component in result)


def _template_palette(presentation: Any, theme: dict[int, tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    palette: list[tuple[int, int, int]] = []
    for slide in presentation.slides:
        for shape in _walk_shapes(slide.shapes):
            try:
                rgb = _resolved_rgb(shape.fill.fore_color, theme)
                if rgb is not None and rgb not in palette: palette.append(rgb)
            except (AttributeError, TypeError, ValueError):
                pass
            if getattr(shape, "has_text_frame", False):
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        rgb = _resolved_rgb(run.font.color, theme)
                        if rgb is not None and rgb not in palette: palette.append(rgb)
    for default in ((24, 53, 80), (255, 255, 255), (0, 0, 0)):
        if default not in palette: palette.append(default)
    return palette


def _effective_background(shape: Any, slide: Any, theme: dict[int, tuple[int, int, int]]) -> tuple[int, int, int]:
    try:
        rgb = _resolved_rgb(shape.fill.fore_color, theme)
        if rgb is not None: return rgb
    except (AttributeError, TypeError, ValueError):
        pass
    center_x, center_y = shape.left + shape.width / 2, shape.top + shape.height / 2
    behind: list[tuple[int, int, int]] = []
    for candidate in slide.shapes:
        if candidate is shape: break
        if candidate.left <= center_x <= candidate.left + candidate.width and candidate.top <= center_y <= candidate.top + candidate.height:
            try:
                rgb = _resolved_rgb(candidate.fill.fore_color, theme)
                if rgb is not None: behind.append(rgb)
            except (AttributeError, TypeError, ValueError):
                pass
    if behind: return behind[-1]
    for owner in (getattr(slide, "background", None), getattr(slide, "slide_layout", None), getattr(slide, "slide_master", None)):
        if owner is None: continue
        try:
            rgb = _resolved_rgb(owner.fill.fore_color, theme)
            if rgb is not None: return rgb
        except (AttributeError, TypeError, ValueError):
            pass
    return (255, 255, 255)


def _luminance(rgb: tuple[int, int, int]) -> float:
    values = []
    for component in rgb:
        value = component / 255
        values.append(value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4)
    return .2126 * values[0] + .7152 * values[1] + .0722 * values[2]


def _contrast(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    light, dark = sorted((_luminance(left), _luminance(right)), reverse=True)
    return (light + .05) / (dark + .05)


def _ensure_text_contrast(paragraphs: list[Any], shape: Any, slide: Any,
                          palette: list[tuple[int, int, int]],
                          theme: dict[int, tuple[int, int, int]]) -> bool:
    background = _effective_background(shape, slide, theme)
    changed = False
    for paragraph in paragraphs:
        for run in paragraph.runs:
            current = _resolved_rgb(run.font.color, theme)
            size = run.font.size.pt if run.font.size else 18.0
            required = 3.0 if size >= 18 or bool(run.font.bold and size >= 14) else 4.5
            if current is not None and _contrast(current, background) >= required:
                continue
            preferred = (24, 53, 80) if _luminance(background) > .5 else (255, 255, 255)
            candidate = preferred if preferred in palette and _contrast(preferred, background) >= required else max(palette, key=lambda color: _contrast(color, background))
            if _contrast(candidate, background) < required:
                raise PopulationError("The template palette cannot provide readable text contrast for an editable target.")
            run.font.color.rgb = RGBColor(*candidate)
            changed = changed or (current is not None and current != candidate)
    return changed


def _estimated_lines(content: str, capacity: dict[str, Any]) -> int:
    characters_per_line = max(1, int(capacity.get("characters_per_line") or 1))
    return sum(max(1, math.ceil(len(line.strip()) / characters_per_line)) for line in (content.splitlines() or [content]))


def _fit_content_to_capacity(content: str, target: dict[str, Any] | None) -> tuple[str, bool]:
    if not target or not target.get("capacity"):
        return content, False
    capacity = target["capacity"]
    approximate = int(target.get("approximate_max_characters") or 0)
    if approximate and len(content) <= approximate * 1.15:
        return content, False
    maximum_lines = max(1, int(capacity.get("maximum_lines") or 1))
    if _estimated_lines(content, capacity) <= maximum_lines:
        return content, False
    lines = [" ".join(line.split()) for line in content.splitlines() if line.strip()]
    if str(target.get("role")) == "short_label":
        lines = lines[:1]
    while lines and _estimated_lines("\n".join(lines), capacity) > maximum_lines:
        longest = max(range(len(lines)), key=lambda index: len(lines[index].split()))
        words = lines[longest].split()
        if len(words) <= 3:
            if len(lines) > 1: lines.pop()
            else: break
        else:
            lines[longest] = " ".join(words[:-1]).rstrip(" ,;:-")
    fitted = "\n".join(lines)
    if not fitted or _estimated_lines(fitted, capacity) > maximum_lines:
        raise PopulationError(
            f"Content cannot fit target {target.get('target_kind')} {target.get('target_id')} at the minimum readable size."
        )
    return fitted, fitted != content


def _walk_shapes(shapes: Iterable[Any]) -> Iterable[Any]:
    for shape in shapes:
        yield shape
        children = getattr(shape, "shapes", None)
        if children is not None: yield from _walk_shapes(children)


def _picture_digest(shape: Any) -> str | None:
    if shape.shape_type != MSO_SHAPE_TYPE.PICTURE:
        return None
    try:
        return sha256(shape.image.blob).hexdigest()
    except (AttributeError, ValueError):
        return None


def _protected_picture(shape: Any, slide_width: int, slide_height: int) -> bool:
    name = str(getattr(shape, "name", "")).casefold()
    if any(word in name for word in ("logo", "brand", "watermark", "background")):
        return True
    slide_area = max(1, slide_width * slide_height)
    area_ratio = max(0, shape.width) * max(0, shape.height) / slide_area
    near_edge = shape.left <= slide_width * .08 or shape.top <= slide_height * .08
    return area_ratio >= .82 or (area_ratio <= .08 and near_edge)


def _remove_stale_template_photos(presentation: Any, populated_slides: set[int]) -> int:
    """Remove unverified stock photos while retaining design-protected images."""
    pictures = [shape for slide in presentation.slides for shape in _walk_shapes(slide.shapes)
                if _picture_digest(shape)]
    removed = 0
    for slide_number in populated_slides:
        slide = presentation.slides[slide_number - 1]
        for shape in list(_walk_shapes(slide.shapes)):
            digest = _picture_digest(shape)
            if digest and not _protected_picture(shape, presentation.slide_width, presentation.slide_height):
                element = shape._element
                parent = element.getparent()
                if parent is not None:
                    parent.remove(element)
                    removed += 1
    return removed


def _approved_targets(template: dict[str, Any] | None) -> dict[int, dict[tuple[str, int], dict[str, Any]]] | None:
    if template is None: return None
    return replaceable_targets_by_slide(template)


def _replaceable_targets(template: dict[str, Any] | None) -> dict[int, dict[tuple[str, int], dict[str, Any]]]:
    if template is None:
        return {}
    result: dict[int, dict[tuple[str, int], dict[str, Any]]] = {}
    for slide in template.get("slides", []):
        result[slide["slide_number"]] = {
            (target["target_kind"], target["target_id"]): target
            for target in slide.get("targets", [])
            if target.get("replaceable") and target.get("role") not in {"decorative", "footer", "page number", "slide_number", "non_writable"}
        }
    return result


def _clearable_targets(template: dict[str, Any] | None) -> dict[int, dict[tuple[str, int], dict[str, Any]]]:
    if template is None:
        return {}
    return {
        slide["slide_number"]: {
            (target["target_kind"], target["target_id"]): target
            for target in slide.get("targets", [])
            if target.get("clear_before_write") and target.get("text_category") in {
                "replaceable_placeholder", "instructional_placeholder", "editable_sample", "sample_content"
            }
        }
        for slide in template.get("slides", [])
    }


def _clone_slide(presentation: Any, source: Any) -> Any:
    destination = presentation.slides.add_slide(source.slide_layout)
    for shape in list(destination.shapes):
        destination.shapes._spTree.remove(shape._element)
    copied_elements = []
    for shape in source.shapes:
        element = deepcopy(shape._element)
        destination.shapes._spTree.insert_element_before(element, "p:extLst")
        copied_elements.append(element)
    relationship_map: dict[str, str] = {}
    for relationship in source.part.rels.values():
        if relationship.reltype in {RT.SLIDE_LAYOUT, RT.NOTES_SLIDE}:
            continue
        target = relationship.target_ref if relationship.is_external else relationship.target_part
        relationship_map[relationship.rId] = destination.part.rels._add_relationship(
            relationship.reltype, target, relationship.is_external
        )
    relationship_attributes = {qn("r:embed"), qn("r:link"), qn("r:id")}
    for copied in copied_elements:
        for element in copied.iter():
            for attribute in relationship_attributes:
                old_id = element.get(attribute)
                if old_id in relationship_map:
                    element.set(attribute, relationship_map[old_id])
    return destination


def _apply_slide_plan(presentation: Any, template: dict[str, Any] | None) -> None:
    plan = (template or {}).get("slide_plan")
    if not plan:
        return
    originals = list(presentation.slides)
    for entry in plan:
        source_number = int(entry["source_slide_number"])
        if source_number < 1 or source_number > len(originals):
            raise PopulationError(f"Planned source slide {source_number} does not exist.")
        cloned = _clone_slide(presentation, originals[source_number - 1])
        for role, field in (("title", "create_title_target"), ("body", "create_body_target")):
            expected_id = entry.get(field)
            if expected_id is None:
                continue
            if role == "title":
                left, top, width, height = (.06, .06, .88, .16)
            else:
                left, top, width, height = (.08, .27, .84, .62)
            shape = cloned.shapes.add_textbox(
                int(presentation.slide_width * left), int(presentation.slide_height * top),
                int(presentation.slide_width * width), int(presentation.slide_height * height),
            )
            if int(shape.shape_id) != int(expected_id):
                raise PopulationError(
                    f"Generated {role} target ID {shape.shape_id} did not match planned ID {expected_id}."
                )
            frame = shape.text_frame
            frame.margin_left = frame.margin_right = int(presentation.slide_width * .012)
            frame.margin_top = frame.margin_bottom = int(presentation.slide_height * .012)
            frame.word_wrap = True
    for slide in originals:
        # slide_id is resolved by relationship id; removing it leaves the newly
        # cloned ten-slide sequence and retains the original theme/masters.
        for item in list(presentation.slides._sldIdLst):
            if presentation.part.related_part(item.rId) is slide.part:
                presentation.slides._sldIdLst.remove(item)
                break


def _shape_maps(slide: Any) -> tuple[dict[int, Any], dict[int, Any]]:
    placeholders: dict[int, Any] = {}
    ordinary: dict[int, Any] = {}
    for shape in _walk_shapes(slide.shapes):
        if shape.is_placeholder:
            try:
                placeholders[int(shape.placeholder_format.idx)] = shape
            except (AttributeError, ValueError):
                pass
        else:
            ordinary[int(shape.shape_id)] = shape
    return placeholders, ordinary


def _keep_inside_slide(shape: Any, slide_width: int, slide_height: int) -> bool:
    """Correct only invalid out-of-bounds geometry; valid template placement stays intact."""
    original = (shape.left, shape.top, shape.width, shape.height)
    overflow = max(
        0, -shape.left, -shape.top,
        shape.left + shape.width - slide_width,
        shape.top + shape.height - slide_height,
    )
    if overflow <= min(slide_width, slide_height) * .01:
        return False
    shape.left = max(0, min(shape.left, max(0, slide_width - 1)))
    shape.top = max(0, min(shape.top, max(0, slide_height - 1)))
    shape.width = max(1, min(shape.width, slide_width - shape.left))
    shape.height = max(1, min(shape.height, slide_height - shape.top))
    changed = original != (shape.left, shape.top, shape.width, shape.height)
    return changed


def _update_page_numbers(slide: Any, slide_metadata: dict[str, Any], number: int) -> None:
    placeholders, ordinary = _shape_maps(slide)
    for shape_metadata in slide_metadata.get("shapes", []):
        if shape_metadata.get("role") not in {"slide_number", "page number"}:
            continue
        if shape_metadata.get("is_placeholder"):
            target = placeholders.get(shape_metadata.get("placeholder_idx"))
        else:
            target = ordinary.get(shape_metadata.get("shape_id"))
        if target is not None and getattr(target, "has_text_frame", False):
            _replace_preserving_format(target.text_frame, str(number))


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


def _resolve_title_body_collision(written: list[tuple[Any, str]], slide_width: int, slide_height: int) -> int:
    titles = [shape for shape, role in written if role == "title"]
    bodies = [shape for shape, role in written if role == "body"]
    repaired = 0
    gap = int(slide_height * .025)
    for title in titles:
        for body in bodies:
            horizontal = min(title.left + title.width, body.left + body.width) - max(title.left, body.left)
            vertical = min(title.top + title.height, body.top + body.height) - max(title.top, body.top)
            if horizontal <= 0 or vertical <= 0: continue
            new_top = title.top + title.height + gap
            available = slide_height - new_top - int(slide_height * .06)
            if available > int(slide_height * .18):
                body.top = new_top
                body.height = min(body.height, available)
                body.left = max(body.left, int(slide_width * .04))
                body.width = min(body.width, slide_width - body.left - int(slide_width * .04))
                repaired += 1
    return repaired


def populate_presentation(source_path: Path, generated: PresentationContent, output_dir: Path, template: dict[str, Any] | None = None) -> tuple[Path, list[str]]:
    presentation = Presentation(str(source_path))
    _apply_slide_plan(presentation, template)
    style_profile = build_style_profile(template or {})
    theme = _theme_colors(presentation)
    palette = _template_palette(presentation, theme)
    warnings: list[str] = []
    removed_photos = _remove_stale_template_photos(
        presentation, {slide.slide_number for slide in generated.slides}
    )
    if removed_photos:
        warnings.append(f"Removed {removed_photos} isolated template photo(s) from populated slides; repeated branding was retained.")
    mappings: set[tuple[int, str, int]] = set(); approved = _approved_targets(template)
    replaceable = _replaceable_targets(template)
    clearable = _clearable_targets(template)
    for slide_content in generated.slides:
        if slide_content.slide_number > len(presentation.slides): raise PopulationError(f"Slide {slide_content.slide_number} does not exist.")
        slide = presentation.slides[slide_content.slide_number - 1]
        slide_metadata = next((item for item in (template or {}).get("slides", []) if item["slide_number"] == slide_content.slide_number), None)
        if slide_metadata is not None:
            _update_page_numbers(slide, slide_metadata, slide_content.slide_number)
        if slide_content.speaker_notes is not None:
            try:
                slide.notes_slide.notes_text_frame.text = slide_content.speaker_notes
            except (AttributeError, NotImplementedError):
                warnings.append(f"Slide {slide_content.slide_number}: speaker notes could not be written by this python-pptx version.")
        placeholders, ordinary_shapes = _shape_maps(slide)
        written_shapes: list[tuple[Any, str]] = []
        # Clearing and writing are deliberately separate phases. Writability
        # comes from trusted template metadata, never from model-returned IDs.
        clear_keys = set(replaceable.get(slide_content.slide_number, {})) | set(clearable.get(slide_content.slide_number, {}))
        generated_keys = {(item.target_kind, item.target_id) for item in slide_content.targets}
        cleared = 0
        format_snapshots: dict[tuple[str, int], tuple[list[Any], Any]] = {}
        original_font_names: dict[tuple[str, int], str | None] = {}
        for target_key in clear_keys:
            shape = placeholders.get(target_key[1]) if target_key[0] == "placeholder" else ordinary_shapes.get(target_key[1])
            if shape is not None and getattr(shape, "has_text_frame", False) and shape.text.strip():
                original_font_names[target_key] = _existing_font_name(shape.text_frame)
                format_snapshots[target_key] = _text_format_snapshot(shape.text_frame)
                shape.text_frame.clear()
                if target_key not in generated_keys:
                    cleared += 1
        for item in slide_content.targets:
            key = (slide_content.slide_number, item.target_kind, item.target_id)
            if key in mappings: raise PopulationError(f"Duplicate mapping for slide {key[0]}, {key[1]} {key[2]}.")
            mappings.add(key)
            target_metadata = approved.get(slide_content.slide_number, {}).get((item.target_kind, item.target_id)) if approved is not None else None
            if approved is not None and target_metadata is None: raise PopulationError(f"Target {item.target_kind} {item.target_id} on slide {slide_content.slide_number} is not approved for replacement.")
            shape = placeholders.get(item.target_id) if item.target_kind == "placeholder" else ordinary_shapes.get(item.target_id)
            if shape is None: raise PopulationError(f"{item.target_kind.title()} {item.target_id} is missing on slide {slide_content.slide_number}.")
            if not shape.has_text_frame: raise PopulationError(f"{item.target_kind.title()} {item.target_id} on slide {slide_content.slide_number} is not text-capable.")
            if _keep_inside_slide(shape, presentation.slide_width, presentation.slide_height):
                warnings.append(f"Slide {slide_content.slide_number}, {item.target_kind} {item.target_id} was fitted inside the slide boundary.")
            text_frame = shape.text_frame
            font_name = ((target_metadata or {}).get("font_name")
                         or original_font_names.get((item.target_kind, item.target_id))
                         or _existing_font_name(text_frame))
            fitted_content, shortened = _fit_content_to_capacity(item.content, target_metadata)
            if shortened:
                warnings.append(f"Slide {slide_content.slide_number}, {item.target_kind} {item.target_id} content was shortened at word boundaries to fit its text region.")
            written_paragraphs = _replace_preserving_format(
                text_frame, fitted_content, format_snapshots.get((item.target_kind, item.target_id))
            )
            typo_role = _typography_role(target_metadata)
            _apply_standard_typography(written_paragraphs, typo_role, style_profile, font_name)
            if _ensure_text_contrast(written_paragraphs, shape, slide, palette, theme):
                warnings.append(f"Slide {slide_content.slide_number}, {item.target_kind} {item.target_id} text colour was adjusted using the template palette for readable contrast.")
            if item.content_type == "bullets":
                if not isinstance(item.bullets, list) or not all(isinstance(bullet, str) for bullet in item.bullets):
                    raise PopulationError("Bullet targets must contain a validated list of strings.")
                _apply_bullet_formatting(written_paragraphs)
            written_shapes.append((shape, typo_role))
            # Disable PowerPoint's own dynamic shrink-to-fit: it would silently
            # rescale text per-shape at render time, undoing the fixed,
            # consistent sizes just applied above. Wrapping stays on so long
            # words/lines break within the shape instead of spilling out.
            text_frame.word_wrap = True
            text_frame.auto_size = MSO_AUTO_SIZE.NONE
            if target_metadata and _reduce_font_for_minor_overflow(written_paragraphs, len(fitted_content), target_metadata, typo_role):
                warnings.append(f"Slide {slide_content.slide_number}, {item.target_kind} {item.target_id} font size was reduced within the configured minimum for minor overflow.")
            if len(item.content) > 500: warnings.append(f"Slide {slide_content.slide_number}, {item.target_kind} {item.target_id} may overflow.")
        if cleared:
            warnings.append(f"Slide {slide_content.slide_number}: cleared {cleared} unused editable template text box(es).")
        repaired_collisions = _resolve_title_body_collision(written_shapes, presentation.slide_width, presentation.slide_height)
        if repaired_collisions:
            warnings.append(f"Slide {slide_content.slide_number}: repaired {repaired_collisions} title/body collision(s) using proportional spacing.")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = confined_path(output_dir, f"presentation_{uuid4().hex}.pptx")
    save_presentation_safely(presentation, output_path)
    return output_path, warnings
