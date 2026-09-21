from __future__ import annotations

from typing import Any

FRIENDLY_STAGES = ("Reading document", "Planning slides", "Selecting template", "Generating content", "Building presentation", "Checking layout", "Ready")

def failure_message(stage: str) -> str:
    labels = {
        "Reading document": "Document extraction failed.",
        "Planning slides": "Presentation planning failed.",
        "Selecting template": "Template retrieval or selection failed.",
        "Generating content": "Generated content could not be validated.",
        "Building presentation": "The PowerPoint file could not be built.",
        "Checking layout": "The generated presentation did not pass layout checks.",
    }
    return labels.get(stage, "The presentation could not be generated.")

def generation_ready(*, services_ready: bool, document_ready: bool, selection_mode: str, template_id: str | None) -> bool:
    return bool(services_ready and document_ready and (selection_mode == "automatic" or template_id))

def sources_ready(batch: Any, unrelated_confirmed: bool) -> bool:
    return bool(batch and not batch.batch_error and batch.valid_records and (not batch.warnings or unrelated_confirmed))

def friendly_stage(internal_stage: str) -> str:
    value = internal_stage.casefold()
    if any(word in value for word in ("retriev", "candidate", "template selected")): return "Selecting template"
    if any(word in value for word in ("summary", "plan")): return "Planning slides"
    if any(word in value for word in ("prompt", "content generated", "model output")): return "Generating content"
    if any(word in value for word in ("populated", "output saved")): return "Building presentation"
    if any(word in value for word in ("layout", "visual validation")): return "Checking layout"
    if any(word in value for word in ("download ready", "temporary files cleaned")): return "Ready"
    return "Reading document"

def result_summary(result: Any, fallback_template_name: str = "Selected template") -> dict[str, Any]:
    template_name = fallback_template_name
    if result.selection is not None:
        selected = next((item for item in result.candidates if item.get("template_id") == result.selection.selected_template_id), None)
        if selected: template_name = selected.get("template_name") or template_name
    slides = sorted(result.content.slides, key=lambda slide: slide.slide_number)
    return {"template_name": template_name, "slide_count": len(slides), "sections": [slide.section_type or slide.layout_name for slide in slides]}
