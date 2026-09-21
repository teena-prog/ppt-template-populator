from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any

from .target_metadata import normalize_template_targets, required_targets_by_slide, usable_body_target


TARGET_SLIDE_COUNT = 10
SLIDE_BLUEPRINT = (
    ("title", "Title", "Use the document topic as a concise presentation title."),
    ("introduction", "Introduction", "Introduce the document's subject and scope."),
    ("problem_statement", "Problem Statement", "Explain only the problem established by the source."),
    ("objectives", "Objectives", "Present only the objectives stated or supported by the source."),
    ("proposed_solution", "Proposed Solution", "Explain only the proposed solution."),
    ("methodology_workflow", "Methodology / Workflow", "Describe only the methodology or workflow."),
    ("case_study", "Example / Case Study", "Present a grounded example or case; do not invent one."),
    ("novelty_key_findings", "Novelty / Key Findings", "Summarize only novelty or key findings in the source."),
    ("conclusion", "Conclusion", "Synthesize the document's supported conclusions."),
    ("closing", "Thank You", "Use only a minimal Thank You / Q&A closing."),
)

FULL_SLIDE_BLUEPRINT = (
    ("title", "Title", "Use the document topic as a concise presentation title."),
    ("introduction", "Introduction", "Explain the subject, context, importance, and presentation scope in 3-5 grounded points."),
    ("problem_statement", "Problem Statement", "Explain only the problem established by the source."),
    ("research_gap_overview", "Research Gap / Topic Overview", "Explain the grounded gap or topic context."),
    ("objectives", "Objectives", "Present only objectives supported by the source."),
    ("dataset_inputs", "Dataset / Input Information", "Describe available data, inputs, or evidence."),
    ("methodology_workflow", "Processing / Methodology", "Describe only processing steps or methodology."),
    ("proposed_solution", "Proposed Solution / Framework", "Explain only the proposed solution or framework."),
    ("analysis_case_study", "Analysis / Case Study", "Present grounded analysis or a source-provided case."),
    ("results_key_findings", "Results / Key Findings", "Present only results or findings in the source."),
    ("quality_limitations", "Quality / Limitations", "State source-supported quality constraints or limitations."),
    ("contribution_novelty", "Contribution / Novelty", "State only the source-supported contribution or novelty."),
    ("conclusion", "Conclusion", "Synthesize only conclusions supported by the source."),
    ("closing", "Thank You", "Use only a minimal Thank You / Q&A closing."),
)


class SlidePlanningError(ValueError):
    pass


@dataclass(frozen=True)
class SlidePlanEntry:
    slide_number: int
    source_slide_number: int
    role: str
    section_title: str
    instruction: str
    source_excerpt: str
    create_title_target: int | None = None
    create_body_target: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "slide_number": self.slide_number,
            "source_slide_number": self.source_slide_number,
            "role": self.role,
            "section_title": self.section_title,
            "instruction": self.instruction,
            "source_excerpt": self.source_excerpt,
            "create_title_target": self.create_title_target,
            "create_body_target": self.create_body_target,
        }


@dataclass(frozen=True)
class SlideAssignment:
    template_slide_number: int
    section_type: str
    title_target: dict[str, Any]
    subtitle_target: dict[str, Any] | None
    primary_body_target: dict[str, Any] | None
    optional_body_targets: tuple[dict[str, Any], ...]
    replaceable_targets: tuple[dict[str, Any], ...]
    static_targets: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "template_slide_number": self.template_slide_number,
            "section_type": self.section_type,
            "title_target": self.title_target,
            "subtitle_target": self.subtitle_target,
            "primary_body_target": self.primary_body_target,
            "optional_body_targets": list(self.optional_body_targets),
            "replaceable_targets": list(self.replaceable_targets),
            "static_targets": list(self.static_targets),
        }


def _target_ref(target: dict[str, Any] | None) -> dict[str, Any] | None:
    if target is None:
        return None
    return {
        "target_kind": target["target_kind"], "target_id": int(target["target_id"]),
        "role": target.get("role", "unknown"),
        "approximate_max_characters": int(target.get("approximate_max_characters") or 0),
    }


def _source_units(text: str) -> list[str]:
    text = re.sub(r"(?im)^\[SOURCE\s+\d+:\s*[^\]]+\]\s*$", "", text)
    units = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z0-9])", text)]
    return [unit for unit in units if unit]


def _distribute_source(text: str, bucket_count: int = 8) -> list[str]:
    """Distribute every source unit across the eight factual slide roles."""
    units = _source_units(text)
    buckets: list[list[str]] = [[] for _ in range(bucket_count)]
    if not units:
        return [""] * bucket_count
    for index, unit in enumerate(units):
        bucket = min(bucket_count - 1, index * bucket_count // len(units))
        buckets[bucket].append(unit)
    if len(units) < len(buckets):
        # A short document still grounds every factual slide in the complete
        # source instead of leaving later slide prompts without evidence.
        for bucket in buckets:
            if not bucket:
                bucket.extend(units)
    return ["\n".join(bucket) for bucket in buckets]


def build_factual_summary(text: str, maximum_units: int = 120) -> str:
    """Build a deterministic extractive summary; no new claims are introduced."""
    units = _source_units(text)
    if len(units) <= maximum_units:
        return "\n".join(units)
    indexes = sorted({round(index * (len(units) - 1) / (maximum_units - 1)) for index in range(maximum_units)})
    return "\n".join(units[index] for index in indexes)


def _overlap_count(targets: list[dict[str, Any]]) -> int:
    count = 0
    for index, left in enumerate(targets):
        a = left.get("position") or {}
        for right in targets[index + 1:]:
            b = right.get("position") or {}
            overlap_width = min(a.get("left", 0) + a.get("width", 0), b.get("left", 0) + b.get("width", 0)) - max(a.get("left", 0), b.get("left", 0))
            overlap_height = min(a.get("top", 0) + a.get("height", 0), b.get("top", 0) + b.get("height", 0)) - max(a.get("top", 0), b.get("top", 0))
            if overlap_width > 0 and overlap_height > 0:
                count += 1
    return count


def _slide_score(slide: dict[str, Any], role: str, required_count: int) -> tuple[int, int, int]:
    writable = [target for target in slide.get("targets", []) if target.get("replaceable", True)]
    target_roles = {str(target.get("role", "unknown")) for target in writable}
    has_title = bool(target_roles & {"title", "heading"})
    has_body = bool(target_roles & {"body", "subtitle"})
    if role == "title":
        suitability = 4 * has_title + 2 * ("subtitle" in target_roles)
    elif role in {"qa", "closing"}:
        suitability = 4 * has_title + has_body
    else:
        suitability = 3 * has_title + 4 * has_body
    return suitability, -_overlap_count(writable), required_count

def _section_matches(slide: dict[str, Any], planned_role: str) -> bool:
    actual = str(slide.get("section_type", "content"))
    aliases = {
        "title": {"title"}, "introduction": {"introduction"},
        "problem_statement": {"problem_statement"}, "objectives": {"objectives"},
        "proposed_solution": {"proposed_solution"},
        "methodology_workflow": {"methodology_workflow"},
        "analysis_case_study": {"case_study", "findings_evidence"},
        "results_key_findings": {"findings_evidence"},
        "quality_limitations": {"risks"}, "validation_advantages": {"findings_evidence"},
        "contribution_novelty": {"findings_evidence", "content"},
        "research_gap_overview": {"problem_statement", "content"},
        "dataset_inputs": {"findings_evidence", "content"},
        "conclusion": {"conclusion"}, "closing": {"thank_you"},
    }
    return actual in aliases.get(planned_role, {planned_role})


def _target_pair(slide: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    writable = [target for target in slide.get("targets", []) if target.get("replaceable")]
    title_rank = {"title": 4, "heading": 3, "subtitle": 2, "unknown": 1, "body": 0}
    title = max(
        writable,
        key=lambda target: (
            title_rank.get(str(target.get("role")), -1),
            float(target.get("font_size") or 0),
            -int((target.get("position") or {}).get("top", 0)),
        ),
        default=None,
    )
    remaining = [
        target for target in writable
        if target is not title and target.get("role") in {"body", "bullets"}
        and usable_body_target(target)
    ]
    body_rank = {"body": 4, "bullets": 4}
    body = max(
        remaining,
        key=lambda target: (
            body_rank.get(str(target.get("role")), -1),
            int((target.get("position") or {}).get("width", 0)) * int((target.get("position") or {}).get("height", 0)),
            int((target.get("position") or {}).get("top", 0)),
        ),
        default=None,
    )
    return title, body


def _next_shape_id(slide: dict[str, Any]) -> int:
    existing = [int(shape.get("shape_id", 0)) for shape in slide.get("shapes", [])]
    existing.extend(int(target.get("target_id", 0)) for target in slide.get("targets", [])
                    if target.get("target_kind") == "shape")
    return max(existing + [0]) + 1


def _synthetic_target(slide_number: int, target_id: int, role: str) -> dict[str, Any]:
    return {
        "slide_number": slide_number, "target_kind": "shape", "target_id": target_id,
        "role": role, "existing_text": "", "approximate_max_characters": 80 if role == "title" else 320,
        "max_content_length": 80 if role == "title" else 320, "required": True,
        "replaceable": True, "classification_confidence": 1.0,
        "shape_name": f"Generated {role.title()}", "font_size": 32.0 if role == "title" else 20.0,
        "position": {}, "generated_target": True, "_target_classified": True,
    }


def _build_slide_plan(template: dict[str, Any], source_content: str, blueprint: tuple[tuple[str, str, str], ...]) -> dict[str, Any]:
    """Return a ten-slide virtual template backed by compatible source slides.

    Target IDs are copied unchanged from their source slide. Only slide numbers
    are remapped to the final 1..10 sequence, so the normal strict validator and
    populator continue to enforce actual placeholder/shape identifiers.
    """
    normalized = normalize_template_targets(template)
    required = required_targets_by_slide(normalized)
    usable = [slide for slide in normalized.get("slides", []) if required.get(slide["slide_number"])]
    if not usable:
        raise SlidePlanningError("The selected template has no writable slides for a ten-slide presentation.")

    excerpts = _distribute_source(source_content, max(1, len(blueprint) - 2))
    source_units = _source_units(source_content)
    unused = {slide["slide_number"] for slide in usable}
    content_compatible = [slide for slide in usable if all(_target_pair(slide))]
    selected: list[dict[str, Any]] = []
    entries: list[SlidePlanEntry] = []
    factual_index = 0
    titled = [slide for slide in usable if _target_pair(slide)[0] is not None]
    cover_source = next((slide for slide in titled if slide.get("section_type") == "title"), next((slide for slide in titled if int(slide["slide_number"]) == 1), titled[0] if titled else usable[0]))
    closing_source = next(
        (slide for slide in reversed(titled) if slide.get("section_type") == "thank_you" and int(slide["slide_number"]) != int(cover_source["slide_number"])),
        next((slide for slide in reversed(titled) if int(slide["slide_number"]) != int(cover_source["slide_number"])), cover_source),
    )
    for output_number, (role, section_title, instruction) in enumerate(blueprint, start=1):
        body_required = output_number not in {1, len(blueprint)}
        if output_number == 1:
            source = cover_source
        elif output_number == len(blueprint):
            source = closing_source
        elif body_required and content_compatible:
            semantic_all = [slide for slide in content_compatible if _section_matches(slide, role)]
            semantic_unused = [slide for slide in semantic_all if slide["slide_number"] in unused]
            pool = semantic_unused or semantic_all or [slide for slide in content_compatible if slide["slide_number"] in unused] or content_compatible
            source = max(pool, key=lambda slide: _slide_score(slide, role, len(required.get(slide["slide_number"], {}))))
        else:
            titled = [slide for slide in usable if _target_pair(slide)[0] is not None]
            candidates = titled or usable
            pool = [slide for slide in candidates if slide["slide_number"] in unused] or candidates
            source = max(pool, key=lambda slide: _slide_score(slide, role, len(required.get(slide["slide_number"], {}))))
        unused.discard(source["slide_number"])
        copied = deepcopy(source)
        source_number = int(source["slide_number"])
        copied["slide_number"] = output_number
        copied["source_slide_number"] = source_number
        for target in copied.get("targets", []):
            target["slide_number"] = output_number
        excerpt = ""
        if output_number == 1 and excerpts:
            # The cover subtitle may summarize the source, but it must never be
            # derived from template metadata or sample text.
            excerpt = excerpts[0]
        elif role == "introduction" and source_units:
            # Introduction needs enough grounded context to produce a useful
            # 3-5 point overview; it is not an agenda or a filename summary.
            excerpt = "\n".join(source_units[:5])
            factual_index += 1
        elif body_required:
            excerpt = excerpts[factual_index]
            factual_index += 1
        create_title_id = None
        create_body_id = None
        copied["planned_role"] = role
        copied["section_type"] = role
        copied["planned_section_title"] = section_title
        copied["source_excerpt"] = excerpt
        replaceable_targets = [target for target in copied.get("targets", []) if target.get("replaceable")]
        title_target, body_target = _target_pair(copied)
        next_id = _next_shape_id(copied)
        if title_target is None:
            create_title_id = next_id; next_id += 1
            title_target = _synthetic_target(output_number, create_title_id, "title")
            copied.setdefault("targets", []).append(title_target)
        if body_required and body_target is None:
            create_body_id = next_id
            body_target = _synthetic_target(output_number, create_body_id, "body")
            copied.setdefault("targets", []).append(body_target)
        entry = SlidePlanEntry(output_number, source_number, role, section_title, instruction, excerpt, create_title_id, create_body_id)
        replaceable_targets = [target for target in copied.get("targets", []) if target.get("replaceable")]
        for target in replaceable_targets:
            target["required"] = target is title_target or (body_required and target is body_target)
            if target is title_target:
                target["role"] = "title"
            elif target is body_target:
                target["role"] = "body"
            target["section_type"] = role
            target["source_excerpt"] = excerpt
        subtitle_target = next(
            (target for target in replaceable_targets if target is not title_target and target.get("role") == "subtitle"),
            None,
        )
        optional_bodies = tuple(
            _target_ref(target) for target in replaceable_targets
            if target is not body_target and usable_body_target(target)
        )
        static_targets = tuple(
            _target_ref(target) for target in copied.get("targets", [])
            if not target.get("replaceable")
        )
        copied["slide_assignment"] = SlideAssignment(
            template_slide_number=source_number,
            section_type=role,
            title_target=_target_ref(title_target),
            subtitle_target=_target_ref(subtitle_target),
            primary_body_target=_target_ref(body_target),
            optional_body_targets=optional_bodies,
            replaceable_targets=tuple(_target_ref(target) for target in replaceable_targets),
            static_targets=static_targets,
        ).as_dict()
        selected.append(copied)
        entries.append(entry)

    planned = dict(normalized)
    planned["slides"] = selected
    planned["slide_count"] = len(blueprint)
    planned["slide_plan"] = [entry.as_dict() for entry in entries]
    return planned


def build_ten_slide_plan(template: dict[str, Any], source_content: str) -> dict[str, Any]:
    return _build_slide_plan(template, source_content, SLIDE_BLUEPRINT)


def build_slide_plan(template: dict[str, Any], source_content: str, desired_slide_count: int = 14) -> dict[str, Any]:
    if desired_slide_count == 10:
        blueprint = SLIDE_BLUEPRINT
    elif desired_slide_count >= 14:
        blueprint = FULL_SLIDE_BLUEPRINT
    else:
        middle_count = max(0, desired_slide_count - 2)
        middle = FULL_SLIDE_BLUEPRINT[1:-1]
        indexes = sorted({round(index * (len(middle) - 1) / max(1, middle_count - 1)) for index in range(middle_count)}) if middle_count else []
        blueprint = (FULL_SLIDE_BLUEPRINT[0], *(middle[index] for index in indexes), FULL_SLIDE_BLUEPRINT[-1])
    return _build_slide_plan(template, source_content, tuple(blueprint))
