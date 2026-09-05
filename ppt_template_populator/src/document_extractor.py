from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_EXTENSIONS = {".docx", ".pdf", ".txt", ".md", ".markdown", ".rtf", ".doc"}
MAX_TEXT_CHARACTERS = 60000


class DocumentExtractionError(ValueError):
    pass


@dataclass
class ExtractedDocument:
    title: str
    text: str
    warnings: list[str] = field(default_factory=list)


def _title_from_text(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return fallback


def _extract_docx(data: bytes) -> tuple[str, str]:
    from docx import Document
    document = Document(io.BytesIO(data))
    paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                paragraphs.append(" | ".join(cells))
    text = "\n".join(paragraphs)
    title = str(document.core_properties.title or "").strip()
    return title, text


def _extract_pdf(data: bytes) -> tuple[str, str]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n".join(page.strip() for page in pages if page.strip())
    title = ""
    try:
        title = str(reader.metadata.title or "").strip() if reader.metadata else ""
    except Exception:
        title = ""
    return title, text


def _extract_rtf(data: bytes) -> tuple[str, str]:
    from striprtf.striprtf import rtf_to_text
    raw = data.decode("utf-8", errors="ignore")
    return "", rtf_to_text(raw).strip()


def _extract_plain(data: bytes) -> tuple[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="ignore")
    return "", text.strip()


def _extract_legacy_doc(data: bytes) -> tuple[str, str, list[str]]:
    """Legacy binary .doc has no reliable pure-Python parser available offline;
    best-effort recovery of printable text runs so something usable comes through."""
    runs = re.findall(rb"[\x20-\x7E\t]{4,}", data)
    text = "\n".join(run.decode("ascii", errors="ignore").strip() for run in runs if run.strip())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    warning = "Legacy .doc extraction is best-effort; formatting and some text may be lost. Re-saving as .docx is recommended for best results."
    return "", text, [warning]


def extract_text(data: bytes, filename: str) -> ExtractedDocument:
    if not data:
        raise DocumentExtractionError("The uploaded file is empty.")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise DocumentExtractionError(f"Unsupported file type '{suffix or filename}'. Supported types: {', '.join(sorted(SUPPORTED_EXTENSIONS))}.")

    warnings: list[str] = []
    try:
        if suffix == ".docx":
            title, text = _extract_docx(data)
        elif suffix == ".pdf":
            title, text = _extract_pdf(data)
        elif suffix in (".txt", ".md", ".markdown"):
            title, text = _extract_plain(data)
        elif suffix == ".rtf":
            title, text = _extract_rtf(data)
        else:
            title, text, doc_warnings = _extract_legacy_doc(data)
            warnings.extend(doc_warnings)
    except DocumentExtractionError:
        raise
    except Exception as exc:
        raise DocumentExtractionError(f"The document could not be read ({type(exc).__name__}). It may be corrupted or password-protected.") from exc

    text = text.strip()
    if not text:
        raise DocumentExtractionError("No readable text could be extracted from the uploaded document.")
    if len(text) > MAX_TEXT_CHARACTERS:
        warnings.append(f"The document was truncated to {MAX_TEXT_CHARACTERS} characters for processing.")
        text = text[:MAX_TEXT_CHARACTERS]

    fallback_title = Path(filename).stem.replace("_", " ").replace("-", " ").strip() or "Presentation"
    title = title.strip() or _title_from_text(text, fallback_title)
    return ExtractedDocument(title=title[:120] or fallback_title, text=text, warnings=warnings)


def estimate_slide_count(text: str, minimum: int = 6, maximum: int = 20) -> int:
    """Rough heuristic so the user never has to specify a slide count: ~120 words per slide."""
    word_count = len(text.split())
    estimate = round(word_count / 120)
    return max(minimum, min(maximum, estimate)) if estimate else minimum
