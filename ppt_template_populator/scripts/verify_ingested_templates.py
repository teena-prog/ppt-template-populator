from __future__ import annotations

import argparse
import sys
from hashlib import sha256
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import get_settings
from src.services import build_application_services


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely verify indexed templates and their MinIO objects.")
    parser.add_argument("template_ids", nargs="+")
    args = parser.parse_args()
    services = build_application_services(get_settings())
    print(f"catalogue_count={len(services.retriever.list())}")
    for template_id in args.template_ids:
        document = services.retriever.get_full(template_id)
        binary = document.pop("pptx_binary_bytes")
        local = Path("templates") / f"{template_id.rsplit('-', 1)[0]}.pptx"
        checksum_matches = (
            local.is_file()
            and sha256(binary).hexdigest()
            == document.get("checksum_sha256")
            == sha256(local.read_bytes()).hexdigest()
        )
        writable = int(document.get("usable_placeholder_count", 0)) + int(document.get("usable_shape_target_count", 0))
        print(
            f"{template_id}: name={document.get('template_name')!r}, slides={document.get('slide_count')}, "
            f"writable={writable}, embedding={document.get('embedding_model_id')}:"
            f"{document.get('embedding_dimensions')}, object={document.get('minio_bucket')}/"
            f"{document.get('minio_object_key')}, checksum_match={checksum_matches}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
