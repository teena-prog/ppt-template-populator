from __future__ import annotations

import io
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from pptx import Presentation

from .elastic_client import INDEX_NAME
from .minio_client import delete_object, get_object, list_object_keys, put_object
from .template_binary import checksum_sha256, verify_checksum
from .template_indexer import TemplateIndexer


class CatalogueReplacementError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogueEntry:
    document_id: str
    template_id: str
    object_key: str


@dataclass(frozen=True)
class ReplacementResult:
    deleted_documents: int
    deleted_objects: int
    template_id: str
    object_key: str
    checksum_sha256: str
    slide_count: int


def _body(response: Any) -> Mapping[str, Any]:
    if isinstance(response, Mapping):
        return response
    body = getattr(response, "body", None)
    if isinstance(body, Mapping):
        return body
    raise CatalogueReplacementError("A template catalogue response was malformed.")


def build_manifest(es_client: Any, template_bucket: str) -> list[CatalogueEntry]:
    response = _body(es_client.search(
        index=INDEX_NAME,
        query={"match_all": {}},
        size=10_000,
        _source=["template_id", "minio_bucket", "minio_object_key"],
    ))
    hits = response.get("hits", {}).get("hits", [])
    if not isinstance(hits, list):
        raise CatalogueReplacementError("Elasticsearch returned a malformed template list.")
    manifest: list[CatalogueEntry] = []
    for hit in hits:
        source = hit.get("_source") if isinstance(hit, Mapping) else None
        document_id = hit.get("_id") if isinstance(hit, Mapping) else None
        if not isinstance(source, Mapping) or not all(isinstance(value, str) and value.strip() for value in (
            document_id, source.get("template_id"), source.get("minio_bucket"), source.get("minio_object_key")
        )):
            raise CatalogueReplacementError("An Elasticsearch document could not be identified safely as a PPT template.")
        if source["minio_bucket"] != template_bucket:
            raise CatalogueReplacementError("A template document references a bucket outside the configured template bucket.")
        if not source["minio_object_key"].lower().endswith(".pptx"):
            raise CatalogueReplacementError("A referenced object could not be identified safely as a PPTX template.")
        manifest.append(CatalogueEntry(str(document_id), source["template_id"], source["minio_object_key"]))
    return manifest


def clear_catalogue(es_client: Any, minio_client: Any, template_bucket: str, manifest: list[CatalogueEntry]) -> tuple[int, int]:
    bucket_keys = list_object_keys(minio_client, template_bucket)
    unsafe = [key for key in bucket_keys if not key.lower().endswith(".pptx")]
    if unsafe:
        raise CatalogueReplacementError("The template bucket contains an object that cannot be identified safely as a PPTX template; nothing was deleted.")
    referenced = {entry.object_key for entry in manifest}
    missing = sorted(referenced - set(bucket_keys))
    if missing:
        raise CatalogueReplacementError("The pre-deletion manifest references a missing MinIO template object; nothing was deleted.")

    deleted_documents = 0
    deleted_objects = 0
    try:
        for entry in manifest:
            result = _body(es_client.delete(index=INDEX_NAME, id=entry.document_id, refresh="wait_for"))
            if result.get("result") != "deleted":
                raise CatalogueReplacementError(f"Elasticsearch template document '{entry.document_id}' was not deleted.")
            deleted_documents += 1
        for object_key in bucket_keys:
            delete_object(minio_client, template_bucket, object_key)
            deleted_objects += 1
    except Exception as exc:
        raise CatalogueReplacementError(
            f"Catalogue deletion was partial: {deleted_documents} Elasticsearch document(s) and "
            f"{deleted_objects} MinIO object(s) were deleted."
        ) from exc

    remaining_docs = _body(es_client.search(index=INDEX_NAME, query={"match_all": {}}, size=0))["hits"]["total"]
    remaining_count = int(remaining_docs.get("value", 0) if isinstance(remaining_docs, Mapping) else remaining_docs)
    remaining_objects = list_object_keys(minio_client, template_bucket)
    if remaining_count or remaining_objects:
        raise CatalogueReplacementError("Template stores were not empty after scoped deletion.")
    return deleted_documents, deleted_objects


def write_replacement(es_client: Any, minio_client: Any, document: dict[str, Any], data: bytes) -> None:
    bucket, object_key = document["minio_bucket"], document["minio_object_key"]
    put_object(minio_client, bucket, object_key, data, document["mime_type"])
    try:
        TemplateIndexer(es_client).index(document)
    except Exception as index_error:
        try:
            delete_object(minio_client, bucket, object_key)
        except Exception as rollback_error:
            raise CatalogueReplacementError("Elasticsearch indexing failed and the new MinIO object rollback also failed.") from rollback_error
        raise CatalogueReplacementError("Elasticsearch indexing failed; the new MinIO object was rolled back.") from index_error


def verify_replacement(es_client: Any, minio_client: Any, document: dict[str, Any]) -> None:
    response = _body(es_client.search(index=INDEX_NAME, query={"match_all": {}}, size=2))
    hits = response.get("hits", {}).get("hits", [])
    keys = list_object_keys(minio_client, document["minio_bucket"])
    if len(hits) != 1 or len(keys) != 1 or set(keys) != {document["minio_object_key"]}:
        raise CatalogueReplacementError("Replacement verification did not find exactly one template in both stores.")
    source = hits[0].get("_source", {})
    if source.get("template_id") != document["template_id"]:
        raise CatalogueReplacementError("The indexed replacement template ID does not match the prepared document.")
    data = get_object(minio_client, document["minio_bucket"], document["minio_object_key"])
    verify_checksum(data, document["checksum_sha256"])
    if checksum_sha256(data) != document["checksum_sha256"] or len(Presentation(io.BytesIO(data)).slides) != document["slide_count"]:
        raise CatalogueReplacementError("The retrieved replacement PPTX failed checksum or slide-count verification.")


def replace_catalogue(es_client: Any, minio_client: Any, template_bucket: str, document: dict[str, Any], data: bytes, manifest_path: Path | None = None) -> ReplacementResult:
    if document.get("minio_bucket") != template_bucket:
        raise CatalogueReplacementError("The replacement document targets a different MinIO bucket.")
    manifest = build_manifest(es_client, template_bucket)
    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps([asdict(item) for item in manifest], indent=2), encoding="utf-8")
    deleted_documents, deleted_objects = clear_catalogue(es_client, minio_client, template_bucket, manifest)
    write_replacement(es_client, minio_client, document, data)
    verify_replacement(es_client, minio_client, document)
    return ReplacementResult(deleted_documents, deleted_objects, document["template_id"], document["minio_object_key"], document["checksum_sha256"], document["slide_count"])
