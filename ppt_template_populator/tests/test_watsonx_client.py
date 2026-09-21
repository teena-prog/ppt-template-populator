from src.watsonx_client import WatsonxClient, WatsonxRequestError


class ReadError(Exception):
    pass


class FlakyModel:
    def __init__(self):
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        if self.calls < 3:
            raise ReadError("connection interrupted")
        return {
            "choices": [{"message": {"content": '{"slides": []}'}}],
            "usage": {"completion_tokens": 3},
        }


def test_transient_read_error_is_retried(monkeypatch):
    model = FlakyModel()
    monkeypatch.setattr("src.watsonx_client.time.sleep", lambda _: None)
    client = WatsonxClient(
        api_key="test-key",
        url="https://example.invalid",
        project_id="project",
        api_client=object(),
        model_factory=lambda _: model,
    )

    result = client.chat("model", [{"role": "user", "content": "test"}])

    assert model.calls == 3
    assert result.text == '{"slides": []}'
    assert result.output_tokens == 3


def test_non_transient_error_is_not_retried():
    class InvalidModel:
        def __init__(self): self.calls = 0
        def chat(self, **kwargs):
            self.calls += 1
            raise ValueError("bad request")

    model = InvalidModel()
    client = WatsonxClient(
        api_key="test-key",
        url="https://example.invalid",
        project_id="project",
        api_client=object(),
        model_factory=lambda _: model,
    )

    try:
        client.chat("model", [{"role": "user", "content": "test"}])
        assert False, "expected request failure"
    except WatsonxRequestError:
        assert model.calls == 1


def test_chat_supplies_supported_json_schema_response_format():
    class CapturingModel:
        def __init__(self): self.params = None
        def chat(self, **kwargs):
            self.params = kwargs["params"]
            return {"choices": [{"message": {"content": '{"slides": []}'}}]}

    model = CapturingModel()
    client = WatsonxClient(api_key="test-key", url="https://example.invalid", project_id="project", api_client=object(), model_factory=lambda _: model)
    schema = {"type": "object", "properties": {"slides": {"type": "array"}}, "required": ["slides"]}
    client.chat("model", [{"role": "user", "content": "test"}], response_schema=schema)
    assert model.params["response_format"] == {"type": "json_object"}
    assert model.params["guided_json"] == schema


def test_rate_limit_and_server_errors_are_retried(monkeypatch):
    class HttpFailure(Exception):
        def __init__(self, status_code): self.status_code = status_code
    class Model:
        def __init__(self): self.calls = 0
        def chat(self, **kwargs):
            self.calls += 1
            if self.calls < 3: raise HttpFailure(429 if self.calls == 1 else 503)
            return {"choices": [{"message": {"content": '{"slides": []}'}}]}
    model = Model()
    monkeypatch.setattr("src.watsonx_client.time.sleep", lambda _: None)
    client = WatsonxClient(api_key="test-key", url="https://example.invalid",
                            project_id="project", api_client=object(), model_factory=lambda _: model)
    assert client.chat("model", [{"role": "user", "content": "test"}]).text
    assert model.calls == 3


def test_authentication_failure_is_classified_and_not_retried():
    class Unauthorized(Exception):
        status_code = 401
    class Model:
        def __init__(self): self.calls = 0
        def chat(self, **kwargs): self.calls += 1; raise Unauthorized()
    model = Model()
    client = WatsonxClient(api_key="test-key", url="https://example.invalid",
                            project_id="project", api_client=object(), model_factory=lambda _: model)
    with __import__("pytest").raises(WatsonxRequestError) as captured:
        client.chat("model", [{"role": "user", "content": "test"}])
    assert captured.value.category == "authentication_failure"
    assert model.calls == 1


def test_malformed_generation_response_is_classified():
    client = WatsonxClient(api_key="test-key", url="https://example.invalid",
                            project_id="project", api_client=object(),
                            model_factory=lambda _: type("Model", (), {"chat": lambda self, **kwargs: {"choices": []}})())
    with __import__("pytest").raises(WatsonxRequestError) as captured:
        client.chat("model", [{"role": "user", "content": "test"}])
    assert captured.value.category == "malformed_response"
