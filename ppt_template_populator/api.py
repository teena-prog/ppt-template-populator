from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from config import get_settings
from src.api_models import (
    ExtractedDocumentResponse, GenerationRequest, HealthResponse,
    JobCreatedResponse, JobStatusResponse, SelectionRequest, SelectionResponse,
    TemplateSummary,
)
from src.application_service import generate_for_request, select_template_for_request
from src.document_extractor import DocumentExtractionError, SUPPORTED_EXTENSIONS, extract_text
from src.elastic_client import test_connection
from src.job_store import InMemoryJobStore
from src.minio_client import test_connection as test_minio_connection
from src.response_validator import ResponseValidationError
from src.security import safe_user_error
from src.services import ApplicationServices, build_application_services

DOCUMENT_MIME_TYPES = {
    ".pdf": {"application/pdf"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".doc": {"application/msword"},
    ".txt": {"text/plain"},
    ".md": {"text/plain", "text/markdown"},
    ".markdown": {"text/plain", "text/markdown"},
    ".rtf": {"application/rtf", "text/rtf"},
}


@lru_cache(maxsize=1)
def default_services() -> ApplicationServices:
    return build_application_services(get_settings())


def create_app(services: ApplicationServices | None = None) -> FastAPI:
    app = FastAPI(
        title="AI-Powered PowerPoint Template Populator API",
        version="1.0.0",
        description="Shared backend API for document extraction, template selection and PPTX generation.",
    )
    app.state.services = services
    app.state.jobs = InMemoryJobStore()

    def dependencies() -> ApplicationServices:
        return app.state.services or default_services()

    @app.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
    def health(svc: ApplicationServices = Depends(dependencies)) -> HealthResponse:
        es_ok, es_message = test_connection(svc.elastic)
        minio_ok, minio_message = test_minio_connection(svc.minio)
        watsonx_state = "configured" if not svc.settings.watsonx_missing() else "configuration required"
        return HealthResponse(
            status="ok" if es_ok and minio_ok and watsonx_state == "configured" else "degraded",
            elasticsearch="connected" if es_ok else es_message,
            minio="connected" if minio_ok else minio_message,
            watsonx=watsonx_state,
        )

    @app.post("/api/v1/documents/extract", response_model=ExtractedDocumentResponse, tags=["documents"])
    async def extract_document(
        document: UploadFile = File(...),
        svc: ApplicationServices = Depends(dependencies),
    ) -> ExtractedDocumentResponse:
        filename = document.filename or "document"
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise HTTPException(415, "Unsupported document type.")
        content_type = (document.content_type or "").lower()
        if content_type and content_type != "application/octet-stream" and content_type not in DOCUMENT_MIME_TYPES[suffix]:
            raise HTTPException(415, "The document MIME type does not match its extension.")
        data = await document.read(svc.settings.max_upload_mb * 1024 * 1024 + 1)
        if len(data) > svc.settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"Document exceeds the {svc.settings.max_upload_mb} MB limit.")
        try:
            extracted = extract_text(data, filename)
        except DocumentExtractionError as exc:
            raise HTTPException(422, safe_user_error(exc)) from exc
        return ExtractedDocumentResponse(
            title=extracted.title, text=extracted.text, warnings=extracted.warnings
        )

    @app.get("/api/v1/templates", response_model=list[TemplateSummary], tags=["templates"])
    def templates(svc: ApplicationServices = Depends(dependencies)) -> list[TemplateSummary]:
        try:
            return [TemplateSummary.model_validate(item) for item in svc.retriever.list(size=200)]
        except Exception as exc:
            raise HTTPException(503, safe_user_error(exc, "Templates could not be listed.")) from exc

    @app.post("/api/v1/templates/select", response_model=SelectionResponse, tags=["templates"])
    def select_template_endpoint(
        request: SelectionRequest,
        svc: ApplicationServices = Depends(dependencies),
    ) -> SelectionResponse:
        try:
            selection, candidates, _ = select_template_for_request(svc, request)
            return SelectionResponse(
                **selection.model_dump(),
                candidates=[TemplateSummary.model_validate(item) for item in candidates],
            )
        except Exception as exc:
            raise HTTPException(422, safe_user_error(exc, "Template selection failed safely.")) from exc

    def run_job(job_id: str, request: GenerationRequest, svc: ApplicationServices) -> None:
        jobs = app.state.jobs
        jobs.update(job_id, status="running")
        try:
            result = generate_for_request(
                svc, request,
                stage_callback=lambda stage: jobs.get(job_id).stages.append(stage),
            )
            jobs.update(
                job_id, status="completed", output_path=result.output_path,
                warnings=list(result.warnings), stages=list(result.stages),
            )
        except ResponseValidationError as exc:
            jobs.update(job_id, status="failed", error="; ".join(exc.errors))
        except Exception as exc:
            jobs.update(job_id, status="failed", error=safe_user_error(exc, "Generation failed safely."))

    @app.post("/api/v1/presentations", response_model=JobCreatedResponse, status_code=202, tags=["presentations"])
    def generate_presentation(
        request: GenerationRequest,
        background_tasks: BackgroundTasks,
        svc: ApplicationServices = Depends(dependencies),
    ) -> JobCreatedResponse:
        if request.selection_mode == "manual" and not request.template_id:
            raise HTTPException(422, "template_id is required for manual selection.")
        job = app.state.jobs.create()
        background_tasks.add_task(run_job, job.job_id, request, svc)
        return JobCreatedResponse(job_id=job.job_id)

    @app.get("/api/v1/jobs/{job_id}", response_model=JobStatusResponse, tags=["presentations"])
    def job_status(job_id: str) -> JobStatusResponse:
        job = app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Generation job was not found.")
        return JobStatusResponse(
            job_id=job.job_id, status=job.status, stages=job.stages,
            warnings=job.warnings, error=job.error,
            download_url=f"/api/v1/jobs/{job.job_id}/download" if job.status == "completed" else None,
        )

    @app.get("/api/v1/jobs/{job_id}/download", tags=["presentations"])
    def download(job_id: str, svc: ApplicationServices = Depends(dependencies)) -> FileResponse:
        job = app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Generation job was not found.")
        if job.status != "completed" or job.output_path is None:
            raise HTTPException(409, "Generation output is not ready.")
        generated_root = svc.settings.generated_dir.resolve()
        output = job.output_path.resolve()
        if generated_root not in output.parents or not output.is_file():
            raise HTTPException(404, "Generated presentation is unavailable.")
        return FileResponse(
            output, media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            filename=output.name,
        )

    return app


app = create_app()
