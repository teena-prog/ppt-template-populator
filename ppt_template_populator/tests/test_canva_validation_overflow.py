import json
from types import SimpleNamespace

import pytest

from src.pipeline import GenerationPipeline
from src.response_validator import ResponseValidationError, validate_response
from src.template_parser import _max_content_length, SimpleShape


def _template(count=1):
    slides = []
    for number in range(1, count + 1):
        slides.append({"slide_number": number, "layout_name": "Blank", "targets": [
            {"slide_number": number, "target_kind": "shape", "target_id": number * 10, "role": "title", "existing_text": f"Title {number}", "approximate_max_characters": 60, "max_content_length": 60, "required": True, "replaceable": True, "position": {"left": 0, "top": 0, "width": 100, "height": 40}},
            {"slide_number": number, "target_kind": "shape", "target_id": number * 10 + 1, "role": "caption", "existing_text": f"Optional {number}", "approximate_max_characters": 80, "max_content_length": 80, "required": False, "replaceable": True, "position": {"left": 0, "top": 50, "width": 80, "height": 20}},
        ]})
    return {"template_id": "canva", "slide_count": count, "slides": slides}


def _payload(number=1, text="A concise title"):
    return {"presentation_title": "Test", "slides": [{"slide_number": number, "layout_name": "Blank", "targets": [{"target_kind": "shape", "target_id": number * 10, "content": text}]}]}


def test_optional_canva_target_may_be_omitted():
    content, _ = validate_response(_payload(), _template())
    assert len(content.slides[0].targets) == 1


def test_required_canva_target_missing():
    payload = {"presentation_title": "Test", "slides": [{"slide_number": 1, "layout_name": "Blank", "targets": []}]}
    with pytest.raises(ResponseValidationError, match="shape 10: required content was not generated"): validate_response(payload, _template())


def test_minor_overflow_is_warning_and_significant_requires_repair():
    template = _template(); template["slides"][0]["targets"][0]["approximate_max_characters"] = 10
    template["slides"][0]["targets"][0]["max_content_length"] = 10
    _, warnings = validate_response(_payload(text="12345678901"), template)
    assert any("minor overflow" in warning for warning in warnings)
    with pytest.raises(ResponseValidationError, match="significant overflow"): validate_response(_payload(text="1234567890123"), template)


def test_role_based_minimum_capacity_prevents_tiny_body_limit():
    shape = SimpleShape({"position": {"width": 100000, "height": 100000}, "text_box_margins": {}})
    assert _max_content_length(shape, "body", 18, "Body") >= 250
    assert _max_content_length(shape, "title", 32, "Title") >= 60


def test_duplicate_and_invented_targets_are_rejected():
    payload = _payload(); payload["slides"][0]["targets"] *= 2
    with pytest.raises(ResponseValidationError, match="Duplicate shape 10"): validate_response(payload, _template())
    payload = _payload(); payload["slides"][0]["targets"][0]["target_id"] = 999
    with pytest.raises(ResponseValidationError, match="Unknown shape 999"): validate_response(payload, _template())


class ChunkWatson:
    def __init__(self): self.calls = []
    def chat(self, model_id, messages, **kwargs):
        structure = json.loads(messages[1]["content"].split("\n", 1)[1])["template_structure"]
        self.calls.append([slide["slide_number"] for slide in structure])
        slides = [{"slide_number": slide["slide_number"], "layout_name": "Blank", "targets": [{"target_kind": "shape", "target_id": slide["targets"][0]["target_id"], "content": "Title"}]} for slide in structure]
        return SimpleNamespace(text=json.dumps({"slides": slides}))


def test_large_template_generation_is_chunked(tmp_path):
    watson = ChunkWatson(); pipeline = GenerationPipeline(None, watson, tmp_path, tmp_path)
    content, _, calls = pipeline._generate_validated(model_id="model", template=_template(9), message_args={"topic": "Test", "source_content": "Facts", "audience": "People", "tone": "Clear", "additional_instructions": ""})
    assert calls == 3 and watson.calls == [[1, 2, 3, 4], [5, 6, 7, 8], [9]] and len(content.slides) == 9


class RepairWatson(ChunkWatson):
    def chat(self, model_id, messages, **kwargs):
        payload = json.loads(messages[1]["content"].split("\n", 1)[-1])
        if "ordered_targets" in payload:
            numbers = [item["slide_number"] for item in payload["ordered_targets"]]; self.calls.append(numbers)
            return SimpleNamespace(text=json.dumps({"contents": ["Title" for _ in payload["ordered_targets"]]}))
        structure = payload["template_structure"]
        numbers = [slide["slide_number"] for slide in structure]; self.calls.append(numbers)
        emitted = numbers[:1] if len(self.calls) == 1 else numbers
        slides = [{"slide_number": number, "layout_name": "Blank", "targets": [{"target_kind": "shape", "target_id": number * 10, "content": "Title"}]} for number in emitted]
        return SimpleNamespace(text=json.dumps({"slides": slides}))


def test_repair_prompt_contains_only_failing_slides(tmp_path):
    watson = RepairWatson(); pipeline = GenerationPipeline(None, watson, tmp_path, tmp_path)
    content, warnings, calls = pipeline._generate_validated(model_id="model", template=_template(2), message_args={"topic": "Test", "source_content": "Facts", "audience": "People", "tone": "Clear", "additional_instructions": ""})
    assert calls == 2 and watson.calls == [[1, 2], [2]] and len(content.slides) == 2
    assert any("Focused recovery" in warning for warning in warnings)


class MissingKindWatson:
    def __init__(self): self.calls = []
    def chat(self, model_id, messages, **kwargs):
        self.calls.append(messages)
        target = {"target_id": 10, "content": "Title"}
        if len(self.calls) == 2: target["target_kind"] = "shape"
        return SimpleNamespace(text=json.dumps({"slides": [{"slide_number": 1, "targets": [target]}]}))


def test_repaired_chunk_receives_exact_field_path_and_schema(tmp_path):
    watson = MissingKindWatson(); pipeline = GenerationPipeline(None, watson, tmp_path, tmp_path)
    content, _, calls = pipeline._generate_validated(model_id="model", template=_template(), message_args={"topic": "Test", "source_content": "Facts", "audience": "People", "tone": "Clear", "additional_instructions": ""})
    repair = json.loads(watson.calls[1][-1]["content"])
    assert calls == 2 and len(content.slides) == 1
    assert "slides.0.targets.0.target_kind: Field required" in repair["validation_errors"]
    assert repair["required_json_schema"]["title"] == "SlideChunkResponse"
