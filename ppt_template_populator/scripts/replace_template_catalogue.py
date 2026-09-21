from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import get_settings
from scripts.ingest_template import build_ingestion_document
from src.elastic_client import INDEX_NAME, create_client
from src.minio_client import create_client as create_minio_client
from src.security import safe_user_error
from src.template_catalogue import replace_catalogue


def main() -> int:
    parser = argparse.ArgumentParser(description="Replace only the configured PPT template catalogue.")
    parser.add_argument("pptx_path", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--description", default="Professional presentation template")
    parser.add_argument("--category", default="Professional")
    parser.add_argument("--use-cases", default="business,presentation")
    parser.add_argument("--audiences", default="professional")
    parser.add_argument("--tones", default="professional")
    parser.add_argument("--visual-style", default="professional")
    parser.add_argument("--supported-sections", default="title,introduction,problem,objectives,solution,methodology,findings,conclusion,qa")
    parser.add_argument("--max-size-mb", type=int)
    args = parser.parse_args()
    try:
        settings = get_settings()
        document, data = build_ingestion_document(args)
        if document["slide_count"] != 17:
            raise ValueError(f"Replacement requires the validated 17-slide deck; found {document['slide_count']} slides.")
        password = settings.elasticsearch_password.get_secret_value() if settings.elasticsearch_password else None
        es = create_client(settings.elasticsearch_url, settings.elasticsearch_username, password, settings.request_timeout_seconds)
        minio = create_minio_client(settings.minio_endpoint, settings.minio_access_key, settings.minio_secret_key.get_secret_value() if settings.minio_secret_key else None, settings.minio_secure)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifest_path = settings.data_dir / f"template_catalogue_manifest_{stamp}.json"
        result = replace_catalogue(es, minio, settings.minio_templates_bucket, document, data, manifest_path)
        print(f"Deleted Elasticsearch templates: {result.deleted_documents}")
        print(f"Deleted MinIO template objects: {result.deleted_objects}")
        print(f"New template ID: {result.template_id}")
        print(f"Elasticsearch index: {INDEX_NAME}")
        print(f"MinIO object: {settings.minio_templates_bucket}/{result.object_key}")
        print(f"Slides: {result.slide_count}")
        print(f"Checksum verified: {result.checksum_sha256}")
        print(f"Pre-deletion manifest: {manifest_path}")
        return 0
    except Exception as exc:
        print("Catalogue replacement failed: " + safe_user_error(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
