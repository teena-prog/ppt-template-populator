import json

import pytest

from src.prompt_builder import build_semantic_messages
from src.response_models import SemanticSlideContent, SemanticSlideResponse
from src.response_validator import ResponseValidationError
from src.semantic_content import map_semantic_to_targets
from src.slide_planner import build_slide_plan
from src.target_metadata import target_capacity_limits, usable_body_target


def _target(target_id, role, maximum, lines, *, required=True, area=100):
    return {
        "slide_number": 1, "target_kind": "shape", "target_id": target_id,
        "role": role, "replaceable": True, "required": required,
        "existing_text": "Template instructions that will be replaced",
        "approximate_max_characters": maximum, "max_content_length": maximum,
        "capacity": {"maximum_characters": maximum, "maximum_words": maximum // 6,
                     "maximum_lines": lines, "characters_per_line": max(12, maximum // max(1, lines)),
                     "line_spacing": 1.2},
        "position": {"left": 0, "top": 0, "width": area, "height": area},
        "_target_classified": True,
    }


def _template(targets):
    return {"template_id": "capacity", "slide_count": 1,
            "slides": [{"slide_number": 1, "layout_name": "Blank",
                        "section_type": "introduction", "source_excerpt": "Grounded source information supports careful operational planning.",
                        "targets": targets}]}


def test_replaceable_placeholder_text_does_not_reduce_capacity():
    short = _target(1, "body", 180, 3)
    long = {**short, "existing_text": "X" * 500}
    assert target_capacity_limits(short) == target_capacity_limits(long)


def test_small_label_is_not_usable_as_body():
    assert not usable_body_target(_target(6, "body", 23, 1))


def test_largest_compatible_body_target_is_selected():
    template = _template([_target(1, "title", 80, 2),
                          _target(2, "body", 70, 2, required=False, area=50),
                          _target(3, "body", 240, 5, required=False, area=200)])
    semantic = SemanticSlideResponse(slides=[SemanticSlideContent(
        slide_number=1, section_type="introduction", title="Introduction",
        bullets=["Grounded source information supports planning"],
    )])
    content, _ = map_semantic_to_targets(semantic, template, "Deck")
    assert [target.target_id for target in content.slides[0].targets] == [1, 3]


def test_two_bullet_capacity_is_sent_to_prompt():
    slide = _template([_target(1, "title", 80, 2), _target(2, "body", 100, 2)])["slides"][0]
    messages = build_semantic_messages(slide=slide, topic="Topic", audience="All", tone="Clear")
    payload = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert payload["planned_slide"]["generation_limits"]["requested_bullet_count"] == 2


def test_zero_capacity_body_is_excluded_from_prompt():
    slide = _template([_target(1, "title", 80, 2), _target(6, "body", 0, 0)])["slides"][0]
    messages = build_semantic_messages(slide=slide, topic="Topic", audience="All", tone="Clear")
    payload = json.loads(messages[1]["content"].split("\n", 1)[1])
    ids = [item["target_id"] for item in payload["planned_slide"]["authoritative_writable_targets"]]
    assert 6 not in ids
    assert payload["planned_slide"]["generation_limits"]["requested_bullet_count"] == 0


def test_duplicate_allocation_key_is_rejected():
    body = _target(2, "body", 180, 3, required=False)
    template = _template([_target(1, "title", 80, 2), body, dict(body)])
    semantic = SemanticSlideResponse(slides=[SemanticSlideContent(
        slide_number=1, section_type="introduction", title="Introduction",
        bullets=["Grounded source information supports planning"],
    )])
    with pytest.raises(ResponseValidationError, match="Duplicate allocation target"):
        map_semantic_to_targets(semantic, template, "Deck")


def test_planner_replaces_only_unsafe_body_with_generated_region():
    template = _template([_target(1, "title", 80, 2), _target(6, "body", 23, 1)])
    template["slides"][0]["shapes"] = [{"shape_id": 1}, {"shape_id": 6}]
    planned = build_slide_plan(template, "Grounded source information supports planning.", 3)
    body = next(target for target in planned["slides"][1]["targets"]
                if target.get("role") == "body" and target.get("required"))
    assert body.get("generated_target") is True
    assert body["target_id"] != 6


def test_no_safe_body_returns_template_compatibility_error():
    template = _template([_target(1, "title", 80, 2), _target(6, "body", 23, 1)])
    semantic = SemanticSlideResponse(slides=[SemanticSlideContent(
        slide_number=1, section_type="introduction", title="Introduction",
        bullets=["Grounded source information supports planning"],
    )])
    with pytest.raises(ResponseValidationError, match="does not provide enough space"):
        map_semantic_to_targets(semantic, template, "Deck")
