from __future__ import annotations

import streamlit as st
from config import get_settings
from src.document_extractor import DocumentExtractionError, SUPPORTED_EXTENSIONS, estimate_slide_count, extract_text
from src.elastic_client import create_client, test_connection
from src.embedding_client import EmbeddingDiagnosticError, WatsonxEmbeddingClient, diagnose_embedding_configuration
from src.minio_client import create_client as create_minio_client, put_object, test_connection as test_minio_connection
from src.model_discovery import discover_models
from src.pipeline import GenerationPipeline
from src.response_validator import ResponseValidationError
from src.security import safe_user_error, sanitize_upload_filename
from src.template_retriever import TemplateRetriever
from src.watsonx_client import WatsonxClient
from uuid import uuid4

st.set_page_config(page_title="AI-Powered PowerPoint Template Populator", layout="wide")
settings = get_settings()

DEFAULT_AUDIENCE = "General audience"
DEFAULT_TONE = "Professional"
DEFAULT_COMPLETE_DEMAND = "Create a complete, well-structured presentation that faithfully covers the uploaded document's content."


@st.cache_resource
def elastic_services():
    password = settings.elasticsearch_password.get_secret_value() if settings.elasticsearch_password else None
    client = create_client(settings.elasticsearch_url, settings.elasticsearch_username, password, settings.request_timeout_seconds)
    return client, TemplateRetriever(client, minio_services())


@st.cache_resource
def minio_services():
    secret = settings.minio_secret_key.get_secret_value() if settings.minio_secret_key else None
    return create_minio_client(settings.minio_endpoint, settings.minio_access_key, secret, settings.minio_secure)


def watsonx_service() -> WatsonxClient:
    missing = settings.watsonx_missing()
    if missing: raise ValueError("Missing required configuration: " + ", ".join(missing))
    return WatsonxClient(api_key=settings.watsonx_api_key.get_secret_value(), url=settings.watsonx_url, project_id=settings.watsonx_project_id, timeout=settings.request_timeout_seconds)


minio_client = minio_services()
elastic, retriever = elastic_services()
st.title("AI-Powered PowerPoint Template Populator")
with st.expander("System status", expanded=True):
    es_ok, es_message = test_connection(elastic)
    minio_ok, minio_message = test_minio_connection(minio_client)
    columns = st.columns(4)
    columns[0].metric("Elasticsearch (search index)", "Connected" if es_ok else "Unavailable")
    columns[1].metric("MinIO (file storage)", "Connected" if minio_ok else "Unavailable")
    columns[2].metric("watsonx.ai", "Configured" if not settings.watsonx_missing() else "Configuration required")
    embedding_status_error = None
    try: template_count = sum(item["count"] for item in retriever.embedding_configurations()) if es_ok else 0
    except Exception as exc: template_count = 0; embedding_status_error = safe_user_error(exc)
    columns[3].metric("Indexed templates", template_count)
    if not es_ok: st.warning(es_message)
    if not minio_ok: st.warning(minio_message)
    if embedding_status_error: st.warning(embedding_status_error)
    if st.button("Test watsonx configuration", disabled=bool(settings.watsonx_missing())):
        try:
            diagnostic_watsonx = watsonx_service(); diagnostic_embedder = WatsonxEmbeddingClient(diagnostic_watsonx.api_client)
            diagnostic = diagnose_embedding_configuration(diagnostic_watsonx.api_client, settings.watsonx_embedding_model_id, diagnostic_embedder)
            st.session_state.embedding_configuration = diagnostic.cache_value()
            st.success(f"Embedding configuration valid: {diagnostic.model_id} ({diagnostic.dimensions} dimensions).")
            st.write("Available embedding model IDs", list(diagnostic.available_model_ids))
        except EmbeddingDiagnosticError as exc:
            st.error(f"watsonx diagnostic [{exc.category}]: {safe_user_error(exc)}")
            if exc.available_model_ids: st.write("Available embedding model IDs", exc.available_model_ids)
        except Exception as exc: st.error(safe_user_error(exc, "watsonx configuration diagnostic failed safely."))

st.subheader("1. Upload your document")
st.caption("Supported formats: " + ", ".join(sorted(ext.lstrip(".").upper() for ext in SUPPORTED_EXTENSIONS)) + ". Just upload the file — no other details needed; topic, audience, tone and slide count are derived automatically.")
uploaded_file = st.file_uploader("Document", type=[ext.lstrip(".") for ext in sorted(SUPPORTED_EXTENSIONS)], label_visibility="collapsed")

extracted = None
if uploaded_file is not None:
    upload_bytes = uploaded_file.getvalue()
    if len(upload_bytes) > settings.max_upload_mb * 1024 * 1024:
        st.error(f"The uploaded file exceeds the {settings.max_upload_mb} MB limit.")
    else:
        try:
            extracted = extract_text(upload_bytes, uploaded_file.name)
            with st.expander(f"Extracted preview — \"{extracted.title}\"", expanded=False):
                st.write(f"{len(extracted.text)} characters extracted.")
                st.text(extracted.text[:2000] + ("..." if len(extracted.text) > 2000 else ""))
            for warning in extracted.warnings: st.warning(warning)
        except DocumentExtractionError as exc:
            st.error(safe_user_error(exc, "The document could not be processed."))

st.subheader("2. Choose a template")
selection_mode = st.radio("Template selection", ["Automatic (AI picks the best template)", "Manual (I choose)"], horizontal=True)
manual_template_id = None
if selection_mode.startswith("Manual"):
    try:
        available_templates = retriever.list(size=200) if es_ok else []
    except Exception as exc:
        available_templates = []; st.warning(safe_user_error(exc, "Templates could not be listed."))
    if not available_templates:
        st.warning("No templates are available to choose from yet. An administrator must ingest templates first.")
    else:
        labels = {f"{item['template_name']} — {item.get('category', 'General')} ({item.get('slide_count', 0)} slides)": item["template_id"] for item in available_templates}
        picked_label = st.selectbox("Available templates", list(labels.keys()))
        manual_template_id = labels.get(picked_label)

ready = es_ok and minio_ok and extracted is not None and (selection_mode.startswith("Automatic") or manual_template_id)
if st.button("Generate Presentation", type="primary", disabled=not ready, use_container_width=True):
    inspector = st.status("Running generation pipeline", expanded=True)
    try:
        watsonx = watsonx_service()
        models, discovery_warning = discover_models(watsonx.api_client, settings.watsonx_model_id)
        if not models: raise ValueError(discovery_warning or "No compatible watsonx.ai generation model is available.")
        if discovery_warning: inspector.write(discovery_warning)
        model_id = models[0]["model_id"]
        pipeline = GenerationPipeline(retriever, watsonx, settings.templates_dir.parent, settings.generated_dir, lambda stage: inspector.write(stage), max_required_targets_per_chunk=settings.max_required_targets_per_chunk)

        try:
            archive_key = f"{uuid4().hex}_{sanitize_upload_filename(uploaded_file.name)}"
            put_object(minio_client, settings.minio_uploads_bucket, archive_key, uploaded_file.getvalue())
        except Exception as exc:
            inspector.write(f"Uploaded document could not be archived in MinIO: {safe_user_error(exc)}")

        desired_slide_count = estimate_slide_count(extracted.text)
        generation_kwargs = dict(model_id=model_id, topic=extracted.title, complete_demand=DEFAULT_COMPLETE_DEMAND, source_content=extracted.text, audience=DEFAULT_AUDIENCE, tone=DEFAULT_TONE, desired_slide_count=desired_slide_count, required_sections=[], visual_preferences="", additional_instructions="")

        if selection_mode.startswith("Automatic"):
            embedding_client = WatsonxEmbeddingClient(watsonx.api_client)
            cached_embedding = st.session_state.get("embedding_configuration")
            if not cached_embedding or (settings.watsonx_embedding_model_id and cached_embedding.get("model_id") != settings.watsonx_embedding_model_id):
                embedding_diagnostic = diagnose_embedding_configuration(watsonx.api_client, settings.watsonx_embedding_model_id, embedding_client)
                cached_embedding = embedding_diagnostic.cache_value()
                st.session_state.embedding_configuration = cached_embedding
            result = pipeline.run_automatic(embedding_client=embedding_client, embedding_configuration=cached_embedding, confidence_threshold=settings.template_selection_confidence, **generation_kwargs)
        else:
            result = pipeline.run_manual(template_id=manual_template_id, **generation_kwargs)

        inspector.update(label="Presentation ready", state="complete")
        if result.selection is not None:
            selection = result.selection
            selected = next(item for item in result.candidates if item["template_id"] == selection.selected_template_id)
            st.subheader("Automatic template selection")
            metrics = st.columns(3); metrics[0].metric("Selected template", selected["template_name"]); metrics[1].metric("Confidence", f"{selection.confidence:.0%}"); metrics[2].metric("Retrieval score", f"{selected['retrieval_score']:.3f}")
            st.write(selection.reason)
            st.write("Matched requirements", selection.matched_requirements or ["None reported"])
            st.write("Unmatched requirements", selection.unmatched_requirements or ["None reported"])
            st.dataframe([{key: candidate.get(key) for key in ("template_name", "category", "slide_count", "retrieval_score", "requirement_match_score")} for candidate in result.candidates], use_container_width=True)
        st.subheader("Generation results")
        st.caption(f"Generation model: {result.generation_model_id} | Processing time: {result.timings['total_seconds']:.2f}s | Auto-detected slide count: {desired_slide_count}")
        st.json(result.content.model_dump(), expanded=False)
        for warning in result.warnings: st.warning(warning)
        st.download_button("Download final PPTX", data=result.output_path.read_bytes(), file_name=result.output_path.name, mime="application/vnd.openxmlformats-officedocument.presentationml.presentation", use_container_width=True)
    except ResponseValidationError as exc:
        inspector.update(label="Generation stopped", state="error")
        st.error("The generated slide content did not match the required schema.")
        for error in exc.errors: st.error(safe_user_error(ValueError(error), "Slide content validation failed safely."))
    except Exception as exc:
        inspector.update(label="Generation stopped", state="error")
        st.error(safe_user_error(exc, "Presentation generation failed safely."))
