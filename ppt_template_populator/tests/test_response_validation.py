import json
import pytest
from src.response_validator import ResponseValidationError, extract_json, parse_chunk_response, validate_chunk_response, validate_response
from src.slide_planner import build_ten_slide_plan


def test_valid_json_response(valid_payload, template_metadata):
    result, warnings = validate_response(f"```json\n{json.dumps(valid_payload)}\n```", template_metadata)
    assert result.presentation_title == "Test" and warnings == []


def test_invalid_json_response():
    with pytest.raises(ResponseValidationError): extract_json("not json")


def test_unknown_slide(valid_payload, template_metadata):
    valid_payload["slides"][0]["slide_number"] = 99
    with pytest.raises(ResponseValidationError, match="Unknown slide"): validate_response(valid_payload, template_metadata)


def test_unknown_placeholder(valid_payload, template_metadata):
    valid_payload["slides"][0]["targets"][0]["target_id"] = 999
    with pytest.raises(ResponseValidationError, match="Unknown placeholder"): validate_response(valid_payload, template_metadata)


def test_missing_content(valid_payload, template_metadata):
    valid_payload["slides"][0]["targets"][0]["content"] = ""
    with pytest.raises(ResponseValidationError): validate_response(valid_payload, template_metadata)


def test_missing_required_placeholder(valid_payload, template_metadata):
    valid_payload["slides"][0]["targets"].pop()
    with pytest.raises(ResponseValidationError, match="placeholder 1: required content was not generated"): validate_response(valid_payload, template_metadata)


def test_known_optional_replaceable_shape_is_allowed(valid_payload, template_metadata):
    template_metadata["slides"][0]["targets"].append({
        "slide_number": 1,
        "target_kind": "shape",
        "target_id": 16,
        "role": "caption",
        "existing_text": "Optional sample caption",
        "approximate_max_characters": 80,
        "max_content_length": 80,
        "required": False,
        "replaceable": True,
        "_target_classified": True,
    })
    valid_payload["slides"][0]["targets"].append({
        "target_kind": "shape", "target_id": 16, "content": "Grounded caption"
    })
    result, _ = validate_response(valid_payload, template_metadata)
    assert any(target.target_id == 16 for target in result.slides[0].targets)


def test_non_replaceable_shape_remains_rejected(valid_payload, template_metadata):
    template_metadata["slides"][0]["targets"].append({
        "slide_number": 1,
        "target_kind": "shape",
        "target_id": 16,
        "role": "footer",
        "existing_text": "Protected footer",
        "approximate_max_characters": 80,
        "required": False,
        "replaceable": False,
        "_target_classified": True,
    })
    valid_payload["slides"][0]["targets"].append({
        "target_kind": "shape", "target_id": 16, "content": "Must not replace"
    })
    with pytest.raises(ResponseValidationError, match="Unknown shape 16"):
        validate_response(valid_payload, template_metadata)


def test_legacy_body_paragraph_is_split_without_requiring_newlines(template_metadata):
    planned = build_ten_slide_plan(template_metadata, "Evidence supports planning. Teams use evidence for decisions.")
    slide = planned["slides"][1]
    targets = []
    for target in slide["targets"]:
        if not target.get("required"): continue
        content = "Evidence supports planning. Teams use evidence for decisions." if target["role"] == "body" else "Introduction"
        targets.append({"target_kind": target["target_kind"], "target_id": target["target_id"], "content": content})
    chunk, _ = validate_chunk_response({"slides": [{"slide_number": 2, "layout_name": slide["layout_name"], "targets": targets}]}, {**planned, "slides": [slide], "slide_count": 1})
    body = next(item for item in chunk.slides[0].targets if item.content_type == "bullets")
    assert body.bullets == ["Evidence supports planning.", "Teams use evidence for decisions."]


def test_grounded_paraphrase_is_accepted_but_new_number_requests_recovery(template_metadata):
    planned = build_ten_slide_plan(template_metadata, "Agricultural evidence supports practical planning for farmers.")
    slide = planned["slides"][1]
    body_meta = next(target for target in slide["targets"] if target.get("required") and target["role"] == "body")
    title_meta = next(target for target in slide["targets"] if target.get("required") and target["role"] == "title")
    def payload(point):
        return {"slides": [{"slide_number": 2, "layout_name": slide["layout_name"], "targets": [
            {"target_kind": title_meta["target_kind"], "target_id": title_meta["target_id"], "content_type": "title", "text": "Introduction"},
            {"target_kind": body_meta["target_kind"], "target_id": body_meta["target_id"], "content_type": "bullets", "bullets": [point]},
        ]}]}
    validate_chunk_response(payload("Farmers can use agricultural evidence when making practical plans."), {**planned, "slides": [slide], "slide_count": 1})
    with pytest.raises(ResponseValidationError, match="source-unverified numbers"):
        validate_chunk_response(payload("Agricultural evidence improved outcomes by 75 percent."), {**planned, "slides": [slide], "slide_count": 1})


def _typed_template(role="title", target_id=7):
    return {"template_id": "typed", "slides": [{
        "slide_number": 1, "layout_name": "Blank", "targets": [{
            "slide_number": 1, "target_kind": "shape", "target_id": target_id,
            "role": role, "existing_text": role, "approximate_max_characters": 250,
            "required": True, "replaceable": True, "_target_classified": True,
        }],
    }]}


@pytest.mark.parametrize("alias", ["heading", "title", "subtitle"])
def test_title_content_type_aliases_come_from_trusted_role(alias):
    payload = {"slides": [{"slide_number": 1, "layout_name": "Blank", "targets": [
        {"target_kind": "shape", "target_id": 7, "content_type": alias, "text": "Trusted title"}
    ]}]}
    parsed = parse_chunk_response(payload, _typed_template("title"))
    assert parsed.slides[0].targets[0].content_type == "title"


@pytest.mark.parametrize("alias", ["body", "text", "paragraph", "bullet", "bullet_list", "list"])
def test_body_content_type_aliases_come_from_trusted_role(alias):
    payload = {"slides": [{"slide_number": 1, "layout_name": "Blank", "targets": [
        {"target_kind": "shape", "target_id": 7, "content_type": alias,
         "text": "First grounded point. Second grounded point."}
    ]}]}
    parsed = parse_chunk_response(payload, _typed_template("body"))
    target = parsed.slides[0].targets[0]
    assert target.content_type == "bullets"
    assert target.bullets == ["First grounded point.", "Second grounded point."]


def test_missing_content_type_is_derived_only_for_known_target_role():
    payload = {"slides": [{"slide_number": 1, "layout_name": "Blank", "targets": [
        {"target_kind": "shape", "target_id": 7, "content": "Known heading"}
    ]}]}
    target = parse_chunk_response(payload, _typed_template("heading")).slides[0].targets[0]
    assert target.content_type == "title" and target.text == "Known heading"


def test_invalid_content_type_with_ambiguous_role_is_not_guessed():
    payload = {"slides": [{"slide_number": 1, "layout_name": "Blank", "targets": [
        {"target_kind": "shape", "target_id": 7, "content_type": "section", "text": "Value"}
    ]}]}
    with pytest.raises(ResponseValidationError, match="content_type"):
        parse_chunk_response(payload, _typed_template("unknown"))


def test_alias_normalization_does_not_make_unknown_target_safe():
    payload = {"slides": [{"slide_number": 1, "layout_name": "Blank", "targets": [
        {"target_kind": "shape", "target_id": 999, "content_type": "title", "text": "Unknown"}
    ]}]}
    with pytest.raises(ResponseValidationError, match="Unknown shape 999"):
        validate_chunk_response(payload, _typed_template("title"))


def test_initial_and_repair_payloads_use_identical_content_type_normalization():
    payload = {"slides": [{"slide_number": 1, "layout_name": "Blank", "targets": [
        {"target_kind": "shape", "target_id": 7, "content_type": "paragraph",
         "text": "First fact. Second fact."}
    ]}]}
    template = _typed_template("body")
    initial = parse_chunk_response(payload, template)
    repaired = parse_chunk_response(payload, template)
    assert initial.model_dump() == repaired.model_dump()
    assert repaired.slides[0].targets[0].content_type == "bullets"
