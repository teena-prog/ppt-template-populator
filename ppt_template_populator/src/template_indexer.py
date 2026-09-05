from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from .elastic_client import INDEX_NAME, ElasticsearchUnavailable, ensure_index

DOCUMENT_FIELDS = {"template_id", "template_name", "description", "category", "use_cases", "target_audiences", "tones", "visual_style", "supported_sections", "filename", "mime_type", "file_size", "checksum_sha256", "slide_count", "has_image_placeholders", "has_chart_placeholders", "usable_placeholder_count", "usable_shape_target_count", "slides_without_writable_targets", "safe_for_automatic_population", "slides", "template_profile", "template_embedding", "embedding_model_id", "embedding_dimensions", "minio_bucket", "minio_object_key", "created_at", "updated_at"}
REQUIRED_FIELDS = {"template_id", "template_name", "filename", "checksum_sha256", "slides", "template_embedding", "embedding_model_id", "embedding_dimensions", "minio_bucket", "minio_object_key"}


def build_document(metadata: dict[str, Any]) -> dict[str, Any]:
    document = {key: value for key, value in metadata.items() if key in DOCUMENT_FIELDS}
    missing = sorted(field for field in REQUIRED_FIELDS if document.get(field) in (None, "", []))
    if missing: raise ValueError("Template document is missing required fields: " + ", ".join(missing))
    if len(document["template_embedding"]) != document["embedding_dimensions"]: raise ValueError("Embedding vector dimensions do not match embedding_dimensions.")
    return document


class DuplicateTemplateError(ValueError): pass


class TemplateIndexer:
    def __init__(self, client: Any): self.client = client

    def find_duplicate(self, template_id: str, checksum: str) -> str | None:
        try:
            if self.client.exists(index=INDEX_NAME, id=template_id): return f"template_id {template_id}"
            response = self.client.search(index=INDEX_NAME, query={"term": {"checksum_sha256": checksum}}, size=1, _source=["template_id"])
            hits = response.get("hits", {}).get("hits", [])
            return f"checksum already stored as {hits[0]['_source']['template_id']}" if hits else None
        except Exception as exc: raise ElasticsearchUnavailable("Duplicate-template check failed.") from exc

    def duplicate_template_id(self, template_id: str, checksum: str) -> str | None:
        try:
            if self.client.exists(index=INDEX_NAME, id=template_id): return template_id
            response = self.client.search(index=INDEX_NAME, query={"term": {"checksum_sha256": checksum}}, size=1, _source=["template_id"])
            hits = response.get("hits", {}).get("hits", [])
            return hits[0]["_source"]["template_id"] if hits else None
        except Exception as exc: raise ElasticsearchUnavailable("Duplicate-template check failed.") from exc

    def index(self, metadata: dict[str, Any], update_existing: bool = False, replace_existing: bool | None = None) -> str:
        if replace_existing is not None:
            update_existing = replace_existing
        document = build_document(metadata); ensure_index(self.client, document["embedding_dimensions"])
        duplicate_id = self.duplicate_template_id(document["template_id"], document["checksum_sha256"])
        if duplicate_id and not update_existing: raise DuplicateTemplateError(f"Template was not ingested: duplicate template {duplicate_id}.")
        if duplicate_id: document["template_id"] = duplicate_id
        try:
            kwargs = {} if duplicate_id else {"op_type": "create"}
            self.client.index(index=INDEX_NAME, id=document["template_id"], document=document, refresh="wait_for", **kwargs)
            return str(document["template_id"])
        except Exception as exc: raise ElasticsearchUnavailable("Template document could not be indexed.") from exc

    def update(self, template_id: str, changes: dict[str, Any]) -> None:
        safe = {key: value for key, value in changes.items() if key in DOCUMENT_FIELDS - {"template_id", "minio_bucket", "minio_object_key", "checksum_sha256"}}
        safe["updated_at"] = datetime.now(timezone.utc).isoformat()
        try: self.client.update(index=INDEX_NAME, id=template_id, doc=safe, refresh="wait_for")
        except Exception as exc: raise ElasticsearchUnavailable("Template metadata could not be updated.") from exc

    def delete(self, template_id: str) -> bool:
        try: return self.client.delete(index=INDEX_NAME, id=template_id, refresh="wait_for", ignore_status=[404]).get("result") == "deleted"
        except Exception as exc: raise ElasticsearchUnavailable("Template metadata could not be deleted.") from exc
