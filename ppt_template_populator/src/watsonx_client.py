from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


class WatsonxConfigurationError(ValueError): pass


class WatsonxRequestError(RuntimeError):
    def __init__(self, message: str, category: str = "api_failure", status_code: int | None = None):
        self.category = category
        self.status_code = status_code
        super().__init__(message)


def _status_code(exc: Exception) -> int | None:
    for owner in (exc, getattr(exc, "response", None), getattr(exc, "meta", None)):
        for attribute in ("status_code", "status", "http_status"):
            value = getattr(owner, attribute, None)
            if isinstance(value, int):
                return value
    return None


def _request_error(exc: Exception) -> WatsonxRequestError:
    status = _status_code(exc)
    name = type(exc).__name__.casefold()
    if status == 400:
        return WatsonxRequestError("watsonx.ai rejected the generation request.", "invalid_request", status)
    if status == 401:
        return WatsonxRequestError("watsonx.ai authentication failed.", "authentication_failure", status)
    if status == 403:
        return WatsonxRequestError("watsonx.ai denied access to the project or model.", "permission_failure", status)
    if status == 404:
        return WatsonxRequestError("The configured watsonx.ai generation model is unavailable.", "unavailable_model", status)
    if status == 429:
        return WatsonxRequestError("watsonx.ai rate limiting persisted after bounded retries.", "rate_limit", status)
    if status is not None and status >= 500:
        return WatsonxRequestError("watsonx.ai remained unavailable after bounded retries.", "service_failure", status)
    if any(marker in name for marker in ("timeout", "connect", "network", "readerror", "remoteprotocol")):
        return WatsonxRequestError("A network error occurred while contacting watsonx.ai.", "network_failure", status)
    return WatsonxRequestError(f"watsonx.ai request failed ({type(exc).__name__}).", "api_failure", status)


def _retryable(exc: Exception) -> bool:
    status = _status_code(exc)
    if status == 429 or (status is not None and status >= 500):
        return True
    name = type(exc).__name__.casefold()
    return any(marker in name for marker in (
        "connecterror", "connecttimeout", "readerror", "readtimeout", "remoteprotocolerror"
    ))


@dataclass
class ChatResult:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    raw: dict[str, Any] | None = None


class WatsonxClient:
    def __init__(self, *, api_key: str, url: str, project_id: str, timeout: int = 120, api_client: Any = None, model_factory: Any = None):
        if not api_key or not url or not project_id: raise WatsonxConfigurationError("watsonx.ai requires API key, URL, and project ID.")
        self.api_key, self.url, self.project_id, self.timeout = api_key, url, project_id, timeout
        if api_client is None:
            import httpx
            from ibm_watsonx_ai import APIClient, Credentials
            from ibm_watsonx_ai.utils.utils import HttpClientConfig

            timeout_config = httpx.Timeout(float(timeout), connect=min(30.0, float(timeout)))
            http_config = HttpClientConfig(
                timeout=timeout_config,
                limits=httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                    keepalive_expiry=30,
                ),
            )
            api_client = APIClient(
                Credentials(url=url, api_key=api_key),
                project_id=project_id,
                httpx_client=http_config,
                async_httpx_client=http_config,
            )
        self.api_client = api_client
        self.model_factory = model_factory

    def chat(self, model_id: str, messages: list[dict[str, str]], *, max_tokens: int = 4096, temperature: float = 0.1, response_schema: dict[str, Any] | None = None) -> ChatResult:
        try:
            if self.model_factory: model = self.model_factory(model_id)
            else:
                from ibm_watsonx_ai.foundation_models import ModelInference
                model = ModelInference(model_id=model_id, api_client=self.api_client, params={"max_tokens": max_tokens, "temperature": temperature})

            response = None
            for attempt in range(3):
                try:
                    params = {"max_tokens": max_tokens, "temperature": temperature, "response_format": {"type": "json_object"}}
                    if response_schema is not None:
                        # ibm-watsonx-ai TextChatParameters exposes JSON Schema
                        # constraints through guided_json.
                        params["guided_json"] = response_schema
                    response = model.chat(messages=messages, params=params)
                    break
                except Exception as exc:
                    if not _retryable(exc) or attempt == 2:
                        raise
                    time.sleep(2 ** attempt)

            if response is None:
                raise WatsonxRequestError("watsonx.ai returned no response.")
            try:
                choice = response["choices"][0]["message"]["content"]
                usage = response.get("usage", {})
            except (KeyError, IndexError, TypeError, AttributeError) as exc:
                raise WatsonxRequestError(
                    "watsonx.ai returned a malformed generation response.", "malformed_response"
                ) from exc
            if not isinstance(choice, str) or not choice.strip() or not isinstance(usage, dict):
                raise WatsonxRequestError(
                    "watsonx.ai returned a malformed generation response.", "malformed_response"
                )
            return ChatResult(text=choice, input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"), output_tokens=usage.get("completion_tokens") or usage.get("output_tokens"), raw=response)
        except WatsonxRequestError:
            raise
        except Exception as exc:
            raise _request_error(exc) from exc
