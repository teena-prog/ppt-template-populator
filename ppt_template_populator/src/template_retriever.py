from __future__ import annotations
import re

import logging
from collections.abc import Mapping
from typing import Any
from elasticsearch import NotFoundError
from .elastic_client import INDEX_NAME, elasticsearch_error
from .minio_client import get_object

SAFE_EXCLUDES = ["template_embedding"]
logger = logging.getLogger(__name__)
MAX_RETRIEVAL_QUERY_CHARACTERS = 1800


def _grounded_excerpt(text: str, budget: int) -> str:
    """Sample beginning, middle and end without asking the embedding model to summarize."""
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= budget:
        return normalized
    if budget < 30:
        return normalized[:budget]
    part = max(1, (budget - 10) // 3)
    middle = max(0, len(normalized) // 2 - part // 2)
    return f"{normalized[:part]} ... {normalized[middle:middle + part]} ... {normalized[-part:]}"[:budget]


def build_retrieval_query(*, topic: str, source_content: str, complete_demand: str = "",
                          audience: str = "", tone: str = "",
                          required_sections: list[str] | None = None,
                          visual_preferences: str = "",
                          additional_instructions: str = "") -> str:
    """Build a model-safe embedding query while preserving grounded coverage."""
    guidance = [topic, complete_demand, audience, tone,
                ", ".join(required_sections or []), visual_preferences,
                additional_instructions]
    compact_guidance = [re.sub(r"\s+", " ", value).strip()[:240]
                        for value in guidance if value and value.strip()]
    prefix = " | ".join(compact_guidance)
    source_budget = max(200, MAX_RETRIEVAL_QUERY_CHARACTERS - len(prefix) - 3)
    excerpt = _grounded_excerpt(source_content, source_budget)
    return " | ".join(filter(None, [prefix, excerpt]))[:MAX_RETRIEVAL_QUERY_CHARACTERS]


class InvalidTemplateIdError(TypeError): pass
class TemplateNotFoundError(LookupError): pass
class MissingTemplateSourceError(ValueError): pass
class MissingTemplateBinaryError(ValueError): pass
class ElasticsearchGetTypeError(RuntimeError): pass


def _template_id(value: Any) -> str:
    if not isinstance(value, str): raise InvalidTemplateIdError("Template ID must be a string, not a selection or candidate object.")
    value = value.strip()
    if not value: raise InvalidTemplateIdError("Template ID must be a non-empty string.")
    return value


def _response_body(response: Any) -> Mapping[str, Any]:
    if isinstance(response, Mapping): return response
    body = getattr(response, "body", None)
    if isinstance(body, Mapping): return body
    raise TypeError("Elasticsearch returned an unsupported response object.")


def validate_embedding(vector: list[float], model_id: str, dimensions: int) -> None:
    if not model_id: raise ValueError("An embedding model ID is required.")
    if not vector or len(vector) != dimensions: raise ValueError(f"Query embedding has {len(vector)} dimensions; expected {dimensions}.")
    if not all(isinstance(value, (int, float)) for value in vector): raise ValueError("Query embedding contains non-numeric values.")


def candidate_match_score(candidate: dict[str, Any], requirements: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    matched, unmatched = [], []
    checks = [("audience", candidate.get("target_audiences", []), requirements.get("audience", "")), ("tone", candidate.get("tones", []), requirements.get("tone", ""))]
    for label, values, requested in checks:
        if not requested: continue
        target = requested.lower(); (matched if any(target in value.lower() or value.lower() in target for value in values) else unmatched).append(label)
    required_sections = {item.lower() for item in requirements.get("required_sections", [])}; available_sections = {item.lower() for item in candidate.get("supported_sections", [])}
    for section in sorted(required_sections): (matched if section in available_sections else unmatched).append(f"section:{section}")
    desired = requirements.get("desired_slide_count")
    if desired:
        delta = abs(candidate.get("slide_count", 0) - desired); (matched if delta <= max(1, round(desired * .2)) else unmatched).append("slide_count")
    visual = requirements.get("visual_preferences", "").lower()
    if visual: (matched if any(word in candidate.get("visual_style", "").lower() for word in visual.split()) else unmatched).append("visual_style")
    source_terms = set(re.findall(r"[a-z0-9]{4,}", f"{requirements.get('topic', '')} {requirements.get('source_content', '')[:3000]}".lower()))
    candidate_text = " ".join(str(candidate.get(field, "")) for field in (
        "template_name", "description", "category", "template_profile", "visual_style",
        "use_cases", "supported_sections",
    )).lower()
    candidate_terms = set(re.findall(r"[a-z0-9]{4,}", candidate_text))
    if source_terms:
        overlap = len(source_terms & candidate_terms) / max(1, min(len(source_terms), 20))
        (matched if overlap >= .05 else unmatched).append("document_topic")
    total = len(matched) + len(unmatched)
    return (len(matched) / total if total else 1.0), matched, unmatched


class TemplateRetriever:
    def __init__(self, client: Any, minio_client: Any = None): self.client, self.minio_client = client, minio_client

    def _fallback_by_template_id(self, template_id: str, include_binary: bool) -> dict[str, Any] | None:
        query = {"bool": {"should": [{"term": {"template_id": template_id}}, {"term": {"template_id.keyword": template_id}}], "minimum_should_match": 1}}
        kwargs = {} if include_binary else {"source_excludes": SAFE_EXCLUDES}
        response = _response_body(self.client.search(index=INDEX_NAME, query=query, size=1, **kwargs))
        hits = response.get("hits", {}).get("hits", [])
        if not hits: return None
        source = hits[0].get("_source")
        if not isinstance(source, Mapping): raise MissingTemplateSourceError("The template search result is missing _source.")
        return dict(source)

    def get(self, template_id: str, include_binary: bool = False) -> dict[str, Any] | None:
        template_id = _template_id(template_id); kwargs = {} if include_binary else {"source_excludes": SAFE_EXCLUDES}
        try:
            try: response = _response_body(self.client.get(index=INDEX_NAME, id=template_id, **kwargs))
            except NotFoundError: return self._fallback_by_template_id(template_id, include_binary)
            if response.get("found") is False: return self._fallback_by_template_id(template_id, include_binary)
            source = response.get("_source")
            if not isinstance(source, Mapping): raise MissingTemplateSourceError("The selected template document is missing _source.")
            return dict(source)
        except (InvalidTemplateIdError, MissingTemplateSourceError): raise
        except TypeError as exc:
            logger.error("Elasticsearch client.get TypeError during template retrieval; verify keyword arguments and response contract. Error type: %s", type(exc).__name__)
            raise ElasticsearchGetTypeError("Elasticsearch client.get rejected the template retrieval call (TypeError).") from exc
        except Exception as exc: raise elasticsearch_error(exc, "template retrieval") from exc

    def get_full(self, template_id: str) -> dict[str, Any]:
        template_id = _template_id(template_id); source = self.get(template_id, include_binary=True)
        if source is None: raise TemplateNotFoundError(f"Template '{template_id}' was not found by document ID or template_id field.")
        bucket, object_key = source.get("minio_bucket"), source.get("minio_object_key")
        if not bucket or not object_key:
            raise MissingTemplateBinaryError(f"Template '{template_id}' does not reference a stored MinIO object.")
        if self.minio_client is None:
            raise MissingTemplateBinaryError("A MinIO client is required to retrieve the template's PPTX binary.")
        result = dict(source)
        result["pptx_binary_bytes"] = get_object(self.minio_client, bucket, object_key)
        return result

    def list(self, size: int = 100) -> list[dict[str, Any]]:
        try:
            response = self.client.search(index=INDEX_NAME, query={"match_all": {}}, size=size, source_excludes=SAFE_EXCLUDES)
            return [hit["_source"] for hit in response["hits"]["hits"]]
        except Exception as exc: raise elasticsearch_error(exc, "template listing") from exc

    def embedding_configurations(self) -> list[dict[str, Any]]:
        try:
            response = self.client.search(index=INDEX_NAME, size=0, aggs={"models": {"composite": {"sources": [{"model_id": {"terms": {"field": "embedding_model_id"}}}, {"dimensions": {"terms": {"field": "embedding_dimensions"}}}]}}})
            return [{"model_id": bucket["key"]["model_id"], "dimensions": int(bucket["key"]["dimensions"]), "count": bucket["doc_count"]} for bucket in response.get("aggregations", {}).get("models", {}).get("buckets", [])]
        except (KeyError, TypeError, ValueError) as exc: raise ValueError("Elasticsearch returned malformed template embedding aggregation data.") from exc
        except Exception as exc: raise elasticsearch_error(exc, "template embedding configuration retrieval") from exc

    def hybrid_search(self, *, retrieval_query: str, query_vector: list[float], embedding_model_id: str, embedding_dimensions: int, requirements: dict[str, Any], size: int = 5) -> list[dict[str, Any]]:
        validate_embedding(query_vector, embedding_model_id, embedding_dimensions)
        filters = [{"term": {"embedding_model_id": embedding_model_id}}, {"term": {"embedding_dimensions": embedding_dimensions}}, {"term": {"safe_for_automatic_population": True}}]
        base_query = {"bool": {"filter": filters, "should": [{"multi_match": {"query": retrieval_query, "fields": ["template_name^4", "description^2", "template_profile", "visual_style", "supported_sections", "use_cases", "target_audiences", "tones"]}}], "minimum_should_match": 0}}
        query = {"script_score": {"query": base_query, "script": {"source": "0.55 * _score + 0.45 * (cosineSimilarity(params.vector, 'template_embedding') + 1.0)", "params": {"vector": query_vector}}}}
        try:
            response = self.client.search(index=INDEX_NAME, query=query, size=min(max(size, 1), 5), source_excludes=SAFE_EXCLUDES); candidates = []
            for hit in response.get("hits", {}).get("hits", []):
                candidate = dict(hit["_source"]); candidate["retrieval_score"] = float(hit.get("_score") or 0)
                match_score, matched, unmatched = candidate_match_score(candidate, requirements)
                retrieval_score = candidate["retrieval_score"]
                candidate.update({"requirement_match_score": match_score,
                                  "rerank_score": retrieval_score * .75 + match_score * .25,
                                  "matched_requirements": matched,
                                  "unmatched_requirements": unmatched}); candidates.append(candidate)
            return sorted(candidates, key=lambda item: (item["rerank_score"], item["retrieval_score"]), reverse=True)
        except Exception as exc: raise elasticsearch_error(exc, "hybrid template retrieval") from exc

    def search(self, keyword: str, size: int = 25) -> list[dict[str, Any]]:
        try:
            response = self.client.search(index=INDEX_NAME, query={"multi_match": {"query": keyword, "fields": ["template_name^3", "description", "category"]}}, size=size, source_excludes=SAFE_EXCLUDES)
            return [hit["_source"] for hit in response["hits"]["hits"]]
        except Exception as exc: raise elasticsearch_error(exc, "template search") from exc
