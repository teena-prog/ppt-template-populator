import argparse
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from scripts import ingest_template
from src.embedding_client import EmbeddingResult
from src.pipeline import GenerationPipeline
from src.template_indexer import DuplicateTemplateError, TemplateIndexer


def ingestion_args(path):
    return argparse.Namespace(pptx_path=path, name="Startup Pitch", description="Investor deck", category="Business", use_cases="startup pitch,fundraising", audiences="investors", tones="professional,persuasive", visual_style="minimal", supported_sections="problem,solution", max_size_mb=10, update_existing=False)


def test_valid_pptx_ingestion_builds_profile_and_binary(monkeypatch, sample_pptx):
    settings = SimpleNamespace(max_upload_mb=25, watsonx_embedding_model_id="embed-model", minio_templates_bucket="ppt-templates")
    monkeypatch.setattr(ingest_template, "get_settings", lambda: settings)
    manager = MagicMock(); manager.get_embeddings_model_specs.return_value = {"resources": [{"model_id": "embed-model"}]}
    watsonx = SimpleNamespace(api_client=SimpleNamespace(foundation_models=manager))
    embedder = MagicMock(); embedder.embed.return_value = EmbeddingResult([.1, .2, .3], "embed-model")
    document, data = ingest_template.build_ingestion_document(ingestion_args(sample_pptx), embedding_client=embedder, watsonx=watsonx)
    assert data and document["minio_bucket"] == "ppt-templates" and document["minio_object_key"] and document["checksum_sha256"]
    assert document["embedding_dimensions"] == 3
    assert "Startup Pitch" in document["template_profile"] and "problem" in document["template_profile"]


def test_duplicate_template_prevention(indexed_template):
    client = MagicMock(); client.indices.exists.return_value = False; client.exists.return_value = True
    with pytest.raises(DuplicateTemplateError): TemplateIndexer(client).index(indexed_template)
    client.index.assert_not_called()


def test_update_existing_ingestion_preserves_template_id(indexed_template):
    client = MagicMock(); client.indices.exists.return_value = False; client.exists.return_value = False
    client.search.return_value = {"hits": {"hits": [{"_source": {"template_id": "existing-id"}}]}}
    result = TemplateIndexer(client).index(indexed_template, update_existing=True)
    assert result == "existing-id"
    assert client.index.call_args.kwargs["id"] == "existing-id"
    assert "op_type" not in client.index.call_args.kwargs


def test_update_existing_cli_option():
    args = ingest_template.parser().parse_args(["template.pptx", "--name", "Template", "--update-existing"])
    assert args.update_existing is True


class AutomaticRetriever:
    def __init__(self, template): self.template = template; self.binary_requested = False
    def embedding_configurations(self): return [{"model_id": self.template["embedding_model_id"], "dimensions": self.template["embedding_dimensions"], "count": 1}]
    def hybrid_search(self, **kwargs):
        safe = {key: value for key, value in self.template.items() if key not in {"pptx_binary_bytes", "template_embedding"}}
        safe.update({"retrieval_score": 2.0, "requirement_match_score": .5, "matched_requirements": ["audience"], "unmatched_requirements": ["tone"]})
        return [safe]
    def get_full(self, template_id): self.binary_requested = True; return self.template


def test_complete_automatic_pipeline(indexed_template, valid_payload, tmp_path):
    selection = {"selected_template_id": indexed_template["template_id"], "confidence": .5, "reason": "Best candidate", "matched_requirements": ["audience"], "unmatched_requirements": ["tone"]}
    chunk_payload = {"slides": valid_payload["slides"]}
    watsonx = MagicMock(); watsonx.chat.side_effect = [SimpleNamespace(text=json.dumps(selection)), SimpleNamespace(text=json.dumps(chunk_payload))]
    embedder = MagicMock(); embedder.embed.return_value = EmbeddingResult(indexed_template["template_embedding"], indexed_template["embedding_model_id"])
    retriever = AutomaticRetriever(indexed_template)
    result = GenerationPipeline(retriever, watsonx, tmp_path, tmp_path / "generated").run_automatic(model_id="generation-model", embedding_client=embedder, topic="Pitch", complete_demand="Build an investor pitch", source_content="Facts", audience="investors", tone="professional", desired_slide_count=1, required_sections=["problem"], visual_preferences="minimal", confidence_threshold=.6)
    assert result.output_path.exists() and retriever.binary_requested
    assert result.stages[-1] == "Temporary files cleaned"
    assert any("below" in warning for warning in result.warnings)
    assert result.generation_model_id == "generation-model"
