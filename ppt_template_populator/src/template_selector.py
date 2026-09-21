from __future__ import annotations
import json
from typing import Any
from pydantic import ValidationError
from .prompt_builder import build_repair_messages
from .response_models import TemplateSelection
from .response_validator import ResponseValidationError, extract_json

SELECTION_SYSTEM_PROMPT = "You select the best PowerPoint template from supplied candidates. Return only valid JSON matching the schema. Never invent a template ID. Do not request or discuss PPT binary data."

# Deliberately a whitelist, not a blacklist of the two huge binary/embedding
# fields: a candidate's full "slides" tree (every shape's position, font
# size, existing_text, classification metadata, ...) can run tens of
# thousands of tokens per template. With several sizeable candidates in the
# same request that blew past the model's context window (156k tokens
# against a 131k limit, once the template pool grew past a single entry).
# template_profile already exists specifically as a compact, LLM-ready
# summary of a template -- that plus these light fields is everything
# selection needs.
SELECTION_SUMMARY_FIELDS = {
    "template_id", "template_name", "description", "category", "use_cases", "target_audiences",
    "tones", "visual_style", "supported_sections", "slide_count", "has_image_placeholders",
    "has_chart_placeholders", "template_profile", "retrieval_score", "requirement_match_score",
    "rerank_score", "matched_requirements", "unmatched_requirements",
}


def extract_selected_template_id(selection: TemplateSelection) -> str:
    if not isinstance(selection, TemplateSelection):
        raise TypeError("Template selection must be a validated TemplateSelection object.")
    template_id = selection.selected_template_id
    if not isinstance(template_id, str) or not template_id.strip():
        raise TypeError("Selected template ID must be a non-empty string.")
    return template_id.strip()


def build_selection_messages(requirements: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
    safe = [{key: value for key, value in item.items() if key in SELECTION_SUMMARY_FIELDS} for item in candidates]
    payload = {"user_requirements": requirements, "candidates": safe, "required_schema": TemplateSelection.model_json_schema()}
    return [{"role": "system", "content": SELECTION_SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(payload, ensure_ascii=True)}]


def validate_selection(raw: str | dict[str, Any], candidate_ids: set[str]) -> TemplateSelection:
    data = extract_json(raw) if isinstance(raw, str) else raw
    try: selection = TemplateSelection.model_validate(data)
    except ValidationError as exc: raise ResponseValidationError([item["msg"] for item in exc.errors()]) from exc
    if selection.selected_template_id not in candidate_ids: raise ResponseValidationError(["The model selected a template ID outside the candidate list."])
    return selection


def select_template(watsonx: Any, model_id: str, requirements: dict[str, Any], candidates: list[dict[str, Any]]) -> tuple[TemplateSelection, bool]:
    if not candidates: raise ValueError("No compatible templates were found.")
    messages = build_selection_messages(requirements, candidates); ids = {item["template_id"] for item in candidates}
    response = watsonx.chat(model_id, messages, max_tokens=1000, temperature=0)
    try: return validate_selection(response.text, ids), False
    except ResponseValidationError as first_error:
        repaired = watsonx.chat(model_id, build_repair_messages(messages, response.text, first_error.errors, response_schema=TemplateSelection.model_json_schema()), max_tokens=1000, temperature=0)
        try: return validate_selection(repaired.text, ids), True
        except ResponseValidationError:
            best = candidates[0]
            return TemplateSelection(selected_template_id=best["template_id"], confidence=min(1.0, max(0.0, best.get("requirement_match_score", 0.0))), reason="The model selection was invalid after one repair; the highest-scoring Elasticsearch candidate was used.", matched_requirements=best.get("matched_requirements", []), unmatched_requirements=best.get("unmatched_requirements", [])), True
