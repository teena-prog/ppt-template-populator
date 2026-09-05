from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


class WatsonxConfigurationError(ValueError): pass
class WatsonxRequestError(RuntimeError): pass


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

    def chat(self, model_id: str, messages: list[dict[str, str]], *, max_tokens: int = 4096, temperature: float = 0.1) -> ChatResult:
        try:
            if self.model_factory: model = self.model_factory(model_id)
            else:
                from ibm_watsonx_ai.foundation_models import ModelInference
                model = ModelInference(model_id=model_id, api_client=self.api_client, params={"max_tokens": max_tokens, "temperature": temperature})

            transient_names = {
                "ConnectError",
                "ConnectTimeout",
                "ReadError",
                "ReadTimeout",
                "RemoteProtocolError",
            }
            response = None
            for attempt in range(3):
                try:
                    response = model.chat(messages=messages, params={"max_tokens": max_tokens, "temperature": temperature, "response_format": {"type": "json_object"}})
                    break
                except Exception as exc:
                    if type(exc).__name__ not in transient_names or attempt == 2:
                        raise
                    time.sleep(2 ** attempt)

            if response is None:
                raise WatsonxRequestError("watsonx.ai returned no response.")
            choice = response["choices"][0]["message"]["content"]
            usage = response.get("usage", {})
            return ChatResult(text=choice, input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"), output_tokens=usage.get("completion_tokens") or usage.get("output_tokens"), raw=response)
        except Exception as exc:
            raise WatsonxRequestError(f"watsonx.ai request failed ({type(exc).__name__}).") from exc
