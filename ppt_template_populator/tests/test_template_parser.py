from pathlib import Path
import pytest
from src.security import SecurityError, sanitize_filename, validate_pptx
from src.template_indexer import build_document
from src.template_parser import parse_template


def test_valid_ppt_parsing(sample_pptx: Path):
    result = parse_template(sample_pptx, file_path="templates/sample.pptx")
    assert result["slide_count"] == 1
    assert result["slides"][0]["slide_number"] == 1
    placeholder = next(s for s in result["slides"][0]["shapes"] if s["is_placeholder"])
    assert isinstance(placeholder["placeholder_idx"], int)
    assert placeholder["shape_id"] != 0


def test_invalid_non_ppt_upload(tmp_path: Path):
    path = tmp_path / "bad.pptx"; path.write_text("not a pptx", encoding="utf-8")
    with pytest.raises(SecurityError): validate_pptx(path, 1000)


def test_document_construction(indexed_template):
    document = build_document({**indexed_template, "unsafe": "ignored"})
    assert "unsafe" not in document
    assert document["template_id"] == "template-1"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("../../report.pptx", "report.pptx"),
        (r"..\..\report.pptx", "report.pptx"),
        ("Q3: Sales?.pptx", "Q3_Sales_.pptx"),
        (".pptx", "template.pptx"),
        ("Deck.PPTX", "Deck.pptx"),
        ("deck.pptx.pptx", "deck.pptx"),
        ("folder/board<>review*.pptx", "board_review_.pptx"),
    ],
)
def test_filename_sanitization(name, expected): assert sanitize_filename(name) == expected
