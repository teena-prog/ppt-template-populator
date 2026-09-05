from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from pptx import Presentation


class TemplateBinaryError(ValueError):
    """Stored template bytes failed integrity or format validation."""


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
        with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as handle:
            handle.write(data)
            path = Path(handle.name)
        Presentation(str(path))
        yield path
    except TemplateBinaryError:
        raise
    except Exception as exc:
        raise TemplateBinaryError("The decoded template is not a readable PPTX presentation.") from exc
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
