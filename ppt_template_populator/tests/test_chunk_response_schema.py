import json

import pytest

from src.response_models import PresentationContent, SlideChunkResponse
from src.response_validator import ResponseValidationError, safe_response_structure, validate_chunk_response, validate_response


def _template(count=1):
    return {"template_id": "canva", "slide_count": count, "slides": [{"slide_number": number, "layout_name": "Blank", "targets": [{"slide_number": number, "target_kind": "shape", "target_id": number * 10, "role": "title", "existing_text": f"Title {number}", "approximate_max_characters": 60, "max_content_length": 60, "required": True, "replaceable": True, "position": {"left": 0, "top": 0, "width": 100, "height": 40}}]} for number in range(1, count + 1)]}


def _payload(number=1, text="A concise title"):
    return {"presentation_title": "Test", "slides": [{"slide_number": number, "layout_name": "Blank", "targets": [{"target_kind": "shape", "target_id": number * 10, "content": text}]}]}


def test_full_response_missing_presentation_title_has_safe_path():
    with pytest.raises(ResponseValidationError) as captured:
        validate_response({"slides": _payload()["slides"]}, _template())
    assert captured.value.errors == ["presentation_title: Field required"]


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
    assert {"target_kind", "target_id", "content"}.issubset(target_required)


def test_final_merge_rejects_missing_and_duplicate_slides():
    template = _template(2)
    missing = _payload(1)
    with pytest.raises(ResponseValidationError, match=r"Missing required slides: \[2\]"): validate_response(missing, template)
    duplicate = {"presentation_title": "Test", "slides": [_payload(1)["slides"][0], _payload(1)["slides"][0]]}
    with pytest.raises(ResponseValidationError, match="Duplicate slide number 1"): validate_response(duplicate, template)


def test_safe_structure_excludes_content_values():
    secret_source_text = "sensitive generated content"
    raw = json.dumps(_payload(text=secret_source_text))
    summary = safe_response_structure(raw)
    assert secret_source_text not in repr(summary) and "content" not in summary["slides"][0]["target_keys"]
