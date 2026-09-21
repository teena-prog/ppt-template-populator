import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.content_normalizer import BulletNormalizationError, normalize_bullets
from src.pipeline import GenerationPipeline
from src.response_models import TargetContent
from src.response_validator import ResponseValidationError, parse_chunk_response


def _target(target_id: int, role: str) -> dict:
    return {
        "slide_number": 1, "target_kind": "shape", "target_id": target_id,
        "role": role, "existing_text": f"Sample {role}",
        "approximate_max_characters": 250, "max_content_length": 250,
        "required": True, "replaceable": True, "_target_classified": True,
        "position": {"left": 0, "top": 0, "width": 300, "height": 100},
    }


def _semantic_template() -> dict:
    return {
        "template_id": "bullet-repair", "slide_count": 1,
        "slide_plan": [{"slide_number": 1}],
        "slides": [{
            "slide_number": 1, "layout_name": "Blank",
            "section_type": "introduction", "planned_section_title": "Introduction",
            "source_excerpt": "Artificial intelligence improves planning and supports decisions.",
            "targets": [_target(1, "title"), _target(2, "body")],
        }],
    }


class _Watsonx:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
        self.messages = []

    def chat(self, _model_id, messages, **_kwargs):
        self.calls += 1
        self.messages.append(messages)
        return SimpleNamespace(text=json.dumps(next(self.responses)))


def _args() -> dict:
    return {"topic": "AI planning", "source_content": "Artificial intelligence improves planning.",
            "audience": "Managers", "tone": "Clear", "additional_instructions": ""}


def test_target_pydantic_error_is_contextual_response_validation_error():
    template = {"slides": [{"slide_number": 2, "layout_name": "Blank", "targets": [
        {**_target(6, "body"), "slide_number": 2},
    ]}]}
    raw = {"slides": [{"slide_number": 2, "layout_name": "Blank", "targets": [{
        "target_kind": "shape", "target_id": 6, "content_type": "bullets",
        "content": ["Think", "Can", "Me", "L"],
    }]}]}
    with pytest.raises(ResponseValidationError) as captured:
        parse_chunk_response(raw, template)
    message = str(captured.value)
    assert "slide 2" in message and "target 6" in message
    assert "body bullets must not contain single-character fragments" in message
    assert "pydantic.dev" not in message
    failure = next(item for item in captured.value.failures if item.target_id == 6)
    assert failure.stage == "schema_validation"
    assert failure.slide_number == 2
    assert failure.target_kind == "shape"
    assert failure.repairable is True


def test_single_character_bullet_triggers_one_controlled_repair(tmp_path):
    invalid = {"slides": [{"slide_number": 1, "section_type": "introduction",
                            "title": "Introduction", "bullets": ["Think", "Can", "Me", "L"]}]}
    repaired = {"slides": [{"slide_number": 1, "section_type": "introduction",
                             "title": "Introduction", "bullets": [
                                 "Artificial intelligence improves planning",
                                 "Evidence supports informed decisions",
                             ]}]}
    watsonx = _Watsonx([invalid, repaired])
    pipeline = GenerationPipeline(None, watsonx, tmp_path, tmp_path)
    content, warnings, calls = pipeline._generate_validated(
        model_id="mock", template=_semantic_template(), message_args=_args())
    assert calls == watsonx.calls == 2
    assert content.slides[0].targets[1].bullets == repaired["slides"][0]["bullets"]
    assert any("focused semantic repair" in warning for warning in warnings)
    repair_text = watsonx.messages[1][-1]["content"]
    assert "Never split text into characters" in repair_text
    assert "source_excerpt" in watsonx.messages[1][1]["content"]


def test_failed_bullet_repair_returns_friendly_error(tmp_path):
    invalid = {"slides": [{"slide_number": 1, "section_type": "introduction",
                            "title": "Introduction", "bullets": ["A", "H", "C", "P"]}]}
    watsonx = _Watsonx([invalid, invalid])
    pipeline = GenerationPipeline(None, watsonx, tmp_path, tmp_path)
    with pytest.raises(ResponseValidationError) as captured:
        pipeline._generate_validated(model_id="mock", template=_semantic_template(), message_args=_args())
    message = str(captured.value)
    assert "could not generate complete, valid bullet content for slide 1" in message
    assert "pydantic.dev" not in message
    assert watsonx.calls == 2


def test_normalization_and_explicit_short_role_policy():
    assert normalize_bullets("AI improves waste collection") == ["AI improves waste collection"]
    assert normalize_bullets("First point\nSecond point") == ["First point", "Second point"]
    assert normalize_bullets(["First point", "Second point", "First point"]) == ["First point", "Second point"]
    assert normalize_bullets("A", role="metric") == ["A"]
    with pytest.raises(BulletNormalizationError):
        normalize_bullets(["A", "H", "C", "P"], role="body")
    with pytest.raises(BulletNormalizationError):
        normalize_bullets({"bullet": "unsafe"})


def test_target_model_never_accepts_character_list():
    with pytest.raises(ValidationError):
        TargetContent(target_kind="shape", target_id=6, content_type="bullets",
                      bullets=["A", "H", "C", "P"])
