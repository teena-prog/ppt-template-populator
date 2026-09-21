import json
import logging
from types import SimpleNamespace

import pytest

from src.pipeline import GenerationPipeline
from src.response_models import RecoveredTargetContent, SlideContent, TargetContent
from src.response_validator import (
    ResponseValidationError, merge_recovered_targets,
    normalize_model_response, validate_chunk_response,
)


def _target(target_id, role="body", limit=250):
    return {
        "slide_number": 12, "target_kind": "shape", "target_id": target_id,
        "role": role, "existing_text": role, "approximate_max_characters": limit,
        "max_content_length": limit, "required": True, "replaceable": True,
    }


def _template():
    return {"template_id": "deck", "slide_count": 1, "slides": [{
        "slide_number": 12, "layout_name": "Blank",
        "targets": [_target(120, "title", 60), _target(121)],
    }]}


def _fragment(target_id, content):
    return {"slide_number": 12, "layout_name": "Blank", "targets": [
        {"target_kind": "shape", "target_id": target_id, "content": content}
    ]}


def test_duplicate_slide_fragments_with_different_targets_merge():
    raw = {"slides": [_fragment(120, "Title"), _fragment(121, "Body detail")]}
    normalized = normalize_model_response(raw, expected_root="slides")
    assert len(normalized["slides"]) == 1
    content, _ = validate_chunk_response(normalized, _template())
    assert {(item.target_kind, item.target_id) for item in content.slides[0].targets} == {
        ("shape", 120), ("shape", 121)
    }


def test_identical_target_duplicates_are_idempotent():
    fragment = _fragment(120, "Title")
    normalized = normalize_model_response({"slides": [fragment, fragment, _fragment(121, "Body")]}, expected_root="slides")
    assert len(normalized["slides"]) == 1
    assert len(normalized["slides"][0]["targets"]) == 2


def test_conflicting_duplicate_target_content_is_rejected():
    with pytest.raises(ResponseValidationError, match="conflicting duplicate target content"):
        normalize_model_response({"slides": [
            _fragment(120, "First"), _fragment(120, "Different")
        ]}, expected_root="slides")


def test_repeated_slide_12_fragments_produce_one_final_slide():
    raw = json.dumps({"slides": [
        _fragment(120, "Slide 12 title"),
        _fragment(121, "Slide 12 body"),
        _fragment(121, "Slide 12 body"),
    ]})
    chunk, _ = validate_chunk_response(raw, _template())
    assert [slide.slide_number for slide in chunk.slides] == [12]
    assert len(chunk.slides[0].targets) == 2


def test_recovery_replaces_only_requested_target_key():
    slides = [SlideContent(slide_number=12, layout_name="Blank", targets=[
        TargetContent(target_kind="shape", target_id=120, content="Keep"),
        TargetContent(target_kind="shape", target_id=121, content="Invalid old value"),
    ])]
    recovered = [RecoveredTargetContent(
        slide_number=12, target_kind="shape", target_id=121, content="Repaired"
    )]
    merged = merge_recovered_targets(slides, recovered, {(12, "shape", 121)})
    values = {item.target_id: item.content for item in merged[0].targets}
    assert values == {120: "Keep", 121: "Repaired"}
    with pytest.raises(ResponseValidationError, match="unrequested"):
        merge_recovered_targets(slides, recovered, {(12, "shape", 120)})


def test_chunk_planner_is_non_overlapping_and_rejects_duplicate_template_slides(tmp_path):
    slides = [{"slide_number": number, "layout_name": "Blank", "targets": []}
              for number in range(1, 13)]
    pipeline = GenerationPipeline(None, None, tmp_path, tmp_path)
    chunks = pipeline._plan_chunks({"slides": slides})
    flattened = [number for chunk in chunks for number in chunk]
    assert flattened == list(range(1, 13)) and len(flattened) == len(set(flattened))
    with pytest.raises(ValueError, match="non-overlapping chunks"):
        pipeline._plan_chunks({"slides": [slides[0], slides[0]]})


class FragmentWatson:
    def chat(self, *_args, **_kwargs):
        return SimpleNamespace(text=json.dumps({"slides": [
            _fragment(120, "SECRET_FACT"), _fragment(121, "Grounded body")
        ]}))


def test_structural_boundary_logs_never_contain_generated_content(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="src.pipeline")
    pipeline = GenerationPipeline(None, FragmentWatson(), tmp_path, tmp_path)
    content, _, _ = pipeline._generate_validated(
        model_id="model", template=_template(),
        message_args={"topic": "Topic", "source_content": "PRIVATE_SOURCE",
                      "audience": "All", "tone": "Clear", "additional_instructions": ""},
    )
    assert len(content.slides) == 1
    logs = caplog.text
    assert "raw_generation" in logs and "normalized_generation" in logs
    assert "chunk_merge" in logs and "final_validation" in logs
    assert "SECRET_FACT" not in logs and "PRIVATE_SOURCE" not in logs
