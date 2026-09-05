from __future__ import annotations

from typing import Any


def _resources(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, list): return response
    if not isinstance(response, dict): return []
    return response.get("resources") or response.get("models") or []


def _normalize(spec: dict[str, Any]) -> dict[str, Any]:
    lifecycle = spec.get("lifecycle") or spec.get("status") or []
    status = lifecycle[-1].get("id") if isinstance(lifecycle, list) and lifecycle and isinstance(lifecycle[-1], dict) else str(lifecycle or "available")
    limits = spec.get("model_limits") or spec.get("limits") or {}
    tasks = spec.get("task_ids") or spec.get("tasks") or ["text_chat"]
    return {"model_id": spec.get("model_id") or spec.get("id"), "display_name": spec.get("label") or spec.get("name") or spec.get("model_id") or spec.get("id"), "provider": spec.get("provider") or str(spec.get("model_id", "")).split("/")[0], "status": status, "context_limit": limits.get("max_sequence_length") or limits.get("max_context_length"), "supported_tasks": tasks, "available_for_inference": status.lower() not in {"withdrawn", "deprecated", "unavailable"}}


def discover_models(api_client: Any, fallback_model_id: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
    try:
        manager = api_client.foundation_models
        response = manager.get_chat_model_specs(get_all=True)
        models = [_normalize(item) for item in _resources(response)]
        models = [item for item in models if item["model_id"] and item["available_for_inference"] and not any(term in item["model_id"].lower() for term in ("embed", "rerank"))]
        preferences = ("ibm/granite-4-h-small", "granite-4-h-micro", "granite-4-h-tiny", "granite-3-3-instruct")
        if models: return sorted(models, key=lambda item: (next((i for i, hint in enumerate(preferences) if hint in item["model_id"].lower()), len(preferences)), item["display_name"].lower())), None
        raise RuntimeError("No chat-capable models were returned.")
    except Exception as exc:
        if fallback_model_id:
            return [{"model_id": fallback_model_id, "display_name": fallback_model_id, "provider": fallback_model_id.split("/")[0], "status": "configured fallback", "context_limit": None, "supported_tasks": ["text_chat"], "available_for_inference": True}], f"Live discovery failed ({type(exc).__name__}); using WATSONX_MODEL_ID."
        return [], f"Model discovery failed ({type(exc).__name__}). Configure WATSONX_MODEL_ID as a fallback."
