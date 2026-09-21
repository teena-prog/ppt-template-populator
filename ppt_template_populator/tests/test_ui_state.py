from types import SimpleNamespace
from src.ui_state import failure_message, friendly_stage, generation_ready, result_summary

def test_frontend_generation_readiness_requires_manual_template():
    assert generation_ready(services_ready=True, document_ready=True, selection_mode="automatic", template_id=None)
    assert not generation_ready(services_ready=True, document_ready=True, selection_mode="manual", template_id=None)
    assert generation_ready(services_ready=True, document_ready=True, selection_mode="manual", template_id="template-1")

def test_frontend_uses_friendly_pipeline_stages():
    assert friendly_stage("Candidate templates retrieved") == "Selecting template"
    assert friendly_stage("14-slide plan constructed") == "Planning slides"
    assert friendly_stage("PowerPoint populated") == "Building presentation"
    assert friendly_stage("Visual validation completed") == "Checking layout"
    assert failure_message("Generating content") == "Generated content could not be validated."

def test_result_summary_is_ordered_and_uses_selected_template():
    slides = [SimpleNamespace(slide_number=2, section_type="introduction", layout_name="Body"), SimpleNamespace(slide_number=1, section_type="title", layout_name="Cover")]
    result = SimpleNamespace(content=SimpleNamespace(slides=slides), selection=SimpleNamespace(selected_template_id="t1"), candidates=[{"template_id": "t1", "template_name": "Professional"}])
    assert result_summary(result) == {"template_name": "Professional", "slide_count": 2, "sections": ["title", "introduction"]}
