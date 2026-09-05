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

