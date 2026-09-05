from __future__ import annotations

import json
from typing import Any
from .response_models import OrderedTargetRecoveryResponse, PresentationContent, SlideChunkResponse
from .target_metadata import normalize_template_targets, required_targets_by_slide

SYSTEM_PROMPT = "You are a presentation content-generation engine. Return only valid JSON that matches the provided schema. Use only the slides and target IDs present in the supplied template. Keep the content concise enough to fit its destination. Do not generate PowerPoint code, Markdown or additional explanations."


def compact_template(template: dict[str, Any], slide_numbers: set[int] | None = None) -> list[dict[str, Any]]:
    template = normalize_template_targets(template)
    required = required_targets_by_slide(template)
    compact = []
    for slide in template["slides"]:
        if slide_numbers is not None and slide["slide_number"] not in slide_numbers: continue
        targets = slide.get("targets")
        if targets is None:
            targets = [{"target_kind": "placeholder", "target_id": shape["placeholder_idx"], "role": (shape.get("placeholder_type") or "unknown").lower(), "max_content_length": shape["max_content_length"], "existing_text": shape["existing_text"]} for shape in slide["shapes"] if shape["is_placeholder"] and shape["has_text_frame"] and shape["placeholder_idx"] is not None]
        listed = required.get(slide["slide_number"], {}).values()
        compact.append({"slide_number": slide["slide_number"], "layout_name": slide["layout_name"], "targets": [{"slide_number": slide["slide_number"], "target_kind": target["target_kind"], "target_id": target["target_id"], "role": target.get("role", "unknown"), "existing_text": target["existing_text"], "approximate_max_characters": target["approximate_max_characters"], "required": True, "replaceable": True} for target in listed]})
    return compact


def build_messages(*, topic: str, source_content: str, audience: str, tone: str, additional_instructions: str, template: dict[str, Any], complete_demand: str = "", required_sections: list[str] | None = None, visual_preferences: str = "", desired_slide_count: int | None = None, slide_numbers: set[int] | None = None) -> list[dict[str, str]]:
    schema = SlideChunkResponse.model_json_schema() if slide_numbers is not None else PresentationContent.model_json_schema()
    payload = {"topic": topic, "complete_presentation_demand": complete_demand, "source_content": source_content, "target_audience": audience, "tone": tone, "required_sections": required_sections or [], "visual_preferences": visual_preferences, "desired_slide_count": desired_slide_count, "additional_instructions": additional_instructions, "selected_template_metadata": {key: value for key, value in template.items() if key not in {"pptx_binary_bytes", "template_embedding", "slides"}}, "template_structure": compact_template(template, slide_numbers), "required_json_schema": schema}
    instruction = "Return every listed target exactly once. The supplied list is complete. Do not omit a target and do not add another target. Keep each value at or below approximate_max_characters."
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": instruction + "\n" + json.dumps(payload, ensure_ascii=True)}]


def build_repair_messages(original_messages: list[dict[str, str]], invalid_response: str, errors: list[str], *, chunk: bool = False) -> list[dict[str, str]]:
    schema = SlideChunkResponse.model_json_schema() if chunk else PresentationContent.model_json_schema()
    repair = {"required_json_schema": schema, "validation_errors": errors, "invalid_response_for_failing_slides": invalid_response, "instruction": "Return only corrected JSON. Include every required target exactly once and obey each exact character maximum."}
    return [*original_messages, {"role": "assistant", "content": invalid_response}, {"role": "user", "content": json.dumps(repair, ensure_ascii=True)}]


def build_missing_target_recovery_messages(
    missing_targets: list[dict[str, Any]],
    context: dict[str, Any],
) -> list[dict[str, str]]:
    payload = {
        "presentation_context": context,
        "ordered_targets": [
            {"slot": index, **target}
            for index, target in enumerate(missing_targets, start=1)
        ],
        "required_json_schema": (
            OrderedTargetRecoveryResponse.model_json_schema()
        ),
        "instruction": (
            "Return JSON only with one key named contents. contents must be "
            "an array containing exactly one string for every ordered target, "
            "in the same slot order. Do not return slide_number, target_kind "
            "or target_id; Python maps slots to those IDs. Respect each "
            "maximum_characters exactly; spaces and punctuation count. "
            "For title and heading targets, generate a short label rather "
            "than a paragraph. For body targets, generate concise explanatory "
            "content. Never exceed the maximum character limit."
        ),
    }

    return [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=True),
        },
    ]
