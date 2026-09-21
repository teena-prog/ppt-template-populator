from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
import re
from typing import Iterable
from uuid import uuid4

from .document_extractor import DocumentExtractionError, ExtractedDocument, extract_text

ALLOWED_SOURCE_EXTENSIONS = {".pdf", ".docx", ".txt"}
ALLOWED_MIME_TYPES = {
    ".pdf": {"application/pdf", "application/x-pdf", "application/octet-stream"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/zip", "application/octet-stream"},
    ".txt": {"text/plain", "application/octet-stream"},
}


@dataclass(frozen=True)
class SourceUpload:
    filename: str
    data: bytes
    mime_type: str = "application/octet-stream"


@dataclass
class SourceRecord:
    source_id: str
    filename: str
    checksum: str
    extracted_text: str
    metadata: dict[str, int | str]
    upload_order: int
    extraction_status: str
    size_bytes: int
    duplicate: bool = False
    error: str | None = None


@dataclass
class SourceBatch:
    records: list[SourceRecord]
    combined_text: str
    title: str
    warnings: list[str] = field(default_factory=list)
    payloads: dict[str, bytes] = field(default_factory=dict, repr=False)
    total_size_bytes: int = 0
    batch_error: str | None = None

    @property
    def valid_records(self) -> list[SourceRecord]:
        return [record for record in self.records if record.extraction_status == "valid" and not record.duplicate]


def _safe_error(filename: str, message: str) -> str:
    return f"{filename}: {message}"


def _metadata(filename: str, extracted: ExtractedDocument) -> dict[str, int | str]:
    suffix = Path(filename).suffix.lower()
    metadata: dict[str, int | str] = {"format": suffix.lstrip(".")}
    if suffix == ".pdf":
        metadata["page_count"] = max(1, len(re.findall(r"\n\s*\n", extracted.text)) + 1)
    elif suffix == ".docx":
        metadata["section_count"] = max(1, len([line for line in extracted.text.splitlines() if line.strip()]))
    return metadata


def _paragraphs(text: str) -> list[str]:
    blocks = re.split(r"\n\s*\n", text)
    if len(blocks) == 1:
        blocks = text.splitlines()
    return [re.sub(r"\s+", " ", block).strip() for block in blocks if block.strip()]


def _deduplicate_records(records: Iterable[SourceRecord]) -> dict[str, list[str]]:
    seen: set[str] = set()
    result: dict[str, list[str]] = {}
    for record in records:
        unique: list[str] = []
        for paragraph in _paragraphs(record.extracted_text):
            key = re.sub(r"[^a-z0-9]+", " ", paragraph.casefold()).strip()
            if key and key not in seen:
                seen.add(key)
                unique.append(paragraph)
        result[record.source_id] = unique
    return result


def _unrelated_warning(records: list[SourceRecord]) -> str | None:
    stop = {"about", "after", "also", "from", "have", "into", "more", "that", "their", "these", "this", "using", "with", "document", "report"}
    sets = []
    for record in records:
        terms = {word for word in re.findall(r"[a-z0-9]{4,}", f"{record.filename} {record.extracted_text[:5000]}".casefold()) if word not in stop}
        sets.append(terms)
    for index, left in enumerate(sets):
        for right in sets[index + 1:]:
            if len(left) >= 4 and len(right) >= 4 and not left.intersection(right):
                return "Some uploaded documents appear to cover unrelated topics. Confirm that they should be combined before generation."
    return None


def process_source_batch(uploads: list[SourceUpload], *, max_file_bytes: int, max_total_bytes: int) -> SourceBatch:
    total = sum(len(upload.data) for upload in uploads)
    if total > max_total_bytes:
        message = f"The combined upload exceeds the {max_total_bytes // (1024 * 1024)} MB batch limit."
        records = [SourceRecord(
            source_id=f"source-{order}-{uuid4().hex[:8]}", filename=Path(upload.filename).name,
            checksum=sha256(upload.data).hexdigest(), extracted_text="",
            metadata={"format": Path(upload.filename).suffix.lower().lstrip(".")}, upload_order=order,
            extraction_status="invalid", size_bytes=len(upload.data), error=message,
        ) for order, upload in enumerate(uploads, start=1)]
        return SourceBatch(records, "", "", total_size_bytes=total, batch_error=message)
    records: list[SourceRecord] = []
    payloads: dict[str, bytes] = {}
    checksums: set[str] = set()
    titles: list[str] = []
    for order, upload in enumerate(uploads, start=1):
        source_id = f"source-{order}-{uuid4().hex[:8]}"
        checksum = sha256(upload.data).hexdigest()
        suffix = Path(upload.filename).suffix.lower()
        base = dict(source_id=source_id, filename=Path(upload.filename).name, checksum=checksum, extracted_text="", metadata={"format": suffix.lstrip(".")}, upload_order=order, size_bytes=len(upload.data))
        error = None
        if suffix not in ALLOWED_SOURCE_EXTENSIONS:
            error = "Unsupported file type. Upload PDF, DOCX or TXT."
        elif upload.mime_type and upload.mime_type.casefold() not in ALLOWED_MIME_TYPES[suffix]:
            error = "The reported file type does not match its extension."
        elif not upload.data:
            error = "The file is empty."
        elif len(upload.data) > max_file_bytes:
            error = f"The file exceeds the {max_file_bytes // (1024 * 1024)} MB per-file limit."
        elif checksum in checksums:
            records.append(SourceRecord(**base, extraction_status="duplicate", duplicate=True, error="Duplicate file content."))
            continue
        if error:
            records.append(SourceRecord(**base, extraction_status="invalid", error=error))
            continue
        checksums.add(checksum)
        try:
            extracted = extract_text(upload.data, upload.filename)
            records.append(SourceRecord(**{**base, "extracted_text": extracted.text, "metadata": _metadata(upload.filename, extracted)}, extraction_status="valid"))
            payloads[source_id] = upload.data
            titles.append(extracted.title)
        except DocumentExtractionError as exc:
            records.append(SourceRecord(**base, extraction_status="failed", error=str(exc)))
    valid = [record for record in records if record.extraction_status == "valid"]
    deduplicated = _deduplicate_records(valid)
    sections = [f"[SOURCE {record.upload_order}: {record.filename}]\n" + "\n\n".join(deduplicated[record.source_id]) for record in valid]
    warning = _unrelated_warning(valid)
    warnings = [warning] if warning else []
    return SourceBatch(records, "\n\n".join(sections), titles[0] if titles else "", warnings, payloads, total)
