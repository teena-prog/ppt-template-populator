from unittest.mock import MagicMock
import pytest
from src import template_retriever as template_retriever_module
from src.response_models import TemplateSelection
from src.template_retriever import ElasticsearchGetTypeError, InvalidTemplateIdError, MissingTemplateBinaryError, MissingTemplateSourceError, TemplateNotFoundError, TemplateRetriever
from src.template_selector import extract_selected_template_id


class ObjectResponse:
    def __init__(self, body): self.body = body


class KeywordOnlyClient:
    def __init__(self, response): self.response = response; self.calls = []
    def get(self, *, index, id, source_excludes=None):
        self.calls.append({"index": index, "id": id, "source_excludes": source_excludes})
        return self.response


def selection(template_id="template-1"):
    return TemplateSelection(selected_template_id=template_id, confidence=.9, reason="Best", matched_requirements=[], unmatched_requirements=[])


def test_exact_old_ignore_status_bug_and_keyword_only_fix(monkeypatch):
    monkeypatch.setattr(template_retriever_module, "get_object", lambda client, bucket, key: b"binary-data")
    client = KeywordOnlyClient({"_source": {"template_id": "template-1", "minio_bucket": "ppt-templates", "minio_object_key": "template-1.pptx"}})
    with pytest.raises(TypeError, match="ignore_status"):
        client.get(index="ppt_templates", id="template-1", ignore_status=[404])
    source = TemplateRetriever(client, MagicMock()).get_full("template-1")
    assert source["template_id"] == "template-1"
    assert source["pptx_binary_bytes"] == b"binary-data"
    assert client.calls == [{"index": "ppt_templates", "id": "template-1", "source_excludes": None}]


def test_selection_result_extracts_non_empty_string():
    assert extract_selected_template_id(selection()) == "template-1"
    with pytest.raises(TypeError): extract_selected_template_id({"selected_template_id": "template-1"})


@pytest.mark.parametrize("invalid", [None, {}, selection(), "", "   "])
def test_invalid_template_id_type(invalid):
    with pytest.raises(InvalidTemplateIdError): TemplateRetriever(MagicMock()).get_full(invalid)


def test_missing_document_after_id_and_term_fallback():
    client = MagicMock(); client.get.return_value = {"found": False}; client.search.return_value = {"hits": {"hits": []}}
    with pytest.raises(TemplateNotFoundError): TemplateRetriever(client).get_full("missing")
    query = client.search.call_args.kwargs["query"]
    assert {"term": {"template_id.keyword": "missing"}} in query["bool"]["should"]


def test_term_fallback_supports_document_id_mismatch(monkeypatch):
    monkeypatch.setattr(template_retriever_module, "get_object", lambda client, bucket, key: b"binary-data")
    client = MagicMock(); client.get.return_value = {"found": False}
    client.search.return_value = {"hits": {"hits": [{"_id": "different-es-id", "_source": {"template_id": "selected-id", "minio_bucket": "ppt-templates", "minio_object_key": "selected-id.pptx"}}]}}
    assert TemplateRetriever(client, MagicMock()).get_full("selected-id")["template_id"] == "selected-id"


def test_missing_source_is_distinct():
    client = MagicMock(); client.get.return_value = {"found": True}
    with pytest.raises(MissingTemplateSourceError): TemplateRetriever(client).get_full("template-1")


def test_missing_binary_reference_is_distinct():
    client = MagicMock(); client.get.return_value = {"_source": {"template_id": "template-1"}}
    with pytest.raises(MissingTemplateBinaryError): TemplateRetriever(client).get_full("template-1")


def test_missing_minio_client_is_distinct():
    client = MagicMock(); client.get.return_value = {"_source": {"template_id": "template-1", "minio_bucket": "ppt-templates", "minio_object_key": "template-1.pptx"}}
    with pytest.raises(MissingTemplateBinaryError): TemplateRetriever(client).get_full("template-1")


def test_object_api_response_compatibility(monkeypatch):
    monkeypatch.setattr(template_retriever_module, "get_object", lambda client, bucket, key: b"binary-data")
    client = KeywordOnlyClient(ObjectResponse({"_source": {"template_id": "template-1", "minio_bucket": "ppt-templates", "minio_object_key": "template-1.pptx"}}))
    assert TemplateRetriever(client, MagicMock()).get_full("template-1")["pptx_binary_bytes"] == b"binary-data"


def test_elasticsearch_api_typeerror_is_recorded_and_categorized(caplog):
    client = MagicMock(); client.get.side_effect = TypeError("unexpected keyword argument")
    with pytest.raises(ElasticsearchGetTypeError): TemplateRetriever(client).get_full("template-1")
    assert "client.get TypeError" in caplog.text
    assert "pptx_binary" not in caplog.text
