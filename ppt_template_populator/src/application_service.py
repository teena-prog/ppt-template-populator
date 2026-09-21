from __future__ import annotations

from typing import Any, Callable

from .api_models import GenerationRequest, SelectionRequest
from .embedding_client import WatsonxEmbeddingClient, diagnose_embedding_configuration
from .model_discovery import discover_models
from .pipeline import GenerationPipeline, PipelineResult
from .template_selector import select_template
from .template_retriever import build_retrieval_query

DEFAULT_COMPLETE_DEMAND = (
    "Create a complete, well-structured presentation that faithfully covers "
    "the uploaded document's content."
)


def generation_model(watsonx: Any, fallback_model_id: str | None) -> str:
    models, warning = discover_models(watsonx.api_client, fallback_model_id)
    if not models:
        raise ValueError(warning or "No compatible watsonx.ai generation model is available.")
    return models[0]["model_id"]


def request_requirements(request: SelectionRequest) -> dict[str, Any]:
    return {
        "topic": request.topic,
        "complete_demand": DEFAULT_COMPLETE_DEMAND,
        "source_content": request.source_content,
        "audience": request.audience,
        "tone": request.tone,
        "desired_slide_count": request.desired_slide_count or 14,
        "required_sections": request.required_sections,
        "visual_preferences": request.visual_preferences,
        "additional_instructions": request.user_instructions,
    }


def select_template_for_request(services: Any, request: SelectionRequest) -> tuple[Any, list[dict[str, Any]], str]:
    watsonx = services.watsonx()
    model_id = generation_model(watsonx, services.settings.watsonx_model_id)
    configurations = services.retriever.embedding_configurations()
    if not configurations:
        raise ValueError("No embedded templates are available.")
    configured = diagnose_embedding_configuration(
        watsonx.api_client,
        services.settings.watsonx_embedding_model_id,
        WatsonxEmbeddingClient(watsonx.api_client),
    )
    configuration = next(
        (item for item in configurations if item["model_id"] == configured.model_id
         and item["dimensions"] == configured.dimensions), None
    )
    if configuration is None:
        raise ValueError("Indexed templates use a different embedding model or dimension.")
    requirements = request_requirements(request)
    # Template choice needs a representative grounded excerpt, while final
    # generation below continues to receive the complete extracted document.
    requirements["source_content"] = request.source_content[:6000]
    retrieval_query = build_retrieval_query(
        topic=request.topic, source_content=request.source_content,
        audience=request.audience, tone=request.tone,
        required_sections=request.required_sections,
        visual_preferences=request.visual_preferences,
        additional_instructions=request.user_instructions,
    )
    embedder = WatsonxEmbeddingClient(watsonx.api_client)
    embedding = embedder.embed(retrieval_query, configuration["model_id"])
    candidates = services.retriever.hybrid_search(
        retrieval_query=retrieval_query,
        query_vector=embedding.vector,
        embedding_model_id=embedding.model_id,
        embedding_dimensions=embedding.dimensions,
        requirements=requirements,
        size=5,
    )
    if not candidates:
        raise ValueError("No compatible template was found.")
    selection, _ = select_template(watsonx, model_id, requirements, candidates)
    return selection, candidates, model_id


def generate_for_request(
    services: Any,
    request: GenerationRequest,
    stage_callback: Callable[[str], None] | None = None,
) -> PipelineResult:
    watsonx = services.watsonx()
    model_id = generation_model(watsonx, services.settings.watsonx_model_id)
    pipeline = GenerationPipeline(
        services.retriever,
        watsonx,
        services.settings.templates_dir.parent,
        services.settings.generated_dir,
        stage_callback,
        max_required_targets_per_chunk=services.settings.max_required_targets_per_chunk,
    )
    kwargs = request_requirements(request)
    if request.selection_mode == "manual":
        if not request.template_id:
            raise ValueError("template_id is required for manual selection.")
        return pipeline.run_manual(template_id=request.template_id, model_id=model_id, **kwargs)
    embedding_client = WatsonxEmbeddingClient(watsonx.api_client)
    configuration = diagnose_embedding_configuration(
        watsonx.api_client,
        services.settings.watsonx_embedding_model_id,
        embedding_client,
    ).cache_value()
    return pipeline.run_automatic(
        model_id=model_id,
        embedding_client=embedding_client,
        embedding_configuration=configuration,
        confidence_threshold=services.settings.template_selection_confidence,
        **kwargs,
    )
