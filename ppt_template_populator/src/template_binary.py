from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import tempfile
import zipfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from pptx import Presentation


class TemplateBinaryError(ValueError):
    """Stored template bytes failed integrity or format validation."""


def duplicate_package_members(path: Path) -> list[str]:
    """Return duplicate OPC member names without reading or exposing payloads."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            counts = Counter(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise TemplateBinaryError("The generated PowerPoint package is not a valid ZIP archive.") from exc
    return sorted(name for name, count in counts.items() if count > 1)


def save_presentation_safely(presentation: Presentation, destination: Path) -> None:
    """Save, validate, and atomically publish a new PPTX package."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pptx", dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
        presentation.save(str(temporary))
        duplicates = duplicate_package_members(temporary)
        if duplicates:
            raise TemplateBinaryError(
                f"The generated PowerPoint package contains {len(duplicates)} duplicate ZIP member name(s)."
            )
        Presentation(str(temporary))
        os.replace(temporary, destination)
        temporary = None
    except TemplateBinaryError:
        raise
    except Exception as exc:
        raise TemplateBinaryError("The generated PowerPoint package could not be saved or reopened safely.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def checksum_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_pptx(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_pptx(encoded: str) -> bytes:
    if not isinstance(encoded, str) or not encoded:
        raise TemplateBinaryError("The selected template has no stored PPTX data.")
    try:
        return base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise TemplateBinaryError("The selected template contains invalid Base64 data.") from exc


def verify_checksum(data: bytes, expected: str) -> None:
    if not expected or not hmac.compare_digest(checksum_sha256(data), expected.lower()):
        raise TemplateBinaryError("The selected template checksum does not match the stored checksum.")


@contextmanager
def temporary_pptx(data: bytes, expected_checksum: str) -> Iterator[Path]:
    verify_checksum(data, expected_checksum)
    path: Path | None = None
    try:
        try:
            with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as handle:
                handle.write(data)
                path = Path(handle.name)
            Presentation(str(path))
        except Exception as exc:
            raise TemplateBinaryError("The decoded template is not a readable PPTX presentation.") from exc
        # Exceptions raised by population or layout validation after this yield
        # describe downstream failures and must not be mislabeled as corruption.
        yield path
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
