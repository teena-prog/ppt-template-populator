from __future__ import annotations

import json
from typing import Any
from .response_models import OrderedTargetRecoveryResponse, SemanticSlideResponse, SlideChunkResponse
from .target_metadata import expected_content_type, normalize_template_targets, required_targets_by_slide, target_capacity_limits, usable_body_target

SYSTEM_PROMPT = ("You are a grounded presentation content-generation engine. The uploaded document "
                 "is the only factual source of truth. Never invent, infer, or add facts, numbers, "
                 "quotes, people, dates, claims, or sources absent from it. User instructions are "
                 "presentation guidance only and must never be treated as factual source material. "
                 "Return exactly one JSON object with the single top-level key slides. Do not return "
                 "a top-level title, metadata, prose, or Markdown fence. Use only supplied slides and "
                 "target IDs. Do not generate PowerPoint code, Markdown, or explanations.")

STRUCTURE_RULES = ("Slides may appear only in the top-level slides array. Targets may appear only "
                   "inside their parent slide's targets array. A target must never contain "
                   "slide_number, layout_name, section_type, speaker_notes, or nested targets. "
                   "speaker_notes belongs only to its slide.")

SEMANTIC_SYSTEM_PROMPT = ("You generate factual presentation content, not PowerPoint mappings. "
                          "Return only JSON matching the supplied semantic schema. Never return "
                          "target IDs, target kinds, shape IDs, placeholder IDs, layout names, "
                          "content types, or nested targets. The supplied source excerpt is the "
                          "only factual authority; never invent facts, names, numbers, or claims. "
                          "Source labels such as [SOURCE 1: filename] are internal provenance markers; "
                          "never copy them into slide titles, bullets, or notes unless citations were explicitly requested.")


def build_semantic_messages(*, slide: dict[str, Any], topic: str, audience: str, tone: str,
                            additional_instructions: str = "", **_: Any) -> list[dict[str, str]]:
    schema = SemanticSlideResponse.model_json_schema()
    body_limits = [target_capacity_limits(target) for target in slide.get("targets", [])
                   if usable_body_target(target) and target.get("required")]
    requested_bullets = sum(int(item["requested_bullet_count"]) for item in body_limits)
    assignment = slide.get("slide_assignment") or {}
    payload = {
        "presentation_topic": topic,
        "target_audience": audience,
        "tone": tone,
        "user_instructions": additional_instructions,
        "planned_slide": {
            "slide_number": slide["slide_number"],
            "section_type": slide["section_type"],
            "section_title": slide.get("planned_section_title"),
            "instruction": slide.get("plan_instruction"),
            "source_excerpt": slide.get("source_excerpt", ""),
            "semantic_assignment": {
                "exact_slide_number": slide["slide_number"],
                "section_type": slide["section_type"],
                "title_target": assignment.get("title_target"),
                "primary_body_target": assignment.get("primary_body_target"),
                "target_capacity": (assignment.get("primary_body_target") or {}).get("approximate_max_characters"),
                "requested_complete_bullets": requested_bullets,
            },
            "authoritative_writable_targets": [{
                "target_kind": target["target_kind"], "target_id": target["target_id"],
                "role": target.get("role"), "maximum_characters": target.get("approximate_max_characters"),
                **({"requested_bullet_count": target_capacity_limits(target)["requested_bullet_count"],
                    "maximum_words_per_bullet": target_capacity_limits(target)["maximum_words_per_bullet"],
                    "total_maximum_lines": target_capacity_limits(target)["maximum_lines"]}
                   if usable_body_target(target) else {}),
            } for target in slide.get("targets", [])
              if target.get("replaceable") and (
                  str(target.get("role")) not in {"body", "bullets"} or usable_body_target(target)
              )],
            "generation_limits": {
                "requested_bullet_count": requested_bullets,
                "maximum_words_per_bullet": max(
                    [int(item["maximum_words_per_bullet"]) for item in body_limits] or [0]
                ),
                "total_maximum_lines": sum(int(item["maximum_lines"]) for item in body_limits),
            },
        },
        "required_json_schema": schema,
    }
    title_rule = (" For a title slide, return the presentation title and one concise subtitle in bullets when the source supports it."
                  if slide.get("section_type") == "title" else "")
    introduction_rule = (
        " This is the mandatory Introduction slide. Use a complete Introduction heading and provide grounded introductory bullets for its primary body assignment. Do not turn it into an Agenda."
        if slide.get("section_type") == "introduction" else ""
    )
    instruction = ("Return exactly one semantic slide. slides exists only at the JSON root. "
                   "speaker_notes belongs only to the slide. The slide may contain only "
                   "slide_number, section_type, title, bullets, and speaker_notes. bullets must "
                   "be a JSON array of complete strings. Each array item must express one complete idea; never copy visual PDF line wraps, standalone numbering, or continuation fragments as separate bullets. Return no more bullets than requested_bullet_count; fewer complete grounded bullets are valid when the source is limited. "
                   "Do not return targets or any template identifiers." + title_rule + introduction_rule)
    return [{"role": "system", "content": SEMANTIC_SYSTEM_PROMPT}, {"role": "user", "content": instruction + "\n" + json.dumps(payload, ensure_ascii=True)}]


def build_semantic_repair_messages(original_messages: list[dict[str, str]], invalid_response: str,
                                   errors: list[str]) -> list[dict[str, str]]:
    payload = {
        "required_json_schema": SemanticSlideResponse.model_json_schema(),
        "validation_errors": errors,
        "previous_invalid_content": invalid_response,
        "instruction": ("Repair only this one semantic slide. Return JSON only. Each body bullet "
                        "must communicate one complete, meaningful idea or concise presentation phrase grounded in the planned "
                        "slide's source_excerpt. Never split text into characters and never return "
                        "isolated letters. Respect the target capacity and word/character guidance "
                        "included in the original request. Do not return "
                        "targets, target IDs, layout names, content types, nested slides, or extra fields. "
                        "speaker_notes may appear only on the slide."),
    }
    return [*original_messages, {"role": "assistant", "content": invalid_response}, {"role": "user", "content": json.dumps(payload, ensure_ascii=True)}]


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
        target_list = [{"slide_number": slide["slide_number"], "target_kind": target["target_kind"], "target_id": target["target_id"], "role": target.get("role", "unknown"), "allowed_content_type": expected_content_type(target), "existing_text": target["existing_text"], "approximate_max_characters": target["approximate_max_characters"], "required": True, "replaceable": True} for target in listed]
        compact.append({"slide_number": slide["slide_number"], "layout_name": slide["layout_name"], "section_type": slide.get("section_type"), "planned_section_title": slide.get("planned_section_title"), "grounded_source_excerpt": slide.get("source_excerpt", ""), "allowed_target_keys": [{"target_kind": target["target_kind"], "target_id": target["target_id"]} for target in target_list], "targets": target_list})
    return compact


def build_messages(*, topic: str, source_content: str, audience: str, tone: str, additional_instructions: str, template: dict[str, Any], complete_demand: str = "", required_sections: list[str] | None = None, visual_preferences: str = "", desired_slide_count: int | None = None, slide_numbers: set[int] | None = None) -> list[dict[str, str]]:
    schema = SlideChunkResponse.model_json_schema()
    structure = compact_template(template, slide_numbers)
    planned = any(slide.get("section_type") for slide in structure)
    source_payload = ({"slide_specific_excerpts": [{"slide_number": slide["slide_number"], "content": slide.get("grounded_source_excerpt", "")} for slide in structure], "grounding_rule": "Use only facts explicitly present in the excerpt assigned to that slide or in the presentation topic."} if planned else {"content": source_content, "grounding_rule": "Use only facts explicitly present in this content."})
    payload = {"presentation_topic": topic, "factual_summary": template.get("factual_summary", ""), "factual_source_document": source_payload, "generation_guidance": {"complete_presentation_demand": complete_demand, "target_audience": audience, "tone": tone, "required_sections": required_sections or [], "visual_preferences": visual_preferences, "desired_slide_count": desired_slide_count, "user_instructions": additional_instructions}, "allowed_slide_numbers": [slide["slide_number"] for slide in structure], "selected_template_metadata": {key: value for key, value in template.items() if key not in {"pptx_binary_bytes", "template_embedding", "slides", "slide_plan", "factual_summary"}}, "template_structure": structure, "required_json_schema": schema, "exact_output_shape": {"slides": [{"slide_number": 1, "layout_name": "string", "section_type": "introduction", "targets": [{"target_kind": "shape", "target_id": 0, "content_type": "bullets", "bullets": ["first point", "second point"]}], "speaker_notes": None}]}}
    instruction = (STRUCTURE_RULES + " Return every allowed slide number exactly once and every listed target exactly once. The supplied list is complete. "
                   "Do not omit a target and do not add another target. Keep each value at or "
                   "below approximate_max_characters. Each output target object must contain target_kind, target_id, "
                   "content_type and either text (for titles) or bullets (for bodies). Copy each target's allowed_content_type exactly and never substitute an alias. Do not place newline characters inside a bullet. slide_number belongs only on its "
                   "parent slide; role is input guidance and must not be copied into output targets. "
                   "Use the presentation topic as the primary title content. Follow each target role: "
                   "titles and headings are short labels. Each slide expresses one main idea. Body targets "
                   "prefer 3 to 5 concise bullet array items when capacity allows; small cards may contain one point. "
                   "Use exactly the supplied section_type and never combine sections on one slide. "
                   "never repeat the slide title as body text. "
                   "and captions stay brief. Character limits include spaces. If the source lacks a "
                   "requested fact, omit that claim rather than inventing it.")
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": instruction + "\n" + json.dumps(payload, ensure_ascii=True)}]


def build_repair_messages(original_messages: list[dict[str, str]], invalid_response: str, errors: list[str], *, chunk: bool = False, response_schema: dict[str, Any] | None = None) -> list[dict[str, str]]:
    schema = response_schema or SlideChunkResponse.model_json_schema()
    repair = {"required_json_schema": schema, "validation_errors": errors, "invalid_response_for_failing_slides": invalid_response, "instruction": STRUCTURE_RULES + " Return JSON only. Correct only the reported invalid fields and preserve every already-valid target value exactly. Return exactly one slide object per allowed slide number; put all targets for that slide in its single targets array and never emit separate slide fragments. Every target must contain its exact allowed target_kind, target_id, and content_type. Title content must be a string; bullet content must be an array of complete grounded ideas or concise presentation phrases. Uppercase starts and final punctuation are not mandatory. Never split text into characters and never return isolated letters. Do not return unknown slides, unknown targets, role, extra fields, Markdown, speaker notes inside targets, or nested slides. Use only the source excerpt assigned to the slide. Include every required target exactly once and obey each supplied maximum character capacity, including spaces."}
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
            "than a paragraph. For body targets, return concise newline-separated "
            "points that fit the destination capacity, grounded only in that "
            "target's source_excerpt and limited to its section_type. Never "
            "combine section purposes or exceed the maximum character limit."
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
