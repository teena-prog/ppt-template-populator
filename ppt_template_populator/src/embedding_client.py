from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PREFERRED_EMBEDDING_HINTS = ("granite-embedding-278m-multilingual", "granite-278m-multilingual")
DIMENSION_PROBE_TEXT = "PowerPoint template retrieval diagnostic."


class EmbeddingDiagnosticError(RuntimeError):
    def __init__(self, category: str, message: str, available_model_ids: list[str] | None = None):
        self.category = category
        self.available_model_ids = available_model_ids or []
        super().__init__(message)


@dataclass(frozen=True)
class EmbeddingResult:
    vector: list[float]
    model_id: str

    @property
    def dimensions(self) -> int: return len(self.vector)


@dataclass(frozen=True)
class EmbeddingConfiguration:
    model_id: str
    dimensions: int
    available_model_ids: tuple[str, ...] = ()

    def cache_value(self) -> dict[str, str | int]:
        return {"model_id": self.model_id, "dimensions": self.dimensions}


def _status_code(exc: Exception) -> int | None:
    for owner in (exc, getattr(exc, "response", None), getattr(exc, "meta", None)):
        for attribute in ("status_code", "status"):
            value = getattr(owner, attribute, None)
            if isinstance(value, int): return value
    return None


def _classified_error(exc: Exception, operation: str) -> EmbeddingDiagnosticError:
    status = _status_code(exc); name = type(exc).__name__.lower(); text = str(exc).lower()
    if status == 401 or "authentication" in name or "unauthorized" in text:
        return EmbeddingDiagnosticError("authentication_failure", f"watsonx.ai authentication failed during {operation}.")
    if status == 403 or "forbidden" in text or "permission" in text:
        return EmbeddingDiagnosticError("permission_failure", f"watsonx.ai denied permission during {operation}. Confirm project access and model entitlements.")
    if status == 404 or "not found" in text or "unavailable" in text:
        return EmbeddingDiagnosticError("unavailable_model", f"The embedding model was unavailable during {operation}.")
    if status == 400 or "apirequestfailure" in name or any(term in text for term in ("token limit", "input length", "too long")):
        return EmbeddingDiagnosticError(
            "invalid_embedding_request",
            f"watsonx.ai rejected the embedding request during {operation}. "
            "The retrieval input is bounded; if this persists, use Test watsonx configuration "
            "to verify that the indexed embedding model remains available.",
        )
    if any(term in name or term in text for term in ("connection", "timeout", "network", "dns")):
        return EmbeddingDiagnosticError("network_failure", f"A network failure occurred during watsonx.ai {operation}.")
    return EmbeddingDiagnosticError("api_failure", f"watsonx.ai {operation} failed ({type(exc).__name__}).")


def _resources(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, list): resources = response
    elif isinstance(response, dict): resources = response.get("resources", response.get("models"))
    else: resources = None
    if not isinstance(resources, list) or any(not isinstance(item, dict) for item in resources):
        raise EmbeddingDiagnosticError("malformed_response", "watsonx.ai returned malformed embedding-model metadata.")
    return resources


def _model_id(spec: dict[str, Any]) -> str | None:
    value = spec.get("model_id") or spec.get("id")
    return value if isinstance(value, str) and value.strip() else None


def _metadata_dimensions(spec: dict[str, Any]) -> int | None:
    candidates = [spec.get("embedding_dimensions"), spec.get("dimensions"), spec.get("output_dimension")]
    limits = spec.get("model_limits") or spec.get("limits") or {}
    if isinstance(limits, dict): candidates.extend([limits.get("embedding_dimensions"), limits.get("dimensions"), limits.get("output_dimension")])
    for value in candidates:
        if isinstance(value, int) and value > 0: return value
    return None


def discover_embedding_model_specs(api_client: Any) -> list[dict[str, Any]]:
    try: response = api_client.foundation_models.get_embeddings_model_specs(get_all=True)
    except Exception as exc: raise _classified_error(exc, "embedding-model discovery") from exc
    resources = _resources(response)
    if any(_model_id(item) is None for item in resources):
        raise EmbeddingDiagnosticError("malformed_response", "watsonx.ai embedding-model metadata contains an invalid model ID.")
    return resources


def discover_embedding_models(api_client: Any, fallback_model_id: str | None = None) -> list[str]:
    specs = discover_embedding_model_specs(api_client)
    ids = [_model_id(item) for item in specs]
    return sorted((item for item in ids if item), key=lambda model_id: (not any(hint in model_id.lower() for hint in PREFERRED_EMBEDDING_HINTS), model_id))


class WatsonxEmbeddingClient:
    def __init__(self, api_client: Any, embedding_factory: Any | None = None):
        self.api_client = api_client
        self.embedding_factory = embedding_factory

    def embed(self, text: str, model_id: str) -> EmbeddingResult:
        if not text.strip(): raise ValueError("Embedding input must not be empty.")
        try:
            if self.embedding_factory is None:
                from ibm_watsonx_ai.foundation_models import Embeddings
                model = Embeddings(model_id=model_id, api_client=self.api_client)
            else:
                model = self.embedding_factory(model_id)
            response = model.generate(inputs=[text])
        except Exception as exc: raise _classified_error(exc, "embedding generation") from exc
        try: vector = response["results"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc: raise EmbeddingDiagnosticError("malformed_response", "watsonx.ai returned a malformed embedding response.") from exc
        if not isinstance(vector, list) or not vector or any(not isinstance(value, (int, float)) for value in vector):
            raise EmbeddingDiagnosticError("malformed_response", "watsonx.ai returned a malformed embedding vector.")
        return EmbeddingResult([float(value) for value in vector], model_id)


def diagnose_embedding_configuration(api_client: Any, configured_model_id: str | None, embedding_client: Any | None = None) -> EmbeddingConfiguration:
    specs = discover_embedding_model_specs(api_client)
    available = sorted(_model_id(item) for item in specs if _model_id(item))
    if not available: raise EmbeddingDiagnosticError("unavailable_model", "No embedding models are available to this watsonx.ai project and region.")
    selected = configured_model_id.strip() if configured_model_id else next((model_id for model_id in available if any(hint in model_id.lower() for hint in PREFERRED_EMBEDDING_HINTS)), available[0])
    if selected not in available:
        raise EmbeddingDiagnosticError("invalid_model_id", f"Configured embedding model '{selected}' is not available to this project and region.", available)
    spec = next(item for item in specs if _model_id(item) == selected); dimensions = _metadata_dimensions(spec)
    if dimensions is None:
        result = (embedding_client or WatsonxEmbeddingClient(api_client)).embed(DIMENSION_PROBE_TEXT, selected)
        dimensions = result.dimensions
    return EmbeddingConfiguration(selected, dimensions, tuple(available))
