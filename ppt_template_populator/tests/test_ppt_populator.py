from pathlib import Path
from pptx import Presentation
from src.ppt_populator import populate_presentation
from src.response_validator import validate_response


def test_powerpoint_output_generation(sample_pptx: Path, template_metadata, valid_payload, tmp_path: Path):
    content, _ = validate_response(valid_payload, template_metadata)
    output, warnings = populate_presentation(sample_pptx, content, tmp_path / "generated")
    assert output.exists() and output.name.startswith("presentation_")
    prs = Presentation(output)
    for item in content.slides[0].placeholders:
        shape = next(s for s in prs.slides[0].shapes if s.is_placeholder and s.placeholder_format.idx == item.placeholder_id)
        assert shape.text == item.content
    assert warnings == []
