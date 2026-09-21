from __future__ import annotations

import logging
from uuid import uuid4
import streamlit as st

from config import get_settings
from src.elastic_client import test_connection
from src.embedding_client import WatsonxEmbeddingClient, diagnose_embedding_configuration
from src.minio_client import put_object, test_connection as test_minio_connection
from src.model_discovery import discover_models
from src.pipeline import GenerationPipeline
from src.response_validator import ResponseValidationError
from src.security import safe_user_error, sanitize_upload_filename
from src.services import build_application_services
from src.source_batch import SourceUpload, process_source_batch
from src.ui_state import FRIENDLY_STAGES, failure_message, friendly_stage, generation_ready, result_summary, sources_ready

st.set_page_config(page_title="AI PowerPoint Builder", layout="wide")
st.markdown("""<style>.block-container{max-width:980px;padding-top:2rem;padding-bottom:4rem}h1{text-align:center}.step{font-weight:700;margin-top:1.7rem}div[data-testid="stMetric"]{border:1px solid #d8dee8;padding:.75rem;border-radius:6px}</style>""", unsafe_allow_html=True)
settings = get_settings()
logger = logging.getLogger(__name__)

@st.cache_resource
def application_services(): return build_application_services(settings)

services = application_services()
elastic, retriever, minio_client = services.elastic, services.retriever, services.minio
es_ok, es_message = test_connection(elastic)
minio_ok, minio_message = test_minio_connection(minio_client)

st.title("AI PowerPoint Builder")
st.caption("Turn a source document into a structured, template-based presentation.")
with st.expander("System status and developer tools"):
    cols = st.columns(3)
    cols[0].metric("Elasticsearch", "Ready" if es_ok else "Unavailable")
    cols[1].metric("MinIO", "Ready" if minio_ok else "Unavailable")
    cols[2].metric("watsonx.ai", "Configured" if not settings.watsonx_missing() else "Configuration required")
    if not es_ok: st.caption(es_message)
    if not minio_ok: st.caption(minio_message)

st.markdown('<div class="step">Step 1: Upload document</div>', unsafe_allow_html=True)
formats = "PDF, DOCX, TXT"
st.caption(f"Supported formats: {formats}. Maximum file size: {settings.max_upload_mb} MB.")
uploaded_files = st.file_uploader("Source documents", type=["pdf", "docx", "txt"], accept_multiple_files=True)
batch = None
if uploaded_files:
    uploads = [SourceUpload(item.name, item.getvalue(), item.type or "application/octet-stream") for item in uploaded_files]
    batch = process_source_batch(uploads, max_file_bytes=settings.max_upload_mb * 1024 * 1024, max_total_bytes=settings.max_total_upload_mb * 1024 * 1024)
    if batch.batch_error: st.error(batch.batch_error)
    included_orders = set()
    for record in batch.records:
        columns = st.columns([4, 1.2, 1.6, 1.4, 1])
        columns[0].write(record.filename)
        columns[1].write(f"{record.size_bytes / 1024:.1f} KB")
        columns[2].write(record.extraction_status.title())
        columns[3].write(f"{len(record.extracted_text):,} chars")
        include = columns[4].checkbox("Use", value=record.extraction_status == "valid", disabled=record.extraction_status != "valid", key=f"source-use-{record.checksum}-{record.upload_order}")
        if include: included_orders.add(record.upload_order)
        if record.error: st.caption(f"{record.filename}: {record.error}")
    selected_uploads = [upload for order, upload in enumerate(uploads, start=1) if order in included_orders]
    if len(selected_uploads) != len(batch.valid_records):
        batch = process_source_batch(selected_uploads, max_file_bytes=settings.max_upload_mb * 1024 * 1024, max_total_bytes=settings.max_total_upload_mb * 1024 * 1024)
    if batch.valid_records:
        st.success(f"Detected title: {batch.title} · {len(batch.valid_records)} valid source(s)")
        with st.expander("Combined extraction preview"):
            st.text(batch.combined_text[:2500] + ("..." if len(batch.combined_text) > 2500 else ""))
    elif not batch.batch_error:
        st.error("No readable source document remains. Add at least one valid file.")

unrelated_confirmation = True
if batch and batch.warnings:
    for warning in batch.warnings: st.warning(warning)
    unrelated_confirmation = st.checkbox("I confirm these documents should be combined into one presentation.")

st.markdown('<div class="step">Step 2: Add optional instructions</div>', unsafe_allow_html=True)
instructions = st.text_area("Presentation guidance", placeholder="Audience, tone, focus, exclusions or design preferences", max_chars=5000)

st.markdown('<div class="step">Step 3: Choose Automatic or Manual selection</div>', unsafe_allow_html=True)
selection_mode = st.radio("Selection mode", ["Automatic", "Manual"], horizontal=True).casefold()
manual_id, manual_name = None, "Selected template"
if selection_mode == "manual":
    try: templates = retriever.list(size=200) if es_ok else []
    except Exception as exc:
        logger.error("Manual template catalogue failed stage=template_listing exception_type=%s", type(exc).__name__)
        st.warning("Templates could not be loaded for manual selection. See developer details in system status.")
        templates = []
    usable = [item for item in templates if item.get("compatibility", {}).get("safe_for_automatic_population", item.get("safe_for_automatic_population", True))]
    labels = {f"{item['template_name']} · {item.get('slide_count', 0)} source layouts": item for item in usable}
    if labels:
        label = st.selectbox("Template", list(labels))
        manual_id, manual_name = labels[label]["template_id"], labels[label]["template_name"]
        st.caption(f"Selected template: {manual_name}")
    else: st.warning("No compatible templates are currently available.")
else:
    st.caption("Hybrid search and reranking will choose the best compatible template.")

st.markdown('<div class="step">Step 4: Review settings</div>', unsafe_allow_html=True)
slide_count = st.number_input("Final slide count", min_value=3, max_value=30, value=14, step=1)
cols = st.columns(4)
cols[0].metric("Estimated slides", slide_count)
cols[1].metric("Title", "Included")
cols[2].metric("Introduction", "Included")
cols[3].metric("Thank You", "Included")

ready = generation_ready(services_ready=es_ok and minio_ok and not settings.watsonx_missing(), document_ready=sources_ready(batch, unrelated_confirmation), selection_mode=selection_mode, template_id=manual_id)
st.markdown('<div class="step">Step 5: Generate and download</div>', unsafe_allow_html=True)
if st.button("Generate presentation", type="primary", disabled=not ready, use_container_width=True):
    progress = st.progress(0, text=FRIENDLY_STAGES[0])
    current_stage = {"label": FRIENDLY_STAGES[0]}
    def update_stage(stage: str) -> None:
        label = friendly_stage(stage)
        current_stage["label"] = label
        progress.progress(FRIENDLY_STAGES.index(label) / (len(FRIENDLY_STAGES) - 1), text=label)
    try:
        watsonx = services.watsonx()
        models, discovery_warning = discover_models(watsonx.api_client, settings.watsonx_model_id)
        if not models: raise ValueError(discovery_warning or "No compatible generation model is available.")
        pipeline = GenerationPipeline(retriever, watsonx, settings.templates_dir.parent, settings.generated_dir, update_stage, max_required_targets_per_chunk=settings.max_required_targets_per_chunk)
        upload_storage_warning = None
        try:
            for record in batch.valid_records:
                key = f"{uuid4().hex}_{sanitize_upload_filename(record.filename)}"
                put_object(minio_client, settings.minio_uploads_bucket, key, batch.payloads[record.source_id])
        except Exception as storage_error:
            upload_storage_warning = safe_user_error(
                storage_error, "One or more source files could not be archived in object storage."
            )
        kwargs = dict(model_id=models[0]["model_id"], topic=batch.title, complete_demand="Create one complete presentation grounded in all valid uploaded sources.", source_content=batch.combined_text, audience="General audience", tone="Professional", desired_slide_count=int(slide_count), required_sections=[], visual_preferences="", additional_instructions=instructions)
        if selection_mode == "automatic":
            embedder = WatsonxEmbeddingClient(watsonx.api_client)
            configuration = diagnose_embedding_configuration(watsonx.api_client, settings.watsonx_embedding_model_id, embedder).cache_value()
            result = pipeline.run_automatic(embedding_client=embedder, embedding_configuration=configuration, confidence_threshold=settings.template_selection_confidence, **kwargs)
        else: result = pipeline.run_manual(template_id=manual_id, **kwargs)
        if upload_storage_warning:
            result.warnings.append(upload_storage_warning)
        update_stage("Download ready")
        st.session_state.last_result, st.session_state.last_template_name = result, manual_name
    except Exception as exc:
        progress.progress(1.0, text="Generation failed")
        st.error(failure_message(current_stage["label"]) + " Review the document and settings, then try again.")
        with st.expander("Developer details"):
            details = exc.errors if isinstance(exc, ResponseValidationError) else [str(exc)]
            for detail in details: st.code(safe_user_error(ValueError(detail), "Generation failed safely."))

result = st.session_state.get("last_result")
if result is not None:
    summary = result_summary(result, st.session_state.get("last_template_name", "Selected template"))
    st.subheader("Presentation ready")
    cols = st.columns(2); cols[0].metric("Template", summary["template_name"]); cols[1].metric("Final slides", summary["slide_count"])
    st.write("**Sections:** " + " · ".join(section.replace("_", " ").title() for section in summary["sections"]))
    if result.selection is not None: st.info(f"Automatic selection: {result.selection.reason} Confidence: {result.selection.confidence:.0%}.")
    if result.warnings:
        with st.expander(f"Warnings and repairs ({len(result.warnings)})"):
            for warning in result.warnings: st.write(warning)
    st.download_button("Download PPTX", result.output_path.read_bytes(), file_name=result.output_path.name, mime="application/vnd.openxmlformats-officedocument.presentationml.presentation", use_container_width=True)
    if st.button("Generate again"):
        st.session_state.pop("last_result", None); st.rerun()
