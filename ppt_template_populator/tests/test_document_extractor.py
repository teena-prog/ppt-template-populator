import io
import pytest
from src.document_extractor import DocumentExtractionError, estimate_slide_count, extract_text


def _docx_bytes(paragraphs: list[str]) -> bytes:
    from docx import Document
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_extraction_uses_first_paragraph_as_title():
    data = _docx_bytes(["Quarterly Business Review", "Revenue grew 20% year over year.", "Customer churn declined."])
    result = extract_text(data, "report.docx")
    assert result.title == "Quarterly Business Review"
    assert "Revenue grew 20%" in result.text
    assert not result.warnings


def test_txt_extraction_falls_back_to_filename_title_when_first_line_used():
    data = b"Launch plan overview\nStep one: finalize scope.\nStep two: assign owners."
    result = extract_text(data, "notes.txt")
    assert result.title == "Launch plan overview"
    assert "Step two" in result.text


def test_unsupported_extension_is_rejected():
    with pytest.raises(DocumentExtractionError):
        extract_text(b"data", "archive.zip")


def test_empty_upload_is_rejected():
    with pytest.raises(DocumentExtractionError):
        extract_text(b"", "notes.txt")


def test_docx_with_no_text_raises_extraction_error():
    data = _docx_bytes([])
    with pytest.raises(DocumentExtractionError):
        extract_text(data, "empty.docx")


def test_estimate_slide_count_scales_with_word_count_within_bounds():
    short_text = "word " * 30
    long_text = "word " * 3000
    assert estimate_slide_count(short_text) == 6
    assert estimate_slide_count(long_text) == 20
