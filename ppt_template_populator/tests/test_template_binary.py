import base64
from pathlib import Path
import pytest
from src.template_binary import TemplateBinaryError, checksum_sha256, decode_pptx, encode_pptx, temporary_pptx, verify_checksum


def test_base64_round_trip_and_checksum(sample_pptx: Path):
    data = sample_pptx.read_bytes(); encoded = encode_pptx(data)
    assert decode_pptx(encoded) == data
    assert checksum_sha256(data) == checksum_sha256(decode_pptx(encoded))


@pytest.mark.parametrize("invalid", ["%%%", "YWJj=trailing", "", "not base64!"])
def test_strict_base64_rejection(invalid):
    with pytest.raises(TemplateBinaryError): decode_pptx(invalid)


def test_checksum_mismatch_rejected(sample_pptx: Path):
    with pytest.raises(TemplateBinaryError, match="checksum"): verify_checksum(sample_pptx.read_bytes(), "0" * 64)


def test_temporary_pptx_is_cleaned(sample_pptx: Path):
    data = sample_pptx.read_bytes(); created = None
    with temporary_pptx(data, checksum_sha256(data)) as path:
        created = path; assert path.exists()
    assert created is not None and not created.exists()


def test_temporary_pptx_does_not_mask_downstream_errors(sample_pptx: Path):
    data = sample_pptx.read_bytes(); created = None
    with pytest.raises(RuntimeError, match="population failed"):
        with temporary_pptx(data, checksum_sha256(data)) as path:
            created = path
            raise RuntimeError("population failed")
    assert created is not None and not created.exists()
