import json
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from src.elastic_client import MappingMigrationRequired, build_index_mapping, ensure_index
from src.response_validator import ResponseValidationError
from src.template_retriever import MAX_RETRIEVAL_QUERY_CHARACTERS, TemplateRetriever, build_retrieval_query, candidate_match_score, validate_embedding
from src.template_selector import build_selection_messages, select_template, validate_selection


def candidate(template_id="one", score=.8):
    return {"template_id": template_id, "template_name": template_id, "target_audiences": ["investors"], "tones": ["professional"], "supported_sections": ["problem"], "visual_style": "minimal", "slide_count": 10, "retrieval_score": score, "requirement_match_score": score, "matched_requirements": ["audience"], "unmatched_requirements": []}


def test_mapping_contains_minio_reference_and_cosine_vector():
    properties = build_index_mapping(3)["mappings"]["properties"]
    assert properties["minio_bucket"]["type"] == "keyword"
    assert properties["minio_object_key"]["type"] == "keyword"
    assert properties["template_embedding"] == {"type": "dense_vector", "dims": 3, "index": True, "similarity": "cosine"}


def test_incompatible_mapping_requires_migration():
    client = MagicMock(); client.indices.exists.return_value = True
    client.indices.get_mapping.return_value = {"ppt_templates": {"mappings": {"properties": {"template_id": {"type": "integer"}}}}}
    with pytest.raises(MappingMigrationRequired, match="No data was changed"): ensure_index(client, 3)
    client.indices.delete.assert_not_called()


def test_compatible_missing_mapping_fields_are_added():
    client = MagicMock(); client.indices.exists.return_value = True
    properties = build_index_mapping(3)["mappings"]["properties"]
    properties.pop("safe_for_automatic_population")
    properties["slides"]["properties"].pop("targets")
    client.indices.get_mapping.return_value = {"ppt_templates": {"mappings": {"properties": properties}}}
    ensure_index(client, 3)
    additions = client.indices.put_mapping.call_args.kwargs["properties"]
    assert "safe_for_automatic_population" in additions
    assert "targets" in additions["slides"]["properties"]


def test_embedding_dimension_validation_and_mixed_model_filter():
    with pytest.raises(ValueError, match="expected 3"): validate_embedding([.1], "model", 3)
    client = MagicMock(); client.search.return_value = {"hits": {"hits": []}}
    TemplateRetriever(client).hybrid_search(retrieval_query="pitch", query_vector=[.1, .2, .3], embedding_model_id="model-a", embedding_dimensions=3, requirements={})
    query = client.search.call_args.kwargs["query"]
    filters = query["script_score"]["query"]["bool"]["filter"]
    assert {"term": {"embedding_model_id": "model-a"}} in filters
    assert {"term": {"safe_for_automatic_population": True}} in filters


def test_hybrid_results_exclude_embedding_and_include_scores():
    client = MagicMock(); client.search.return_value = {"hits": {"hits": [{"_score": 2.5, "_source": candidate()}]}}
    results = TemplateRetriever(client).hybrid_search(retrieval_query="pitch", query_vector=[.1, .2, .3], embedding_model_id="model", embedding_dimensions=3, requirements={"audience": "investors"})
    assert results[0]["retrieval_score"] == 2.5
    assert "template_embedding" in client.search.call_args.kwargs["source_excludes"]


def test_candidate_scoring():
    score, matched, unmatched = candidate_match_score(candidate(), {"audience": "investors", "tone": "casual", "required_sections": ["problem", "financials"], "desired_slide_count": 10})
    assert 0 < score < 1 and "audience" in matched and "tone" in unmatched


def test_valid_selection_and_invented_id_rejection():
    data = {"selected_template_id": "one", "confidence": .9, "reason": "Best match", "matched_requirements": [], "unmatched_requirements": []}
    assert validate_selection(data, {"one"}).confidence == .9
    data["selected_template_id"] = "invented"
    with pytest.raises(ResponseValidationError): validate_selection(data, {"one"})


def test_invalid_selection_falls_back_to_highest_candidate():
    watsonx = MagicMock(); watsonx.chat.side_effect = [SimpleNamespace(text="invalid"), SimpleNamespace(text="still invalid")]
    selection, repaired = select_template(watsonx, "model", {}, [candidate("best", .75), candidate("other", .4)])
    assert selection.selected_template_id == "best" and selection.confidence == .75 and repaired


def test_selection_payload_excludes_bulky_per_slide_data():
    # Regression: with only one template in the pool, the selection request
    # was small enough to never hit the model's context limit. Once the pool
    # grew, sending every candidate's full slide/shape tree (position, font
    # size, existing_text, ...) for up to 5 candidates blew straight past it
    # (156k tokens against a 131k limit) and the whole "Automatic" pipeline
    # failed with a raw ApiRequestFailure. Only the compact template_profile
    # summary (plus light scalar fields) belongs in this request.
    bulky = candidate("one")
    bulky["template_profile"] = "Template: One. Category: Business."
    bulky["slides"] = [{"slide_number": n, "shapes": [{"existing_text": "x" * 500}] * 40} for n in range(1, 15)]
    messages = build_selection_messages({}, [bulky])
    content = messages[-1]["content"]
    assert "template_profile" in content and "Template: One" in content
    assert "slides" not in content and "shapes" not in content and "x" * 500 not in content


def test_retrieval_query_is_bounded_and_samples_large_grounded_document():
    source = "BEGINFACT " + ("middleword " * 2000) + " ENDFACT"
    query = build_retrieval_query(
        topic="Women Empowerment", source_content=source,
        audience="Leaders", additional_instructions="Use a formal tone",
    )
    assert len(query) <= MAX_RETRIEVAL_QUERY_CHARACTERS
    assert "Women Empowerment" in query
    assert "BEGINFACT" in query and "ENDFACT" in query
