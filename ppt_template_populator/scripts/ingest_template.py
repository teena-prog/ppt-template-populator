from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import get_settings
from src.elastic_client import create_client
from src.embedding_client import WatsonxEmbeddingClient, diagnose_embedding_configuration
from src.minio_client import create_client as create_minio_client, delete_object, put_object
from src.security import safe_user_error, sanitize_filename, validate_pptx
from src.template_binary import checksum_sha256
from src.template_indexer import TemplateIndexer
from src.template_parser import parse_template
from src.template_profile import build_template_profile, csv_values
from src.watsonx_client import WatsonxClient

MIME_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Ingest a PowerPoint template into Elasticsearch.")
    command.add_argument("pptx_path", type=Path); command.add_argument("--name", required=True)
    command.add_argument("--description", default=""); command.add_argument("--category", default="General")
    command.add_argument("--use-cases", default=""); command.add_argument("--audiences", default="")
    command.add_argument("--tones", default=""); command.add_argument("--visual-style", default="")
    command.add_argument("--supported-sections", default=""); command.add_argument("--max-size-mb", type=int)
    command.add_argument("--update-existing", "--replace-existing", dest="update_existing", action="store_true", help="Reparse and update a checksum-identical indexed template while preserving its existing template ID.")
    return command


def build_ingestion_document(args: argparse.Namespace, embedding_client=None, watsonx=None, elastic=None, minio_client=None) -> tuple[dict, bytes]:
    settings = get_settings(); path = args.pptx_path.resolve()
    max_mb = args.max_size_mb or settings.max_upload_mb
    validate_pptx(path, max_mb * 1024 * 1024)
    safe_filename = sanitize_filename(path.name); template_id = f"{Path(safe_filename).stem}-{uuid4().hex[:12]}"
    metadata = parse_template(path, file_path=safe_filename, template_name=args.name, description=args.description, category=args.category, template_id=template_id, max_bytes=max_mb * 1024 * 1024)
    metadata.update({"use_cases": csv_values(args.use_cases), "target_audiences": csv_values(args.audiences), "tones": csv_values(args.tones), "visual_style": args.visual_style.strip(), "supported_sections": csv_values(args.supported_sections)})
    metadata["template_profile"] = build_template_profile(metadata)
    if watsonx is None:
        missing = settings.watsonx_missing()
        if missing: raise ValueError("Missing required configuration: " + ", ".join(missing))
        watsonx = WatsonxClient(api_key=settings.watsonx_api_key.get_secret_value(), url=settings.watsonx_url, project_id=settings.watsonx_project_id, timeout=settings.request_timeout_seconds)
    embedder = embedding_client or WatsonxEmbeddingClient(watsonx.api_client)
    configuration = diagnose_embedding_configuration(watsonx.api_client, settings.watsonx_embedding_model_id, embedder)
    embedding = embedder.embed(metadata["template_profile"], configuration.model_id)
    if embedding.dimensions != configuration.dimensions:
        raise ValueError("Embedding dimensions changed between configuration validation and template ingestion.")
    data = path.read_bytes(); now = datetime.now(timezone.utc).isoformat()
    object_key = f"{template_id}.pptx"
    metadata.update({"filename": safe_filename, "mime_type": MIME_TYPE, "file_size": len(data), "checksum_sha256": checksum_sha256(data), "template_embedding": embedding.vector, "embedding_model_id": embedding.model_id, "embedding_dimensions": embedding.dimensions, "minio_bucket": settings.minio_templates_bucket, "minio_object_key": object_key, "created_at": now, "updated_at": now})
    metadata.pop("file_path", None)
    return metadata, data


def main() -> int:
    args = parser().parse_args()
    try:
        settings = get_settings(); document, data = build_ingestion_document(args)
        password = settings.elasticsearch_password.get_secret_value() if settings.elasticsearch_password else None
        client = create_client(settings.elasticsearch_url, settings.elasticsearch_username, password, settings.request_timeout_seconds)
        minio = create_minio_client(settings.minio_endpoint, settings.minio_access_key, settings.minio_secret_key.get_secret_value() if settings.minio_secret_key else None, settings.minio_secure)
        put_object(minio, document["minio_bucket"], document["minio_object_key"], data, MIME_TYPE)
        try:
            TemplateIndexer(client).index(document, update_existing=args.update_existing)
        except Exception:
            # Roll back the just-uploaded binary so a failed/duplicate/rejected
            # index write never leaves an orphaned object in MinIO.
            try: delete_object(minio, document["minio_bucket"], document["minio_object_key"])
            except Exception: pass
            raise
        print(f"Ingested template '{document['template_name']}' ({document['template_id']}): {document['slide_count']} slides, {document['file_size']} bytes, embedding {document['embedding_model_id']} ({document['embedding_dimensions']} dimensions), stored at minio://{document['minio_bucket']}/{document['minio_object_key']}.")
        return 0
    except Exception as exc:
        print("Ingestion failed: " + safe_user_error(exc), file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
