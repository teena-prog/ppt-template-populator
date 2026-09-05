from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from src.embedding_client import EmbeddingDiagnosticError, EmbeddingResult, WatsonxEmbeddingClient, diagnose_embedding_configuration


def api_with(response=None, error=None):
    manager = MagicMock()
    if error: manager.get_embeddings_model_specs.side_effect = error
    else: manager.get_embeddings_model_specs.return_value = response
    return SimpleNamespace(foundation_models=manager)


def test_available_configured_model_uses_metadata_dimensions():
    api = api_with({"resources": [{"model_id": "ibm/available", "embedding_dimensions": 384}]})
    embedder = MagicMock()
    result = diagnose_embedding_configuration(api, "ibm/available", embedder)
    assert result.model_id == "ibm/available" and result.dimensions == 384
    embedder.embed.assert_not_called()


def test_unavailable_configured_model_reports_available_ids():
    api = api_with({"resources": [{"model_id": "ibm/available"}]})
    with pytest.raises(EmbeddingDiagnosticError) as caught:
        diagnose_embedding_configuration(api, "ibm/missing", MagicMock())
    assert caught.value.category == "invalid_model_id"
    assert caught.value.available_model_ids == ["ibm/available"]


def test_permission_failure_is_categorized():
    error = RuntimeError("permission denied"); error.status_code = 403
    with pytest.raises(EmbeddingDiagnosticError) as caught:
        diagnose_embedding_configuration(api_with(error=error), "ibm/model")
    assert caught.value.category == "permission_failure"


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (SimpleNamespace(status_code=401), "authentication_failure"),
        (SimpleNamespace(status_code=404), "unavailable_model"),
        (TimeoutError("network timeout"), "network_failure"),
    ],
)
def test_discovery_failures_are_categorized(error, category):
    if not isinstance(error, Exception):
        exception = RuntimeError(category); exception.status_code = error.status_code
    else:
        exception = error
    with pytest.raises(EmbeddingDiagnosticError) as caught:
        diagnose_embedding_configuration(api_with(error=exception), "ibm/model")
    assert caught.value.category == category


@pytest.mark.parametrize("response", [None, {}, {"resources": "invalid"}, {"resources": [{}]}])
def test_malformed_discovery_response(response):
    with pytest.raises(EmbeddingDiagnosticError) as caught:
        diagnose_embedding_configuration(api_with(response), "ibm/model")
    assert caught.value.category == "malformed_response"


def test_missing_dimensions_are_inferred_from_probe_embedding():
    api = api_with({"resources": [{"model_id": "ibm/available"}]})
    embedder = MagicMock(); embedder.embed.return_value = EmbeddingResult([.1, .2, .3, .4], "ibm/available")
    result = diagnose_embedding_configuration(api, "ibm/available", embedder)
    assert result.dimensions == 4
    assert "diagnostic" in embedder.embed.call_args.args[0].lower()


@pytest.mark.parametrize("response", [{}, {"results": []}, {"results": [{}]}, {"results": [{"embedding": []}]}, {"results": [{"embedding": [1, "bad"]}]}])
def test_malformed_embedding_response(response):
    model = MagicMock(); model.generate.return_value = response
    client = WatsonxEmbeddingClient(MagicMock(), embedding_factory=lambda model_id: model)
    with pytest.raises(EmbeddingDiagnosticError) as caught: client.embed("diagnostic", "ibm/model")
    assert caught.value.category == "malformed_response"
