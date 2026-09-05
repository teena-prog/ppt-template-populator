import json
import pytest
from src.response_validator import ResponseValidationError, extract_json, validate_response


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
