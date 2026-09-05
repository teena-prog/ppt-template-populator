from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4


class SecurityError(ValueError):
    """An unsafe file or path was rejected."""


_SECRETS = re.compile(r"(?i)(api[_-]?key|password|token|authorization)(\s*[:=]\s*)([^\s,;]+)")
_BEARER = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]+)")


def mask_secrets(value: object) -> str:
    text = _SECRETS.sub(r"\1\2[REDACTED]", str(value))
    return _BEARER.sub(r"\1[REDACTED]", text)


def safe_user_error(error: Exception, fallback: str = "The operation could not be completed.") -> str:
    safe = mask_secrets(error)
    return safe[:500] if safe and not isinstance(error, (KeyError, AttributeError)) else fallback


def sanitize_filename(name: str, default: str = "template") -> str:
    basename = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename
    while stem.lower().endswith(".pptx"):
        stem = stem[:-5]

    # Replace Windows-invalid characters, controls, and whitespace as groups.
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*\s]+', "_", stem)
    cleaned = cleaned.strip(".")[:80]
    return f"{cleaned or default}.pptx"


def sanitize_upload_filename(name: str, default: str = "document") -> str:
    basename = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(basename).suffix.lower()
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*\s]+', "_", Path(basename).stem)
    cleaned = cleaned.strip(".")[:80]
    return f"{cleaned or default}{suffix}"


def confined_path(root: Path, relative_path: str | Path) -> Path:
    root_resolved = root.resolve()
    candidate = (root_resolved / relative_path).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise SecurityError("The requested path is outside the allowed directory.")
    return candidate


def validate_pptx(path: Path, max_bytes: int) -> None:
    if path.suffix.lower() != ".pptx":
        raise SecurityError("Only .pptx files are accepted.")
    if not path.is_file() or path.stat().st_size == 0:
        raise SecurityError("The uploaded PowerPoint file is empty or missing.")
    if path.stat().st_size > max_bytes:
        raise SecurityError(f"The uploaded file exceeds the {max_bytes // (1024 * 1024)} MB limit.")
    if not zipfile.is_zipfile(path):
        raise SecurityError("The upload is not a valid Office Open XML file.")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if not {"[Content_Types].xml", "ppt/presentation.xml"}.issubset(names):
                raise SecurityError("The upload does not contain a valid PowerPoint structure.")
            for info in archive.infolist():
                member = Path(info.filename.replace("\\", "/"))
                if member.is_absolute() or ".." in member.parts:
                    raise SecurityError("The PowerPoint archive contains an unsafe path.")
    except (zipfile.BadZipFile, OSError) as exc:
        raise SecurityError("The PowerPoint archive could not be read.") from exc


def save_uploaded_pptx(stream: BinaryIO, original_name: str, templates_dir: Path, max_bytes: int) -> tuple[Path, str]:
    templates_dir.mkdir(parents=True, exist_ok=True)
    destination = confined_path(templates_dir, f"{uuid4().hex}_{sanitize_filename(original_name)}")
    with destination.open("wb") as output:
        shutil.copyfileobj(stream, output, length=1024 * 1024)
    try:
        validate_pptx(destination, max_bytes)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination, destination.relative_to(templates_dir.parent).as_posix()
