from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import api as api_module
from api import create_app
from src.pipeline import PipelineResult
from src.response_models import PresentationContent


def _services(tmp_path: Path):
    elastic = MagicMock()
    elastic.info.return_value = {"version": {"number": "8.0"}}
    minio = MagicMock()
    minio.list_buckets.return_value = []
    settings = SimpleNamespace(
        max_upload_mb=1,
        generated_dir=tmp_path / "generated",
        watsonx_model_id=None,
        watsonx_embedding_model_id=None,
        template_selection_confidence=.6,
        max_required_targets_per_chunk=12,
        templates_dir=tmp_path / "templates",
        watsonx_missing=lambda: [],
    )
    template = {
        "template_id": "t1", "template_name": "Business", "category": "Business",
        "slide_count": 4, "safe_for_automatic_population": True,
    }
    retriever = MagicMock()
    retriever.list.return_value = [template]
    return SimpleNamespace(settings=settings, elastic=elastic, minio=minio, retriever=retriever)


def test_health_and_template_listing_are_safe(tmp_path):
    client = TestClient(create_app(_services(tmp_path)))
    health = client.get("/api/v1/health")
    assert health.status_code == 200 and health.json()["status"] == "ok"
    response = client.get("/api/v1/templates")
    assert response.status_code == 200
    assert response.json()[0]["template_id"] == "t1"


def test_document_extraction_and_file_validation(tmp_path):
    client = TestClient(create_app(_services(tmp_path)))
    response = client.post(
        "/api/v1/documents/extract",
        files={"document": ("brief.txt", b"Women Empowerment\nGrounded source text.", "text/plain")},
    )
    assert response.status_code == 200
    assert response.json()["title"] == "Women Empowerment"
    rejected = client.post(
        "/api/v1/documents/extract",
        files={"document": ("payload.exe", b"not executable", "application/octet-stream")},
    )
    assert rejected.status_code == 415


def test_generation_job_status_and_download(tmp_path, monkeypatch):
    services = _services(tmp_path)
    output_dir = services.settings.generated_dir
    output_dir.mkdir(parents=True)
    output = output_dir / "presentation_test.pptx"
    output.write_bytes(b"safe-pptx-test")
    result = PipelineResult(
        content=PresentationContent(presentation_title="Test", slides=[]),
        output_path=output, warnings=["review"], timings={}, stages=["Output saved"],
    )
    monkeypatch.setattr(api_module, "generate_for_request", lambda *_args, **_kwargs: result)
    client = TestClient(create_app(services))
    request = {
        "topic": "Women Empowerment", "source_content": "Grounded facts from the document.",
        "selection_mode": "manual", "template_id": "t1",
        "user_instructions": "Formal tone; exclude unrelated material.",
    }
    created = client.post("/api/v1/presentations", json=request)
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    status = client.get(f"/api/v1/jobs/{job_id}")
    assert status.json()["status"] == "completed"
    assert status.json()["download_url"].endswith("/download")
    download = client.get(status.json()["download_url"])
    assert download.status_code == 200 and download.content == b"safe-pptx-test"


def test_manual_generation_requires_template_id(tmp_path):
    client = TestClient(create_app(_services(tmp_path)))
    response = client.post("/api/v1/presentations", json={
        "topic": "Topic", "source_content": "Facts", "selection_mode": "manual",
    })
    assert response.status_code == 422


def test_job_not_found(tmp_path):
    client = TestClient(create_app(_services(tmp_path)))
    assert client.get("/api/v1/jobs/not-found").status_code == 404
