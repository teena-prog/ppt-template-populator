import json

from src.prompt_builder import build_messages


def _template():
    return {
        "template_id": "one",
        "slides": [{
            "slide_number": 1,
            "layout_name": "Blank",
            "targets": [{
                "slide_number": 1, "target_kind": "shape", "target_id": 7,
                "role": "title", "existing_text": "Old title",
                "approximate_max_characters": 60, "required": True,
                "replaceable": True,
            }],
        }],
    }


def test_prompt_separates_facts_from_user_guidance_and_keeps_exact_schema():
    messages = build_messages(
        topic="Grounded title", source_content="Verified fact A.",
        audience="Leaders", tone="Formal",
        additional_instructions="Exclude technical detail.", template=_template(),
        slide_numbers={1},
    )
    payload = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert payload["factual_source_document"]["content"] == "Verified fact A."
    assert payload["generation_guidance"]["user_instructions"] == "Exclude technical detail."
    assert "only factual source" in messages[0]["content"].lower()
    target_schema = payload["required_json_schema"]["$defs"]["TargetContent"]
    assert set(target_schema["required"]) == {"target_kind", "target_id", "content_type"}
    assert {"text", "bullets"}.issubset(target_schema["properties"])


def test_prompt_exposes_role_and_exact_limit_only_as_input_metadata():
    messages = build_messages(
        topic="Title", source_content="Fact", audience="All", tone="Clear",
        additional_instructions="", template=_template(), slide_numbers={1},
    )
    payload = json.loads(messages[1]["content"].split("\n", 1)[1])
    target = payload["template_structure"][0]["targets"][0]
    assert target["role"] == "title"
    assert target["allowed_content_type"] == "title"
    assert target["approximate_max_characters"] == 60
    assert payload["template_structure"][0]["allowed_target_keys"] == [
        {"target_kind": "shape", "target_id": 7}
    ]
    assert "content_type and either text (for titles) or bullets (for bodies)" in messages[1]["content"]
