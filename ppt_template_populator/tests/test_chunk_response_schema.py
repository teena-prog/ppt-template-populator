import json

import pytest

from src.response_models import PresentationContent, SlideChunkResponse
from src.response_validator import ResponseValidationError, safe_response_structure, validate_chunk_response, validate_response


def _template(count=1):
    return {"template_id": "canva", "slide_count": count, "slides": [{"slide_number": number, "layout_name": "Blank", "targets": [{"slide_number": number, "target_kind": "shape", "target_id": number * 10, "role": "title", "existing_text": f"Title {number}", "approximate_max_characters": 60, "max_content_length": 60, "required": True, "replaceable": True, "position": {"left": 0, "top": 0, "width": 100, "height": 40}}]} for number in range(1, count + 1)]}


def _payload(number=1, text="A concise title"):
    return {"presentation_title": "Test", "slides": [{"slide_number": number, "layout_name": "Blank", "targets": [{"target_kind": "shape", "target_id": number * 10, "content": text}]}]}


def test_slides_only_response_is_the_valid_generation_contract():
    content, _ = validate_response({"slides": _payload()["slides"]}, _template(), presentation_title="Trusted title")
    assert content.presentation_title == "Trusted title"


def test_harmless_top_level_title_is_removed_when_slides_are_valid():
    payload = {"title": "Model title", "slides": _payload()["slides"]}
    content, _ = validate_response(payload, _template(), presentation_title="Trusted title")
    assert content.presentation_title == "Trusted title"


def test_obvious_model_response_wrapper_is_unwrapped():
    raw = "Here is the result: " + json.dumps({"response": {"slides": _payload()["slides"]}})
    content, _ = validate_response(raw, _template())
    assert content.slides[0].targets[0].target_id == 10


def test_unknown_top_level_and_target_fields_remain_forbidden():
    payload = _payload(); payload["metadata"] = {"source": "model"}
    with pytest.raises(ResponseValidationError, match="metadata: Extra inputs"):
        validate_response(payload, _template())
    payload = _payload(); payload["slides"][0]["targets"][0]["role"] = "title"
    with pytest.raises(ResponseValidationError, match="role: Extra inputs"):
        validate_response(payload, _template())
    payload = _payload(); payload["slides"][0]["targets"][0]["confidence"] = .9
    with pytest.raises(ResponseValidationError, match="confidence: Extra inputs"):
        validate_response(payload, _template())


def test_chunk_without_title_and_speaker_notes_is_valid_and_layout_is_trusted():
    slide = _payload()["slides"][0]; slide.pop("layout_name")
    chunk, _ = validate_chunk_response({"slides": [slide]}, _template())
    assert chunk.slides[0].layout_name == "Blank" and chunk.slides[0].speaker_notes is None


def test_missing_target_kind_reports_complete_path():
    payload = _payload(); payload["slides"][0]["targets"][0].pop("target_kind")
    with pytest.raises(ResponseValidationError) as captured: validate_response(payload, _template())
    assert "slides.0.targets.0.target_kind: Field required" in captured.value.errors


def test_old_placeholder_only_schema_and_placeholder_id_are_rejected():
    old = _payload(); target = old["slides"][0].pop("targets")[0]
    old["slides"][0]["placeholders"] = [{"placeholder_id": target["target_id"], "content": target["content"]}]
    with pytest.raises(ResponseValidationError) as captured: validate_response(old, _template())
    assert "slides.0.targets: Field required" in captured.value.errors
    current = _payload(); target = current["slides"][0]["targets"][0]; target["placeholder_id"] = target.pop("target_id")
    with pytest.raises(ResponseValidationError) as captured: validate_response(current, _template())
    assert "slides.0.targets.0.target_id: Field required" in captured.value.errors


def test_markdown_wrapped_chunk_json_is_parsed():
    raw = "```json\n" + json.dumps({"slides": _payload()["slides"]}) + "\n```"
    chunk, _ = validate_chunk_response(raw, _template())
    assert chunk.slides[0].slide_number == 1
    assert safe_response_structure(raw)["markdown_fence"] is True


def test_complete_and_chunk_schemas_are_distinct():
    assert "presentation_title" in PresentationContent.model_json_schema()["required"]
    assert "presentation_title" not in SlideChunkResponse.model_json_schema().get("properties", {})
    target_required = SlideChunkResponse.model_json_schema()["$defs"]["TargetContent"]["required"]
    assert {"target_kind", "target_id", "content_type"}.issubset(target_required)


def test_final_merge_rejects_missing_slides_and_coalesces_identical_fragments():
    template = _template(2)
    missing = _payload(1)
    with pytest.raises(ResponseValidationError, match=r"Missing required slides: \[2\]"): validate_response(missing, template)
    duplicate = {"presentation_title": "Test", "slides": [_payload(1)["slides"][0], _payload(1)["slides"][0]]}
    with pytest.raises(ResponseValidationError, match=r"Missing required slides: \[2\]"):
        validate_response(duplicate, template)


def test_safe_structure_excludes_content_values():
    secret_source_text = "sensitive generated content"
    raw = json.dumps(_payload(text=secret_source_text))
    summary = safe_response_structure(raw)
    assert secret_source_text not in repr(summary) and "content" not in summary["slides"][0]["target_keys"]


def test_target_level_metadata_is_not_silently_removed():
    payload = _payload()
    payload["slides"][0]["targets"][0].update({"slide_number": 1, "role": "title"})
    with pytest.raises(ResponseValidationError) as captured:
        validate_response(payload, _template())
    assert "slides.0.targets.0.slide_number: Extra inputs are not permitted" in captured.value.errors
    assert "slides.0.targets.0.role: Extra inputs are not permitted" in captured.value.errors


def test_nested_slide_number_is_rejected_as_target_extra():
    payload = _payload()
    payload["slides"][0]["targets"][0]["slide_number"] = 99
    with pytest.raises(ResponseValidationError, match="slide_number: Extra inputs are not permitted"):
        validate_response(payload, _template())


def test_target_level_speaker_notes_move_to_empty_parent_slide():
    payload = _payload()
    payload["slides"][0]["targets"][0]["speaker_notes"] = "Presenter context"
    content, _ = validate_response(payload, _template())
    assert content.slides[0].speaker_notes == "Presenter context"
    assert content.slides[0].targets[0].target_id == 10


def test_complete_slide_nested_in_targets_is_lifted_and_validated():
    template = _template(2)
    first = _payload(1)["slides"][0]
    second = _payload(2)["slides"][0]
    first["section_type"] = "introduction"
    second["section_type"] = "problem"
    first["targets"].append(second)
    content, _ = validate_response({"slides": [first]}, template)
    assert [slide.slide_number for slide in content.slides] == [1, 2]


def test_ambiguous_nested_slide_and_target_object_is_rejected():
    payload = _payload()
    payload["slides"][0]["targets"].append({
        "slide_number": 2, "layout_name": "Blank", "section_type": "problem",
        "targets": [], "target_kind": "shape", "target_id": 20,
        "content_type": "title", "text": "Ambiguous",
    })
    with pytest.raises(ResponseValidationError, match="ambiguous object mixes slide and target fields"):
        validate_response(payload, _template(2))


def test_misplaced_notes_conflicting_with_parent_are_rejected():
    payload = _payload()
    payload["slides"][0]["speaker_notes"] = "Parent notes"
    payload["slides"][0]["targets"][0]["speaker_notes"] = "Different notes"
    with pytest.raises(ResponseValidationError, match="conflicts with parent slide notes"):
        validate_response(payload, _template())


def test_lifted_unknown_slide_is_not_made_safe():
    payload = _payload()
    nested = _payload(99)["slides"][0]
    nested["section_type"] = "unknown"
    payload["slides"][0]["targets"].append(nested)
    with pytest.raises(ResponseValidationError, match="Unknown slide"):
        validate_response(payload, _template())


def test_missing_id_is_recovered_only_for_one_unused_expected_target():
    payload = _payload()
    payload["slides"][0]["targets"][0].pop("target_id")
    content, _ = validate_response(payload, _template())
    assert content.slides[0].targets[0].target_id == 10


def test_missing_id_remains_invalid_when_multiple_targets_are_possible():
    template = _template()
    template["slides"][0]["targets"].append({
        **template["slides"][0]["targets"][0], "target_id": 11,
        "role": "body", "existing_text": "Body",
        "approximate_max_characters": 250, "max_content_length": 250,
    })
    payload = _payload()
    payload["slides"][0]["targets"][0].pop("target_id")
    with pytest.raises(ResponseValidationError) as captured:
        validate_response(payload, template)
    assert "slides.0.targets.0.target_id: Field required" in captured.value.errors
