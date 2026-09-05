import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from config import Settings
from src.pipeline import GenerationPipeline
from src.response_validator import ResponseValidationError


class Retriever:
    def __init__(self, metadata): self.metadata = metadata
    def get(self, template_id): return self.metadata if template_id == self.metadata["template_id"] else None


class Watsonx:
    def __init__(self, responses): self.responses = iter(responses); self.calls = 0
    def chat(self, *args, **kwargs): self.calls += 1; return SimpleNamespace(text=next(self.responses))


def test_repair_attempt_behavior(sample_pptx: Path, template_metadata, valid_payload, tmp_path: Path):
    template_metadata["file_path"] = sample_pptx.name
    wx = Watsonx(["invalid", json.dumps(valid_payload)])
    pipeline = GenerationPipeline(Retriever(template_metadata), wx, sample_pptx.parent, tmp_path / "out")
    result = pipeline.run(template_id="template-1", model_id="model", topic="Topic", source_content="Source", audience="Audience", tone="Tone")
    assert wx.calls == 2 and result.output_path.exists()
    assert "required one repair" in result.warnings[0]


def test_only_one_repair_attempt(sample_pptx: Path, template_metadata, tmp_path: Path):
    template_metadata["file_path"] = sample_pptx.name
    wx = Watsonx(["invalid", "still invalid"])
    pipeline = GenerationPipeline(Retriever(template_metadata), wx, sample_pptx.parent, tmp_path / "out")
    with pytest.raises(ResponseValidationError, match="one controlled repair"):
        pipeline.run(template_id="template-1", model_id="model", topic="Topic", source_content="Source", audience="Audience", tone="Tone")
    assert wx.calls == 2


def test_missing_environment_variables(monkeypatch, tmp_path):
    for name in ("WATSONX_API_KEY", "WATSONX_PROJECT_ID"): monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None, watsonx_api_key=None, watsonx_project_id=None, templates_dir=tmp_path/"t", generated_dir=tmp_path/"g", data_dir=tmp_path/"d")
    assert {"WATSONX_API_KEY", "WATSONX_PROJECT_ID"}.issubset(settings.watsonx_missing())


def test_mock_elasticsearch_indexing(indexed_template):
    from unittest.mock import MagicMock
    from src.template_indexer import TemplateIndexer
    client = MagicMock(); client.indices.exists.return_value = False
    client.exists.return_value = False; client.search.return_value = {"hits": {"hits": []}}
    TemplateIndexer(client).index(indexed_template)
    client.index.assert_called_once()
