from __future__ import annotations
from typing import Any
from elasticsearch import ApiError, AuthenticationException, AuthorizationException
from elasticsearch import ConnectionError as ElasticsearchConnectionError
from elasticsearch import ConnectionTimeout, Elasticsearch

INDEX_NAME = "ppt_templates"
class ElasticsearchUnavailable(RuntimeError):
    def __init__(self, message: str, category: str = "elasticsearch_failure"):
        self.category = category
        super().__init__(message)
class MappingMigrationRequired(RuntimeError): pass


def elasticsearch_error(exc: Exception, operation: str) -> ElasticsearchUnavailable:
    if isinstance(exc, ConnectionTimeout): return ElasticsearchUnavailable(f"Elasticsearch timed out during {operation}.", "timeout")
    if isinstance(exc, AuthenticationException): return ElasticsearchUnavailable(f"Elasticsearch authentication failed during {operation}.", "authentication_failure")
    if isinstance(exc, AuthorizationException): return ElasticsearchUnavailable(f"Elasticsearch denied permission during {operation}.", "permission_failure")
    if isinstance(exc, ElasticsearchConnectionError): return ElasticsearchUnavailable(f"Elasticsearch connection was refused or unreachable during {operation}.", "network_failure")
    status = getattr(getattr(exc, "meta", None), "status", None)
    if status == 404: return ElasticsearchUnavailable("The ppt_templates index does not exist. An administrator must ingest a template first.", "index_missing")
    if status == 400: return ElasticsearchUnavailable(f"Elasticsearch rejected {operation}; verify the ppt_templates mapping and indexed embedding fields.", "invalid_mapping")
    return ElasticsearchUnavailable(f"Elasticsearch failed during {operation} ({type(exc).__name__}).")


def build_index_mapping(embedding_dimensions: int) -> dict[str, Any]:
    if embedding_dimensions <= 0: raise ValueError("Embedding dimensions must be positive.")
    shapes = {"slide_number": {"type": "integer"}, "shape_id": {"type": "integer"}, "shape_name": {"type": "keyword"}, "group_path": {"type": "integer"}, "is_placeholder": {"type": "boolean"}, "placeholder_idx": {"type": "integer"}, "placeholder_type": {"type": "keyword"}, "existing_text": {"type": "text"}, "has_text_frame": {"type": "boolean"}, "font_size": {"type": "float"}, "role": {"type": "keyword"}, "is_writable_target": {"type": "boolean"}, "max_content_length": {"type": "integer"}, "position": {"properties": {key: {"type": "long"} for key in ("left", "top", "width", "height")}}}
    return {"mappings": {"properties": {
        "template_id": {"type": "keyword"}, "template_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "description": {"type": "text"}, "category": {"type": "keyword"}, "use_cases": {"type": "keyword"}, "target_audiences": {"type": "keyword"}, "tones": {"type": "keyword"}, "visual_style": {"type": "text"}, "supported_sections": {"type": "keyword"},
        "filename": {"type": "keyword"}, "mime_type": {"type": "keyword"}, "file_size": {"type": "long"}, "checksum_sha256": {"type": "keyword"}, "slide_count": {"type": "integer"}, "has_image_placeholders": {"type": "boolean"}, "has_chart_placeholders": {"type": "boolean"},
        "slides": {"type": "nested", "properties": {"slide_number": {"type": "integer"}, "layout_name": {"type": "keyword"}, "has_image_placeholders": {"type": "boolean"}, "has_chart_placeholders": {"type": "boolean"}, "shapes": {"type": "nested", "properties": shapes}, "targets": {"type": "nested", "properties": {"slide_number": {"type": "integer"}, "target_kind": {"type": "keyword"}, "target_id": {"type": "integer"}, "role": {"type": "keyword"}, "existing_text": {"type": "text"}, "max_content_length": {"type": "integer"}, "approximate_max_characters": {"type": "integer"}, "required": {"type": "boolean"}, "replaceable": {"type": "boolean"}, "classification_confidence": {"type": "float"}, "shape_name": {"type": "keyword"}, "font_size": {"type": "float"}, "position": {"properties": {key: {"type": "long"} for key in ("left", "top", "width", "height")}}}}}},
        "usable_placeholder_count": {"type": "integer"}, "usable_shape_target_count": {"type": "integer"}, "slides_without_writable_targets": {"type": "integer"}, "safe_for_automatic_population": {"type": "boolean"},
        "template_profile": {"type": "text"}, "template_embedding": {"type": "dense_vector", "dims": embedding_dimensions, "index": True, "similarity": "cosine"}, "embedding_model_id": {"type": "keyword"}, "embedding_dimensions": {"type": "integer"}, "minio_bucket": {"type": "keyword"}, "minio_object_key": {"type": "keyword"}, "created_at": {"type": "date"}, "updated_at": {"type": "date"},
    }}}


INDEX_MAPPING = build_index_mapping(768)


def create_client(url: str, username: str | None = None, password: str | None = None, timeout: int = 30) -> Elasticsearch:
    kwargs: dict[str, Any] = {"hosts": [url], "request_timeout": timeout, "retry_on_timeout": True, "max_retries": 2}
    if username and password: kwargs["basic_auth"] = (username, password)
    return Elasticsearch(**kwargs)


def test_connection(client: Any) -> tuple[bool, str]:
    try: client.info(); return True, "Connected"
    except ConnectionTimeout: return False, "Elasticsearch connection timed out. Verify ELASTICSEARCH_URL, network routing, and firewall rules."
    except (AuthenticationException, AuthorizationException): return False, "Elasticsearch rejected authentication. Verify ELASTICSEARCH_USERNAME and ELASTICSEARCH_PASSWORD and confirm the account has cluster access."
    except ElasticsearchConnectionError: return False, "Elasticsearch connection was refused or the host is unreachable. Verify that Elasticsearch is running at ELASTICSEARCH_URL and that the configured port is the Elasticsearch API port, not the Kibana port."
    except ApiError as exc:
        status = getattr(getattr(exc, "meta", None), "status", None)
        return False, f"Elasticsearch returned an API error{f' (HTTP {status})' if status else ''}."
    except Exception as exc: return False, f"Elasticsearch health check failed ({type(exc).__name__})."


def _mapping_issues(properties: dict[str, Any], dimensions: int) -> list[str]:
    expected = build_index_mapping(dimensions)["mappings"]["properties"]; issues = []
    for name, definition in expected.items():
        actual = properties.get(name)
        if actual is not None and definition.get("type") and actual.get("type") != definition["type"]: issues.append(f"field {name} must be {definition['type']}")
    vector = properties.get("template_embedding", {})
    if vector and vector.get("dims") != dimensions: issues.append(f"template_embedding dimensions must be {dimensions}")
    if vector.get("similarity", "cosine") != "cosine": issues.append("template_embedding similarity must be cosine")
    slide_properties = properties.get("slides", {}).get("properties", {})
    for nested_name in ("shapes", "targets"):
        actual_nested = slide_properties.get(nested_name)
        if actual_nested is not None and actual_nested.get("type") != "nested": issues.append(f"slides.{nested_name} must be nested")
    return issues


def ensure_index(client: Any, embedding_dimensions: int = 768) -> None:
    try:
        if not client.indices.exists(index=INDEX_NAME):
            client.indices.create(index=INDEX_NAME, **build_index_mapping(embedding_dimensions)); return
        response = client.indices.get_mapping(index=INDEX_NAME)
        properties = response[INDEX_NAME]["mappings"].get("properties", {})
        issues = _mapping_issues(properties, embedding_dimensions)
        if issues: raise MappingMigrationRequired("The existing ppt_templates index requires migration/reindexing: " + "; ".join(issues) + ". No data was changed.")
        expected = build_index_mapping(embedding_dimensions)["mappings"]["properties"]
        missing = {name: definition for name, definition in expected.items() if name not in properties}
        slide_properties = properties.get("slides", {}).get("properties", {})
        expected_slide_properties = expected["slides"]["properties"]
        nested_missing = {name: definition for name, definition in expected_slide_properties.items() if name not in slide_properties}
        actual_shapes = slide_properties.get("shapes", {}).get("properties", {})
        expected_shapes = expected_slide_properties["shapes"]["properties"]
        shape_missing = {name: definition for name, definition in expected_shapes.items() if name not in actual_shapes}
        if shape_missing: nested_missing["shapes"] = {"type": "nested", "properties": shape_missing}
        if nested_missing: missing["slides"] = {"type": "nested", "properties": nested_missing}
        if missing: client.indices.put_mapping(index=INDEX_NAME, properties=missing)
    except MappingMigrationRequired: raise
    except Exception as exc: raise ElasticsearchUnavailable("Elasticsearch index initialization failed.") from exc
