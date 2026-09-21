import json
from types import SimpleNamespace

import pytest

from src.pipeline import GenerationPipeline
from src.response_validator import ResponseValidationError
from src.semantic_content import map_semantic_to_targets, validate_semantic_response
from src.slide_planner import build_slide_plan


def _target(target_id, role, *, required=True, capacity=250, left=0):
    return {"slide_number": 1, "target_kind": "shape", "target_id": target_id,
            "role": role, "existing_text": f"Sample {role}",
            "approximate_max_characters": capacity, "max_content_length": capacity,
            "required": required, "replaceable": True, "_target_classified": True,
            "position": {"left": left, "top": 0, "width": 100, "height": 50}}


def _template(section="introduction", targets=None):
    return {"template_id": "semantic", "slide_count": 1, "slide_plan": [{"slide_number": 1}],
            "slides": [{"slide_number": 1, "layout_name": "Blank", "section_type": section,
                        "planned_section_title": section.title(), "source_excerpt": "Evidence supports decisions. Evidence improves planning.",
                        "targets": targets or [_target(1, "title"), _target(2, "body")]}]}


def test_semantic_schema_keeps_notes_at_slide_level():
    response = validate_semantic_response({"slides": [{"slide_number": 1, "section_type": "title",
        "title": "A title", "bullets": [], "speaker_notes": "Say this"}]}, {1: "title"})
    assert response.slides[0].speaker_notes == "Say this"


def test_target_like_model_output_is_rejected_by_semantic_schema():
    raw = {"slides": [{"slide_number": 1, "section_type": "title", "title": "Title", "bullets": [],
                       "speaker_notes": None, "targets": [{"target_kind": "shape", "target_id": 1}]}]}
    with pytest.raises(ResponseValidationError, match="targets: Extra inputs are not permitted"):
        validate_semantic_response(raw, {1: "title"})


def test_semantic_content_maps_to_authoritative_targets_and_cards():
    template = _template(targets=[_target(1, "title", capacity=60), _target(2, "card", capacity=70, left=0),
                                 _target(3, "card", required=False, capacity=70, left=100)])
    semantic = validate_semantic_response({"slides": [{"slide_number": 1, "section_type": "introduction",
        "title": "Evidence overview", "bullets": ["Evidence supports decisions", "Evidence improves planning"],
        "speaker_notes": "Explain the evidence"}]}, {1: "introduction"})
    mapped, _ = map_semantic_to_targets(semantic, template, "Deck")
    slide = mapped.slides[0]
    assert [(item.target_kind, item.target_id) for item in slide.targets] == [("shape", 1), ("shape", 2), ("shape", 3)]
    assert slide.targets[1].bullets == ["Evidence supports decisions"]
    assert slide.targets[2].bullets == ["Evidence improves planning"]
    assert slide.speaker_notes == "Explain the evidence"


@pytest.mark.parametrize("section", ["title", "qa"])
def test_title_and_closing_slides_require_no_body_mapping(section):
    template = _template(section=section, targets=[_target(1, "title")])
    semantic = validate_semantic_response({"slides": [{"slide_number": 1, "section_type": section,
        "title": "Questions?" if section == "qa" else "Presentation", "bullets": [], "speaker_notes": None}]}, {1: section})
    mapped, _ = map_semantic_to_targets(semantic, template, "Deck")
    assert len(mapped.slides[0].targets) == 1 and mapped.slides[0].targets[0].target_id == 1


def test_only_failed_semantic_slide_gets_one_focused_repair(tmp_path):
    template = _template(section="title", targets=[_target(1, "title")])
    responses = iter([
        {"slides": [{"slide_number": 1, "section_type": "title", "targets": []}]},
        {"slides": [{"slide_number": 1, "section_type": "title", "title": "Repaired", "bullets": [], "speaker_notes": None}]},
    ])
    class Watsonx:
        def __init__(self): self.calls = 0
        def chat(self, *_args, **_kwargs): self.calls += 1; return SimpleNamespace(text=json.dumps(next(responses)))
    watsonx = Watsonx(); pipeline = GenerationPipeline(None, watsonx, tmp_path, tmp_path)
    content, warnings, calls = pipeline._generate_validated(model_id="mock", template=template,
        message_args={"topic": "Deck", "source_content": "Evidence", "audience": "All", "tone": "Clear", "additional_instructions": ""})
    assert calls == watsonx.calls == 2
    assert content.slides[0].targets[0].text == "Deck"
    assert "focused semantic repair" in warnings[0]


def test_unknown_semantic_slide_is_rejected():
    with pytest.raises(ResponseValidationError, match="Unknown semantic slide"):
        validate_semantic_response({"slides": [{"slide_number": 9, "section_type": "title", "title": "X", "bullets": []}]}, {1: "title"})


def test_mapping_sorts_out_of_order_semantic_results_by_slide_number():
    template = _template(section="title", targets=[_target(1, "title")])
    second = {**template["slides"][0], "slide_number": 2, "section_type": "qa"}
    second["targets"] = [{**_target(2, "title"), "slide_number": 2}]
    template["slides"].append(second); template["slide_count"] = 2
    semantic = validate_semantic_response({"slides": [
        {"slide_number": 2, "section_type": "qa", "title": "Questions", "bullets": []},
        {"slide_number": 1, "section_type": "title", "title": "Document title", "bullets": []},
    ]}, {1: "title", 2: "qa"})
    mapped, _ = map_semantic_to_targets(semantic, template, "Document title")
    assert [slide.slide_number for slide in mapped.slides] == [1, 2]


def test_closing_content_is_forced_to_minimal_thank_you():
    template = _template(section="closing", targets=[_target(1, "title")])
    semantic = validate_semantic_response({"slides": [{"slide_number": 1, "section_type": "closing", "title": "Untrusted closing", "bullets": ["Unrelated bullet"]}]}, {1: "closing"})
    mapped, _ = map_semantic_to_targets(semantic, template, "Document title")
    assert mapped.slides[0].targets[0].text == "Thank You"
    assert len(mapped.slides[0].targets) == 1


def test_title_uses_trusted_document_title_and_grounded_subtitle():
    template = _template(section="title", targets=[_target(1, "title"), _target(2, "subtitle", required=False)])
    semantic = validate_semantic_response({"slides": [{"slide_number": 1, "section_type": "title",
        "title": "PptxGenJS Presentation", "bullets": [], "speaker_notes": None}]}, {1: "title"})
    mapped, _ = map_semantic_to_targets(semantic, template, "Trusted document title")
    values = {target.target_id: target.text for target in mapped.slides[0].targets}
    assert values[1] == "Trusted document title"
    assert values[2].startswith("Evidence supports decisions")


def test_tiny_misclassified_body_target_is_replaced_during_planning():
    template = _template(targets=[
        _target(1, "title", capacity=60),
        {**_target(2, "body", capacity=23),
         "capacity": {"characters_per_line": 6, "maximum_lines": 1}},
    ])
    template["slides"][0]["shapes"] = [{"shape_id": 1}, {"shape_id": 2}]
    planned = build_slide_plan(template, "Evidence supports planning and informed decisions.", 3)
    content_slide = planned["slides"][1]
    body = next(target for target in content_slide["targets"]
                if target.get("role") == "body" and target.get("required"))
    assert body["target_id"] != 2
    assert body["generated_target"] is True
    assert body["approximate_max_characters"] >= 250
