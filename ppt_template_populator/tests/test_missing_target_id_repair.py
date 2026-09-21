import json
from types import SimpleNamespace

from src.pipeline import GenerationPipeline


def _template():
    targets = []
    for target_id, role, limit in ((10, "title", 60), (11, "heading", 50), (12, "body", 250)):
        targets.append({
            "slide_number": 1, "target_kind": "shape", "target_id": target_id,
            "role": role, "existing_text": role.title(),
            "approximate_max_characters": limit, "max_content_length": limit,
            "required": True, "replaceable": True,
        })
    return {"template_id": "t", "slide_count": 1, "slides": [{
        "slide_number": 1, "layout_name": "Blank", "targets": targets,
    }]}


class MissingIdThenRepairWatson:
    def __init__(self):
        self.calls = []

    def chat(self, model_id, messages, **kwargs):
        self.calls.append(messages)
        if len(self.calls) == 1:
            targets = [
                {"target_kind": "shape", "target_id": 10, "content": "Keep this title"},
                {"target_kind": "shape", "content": "Heading without ID"},
                {"target_kind": "shape", "content": "Body without ID"},
            ]
        else:
            targets = [
                {"target_kind": "shape", "target_id": 10, "content": "Do not overwrite"},
                {"target_kind": "shape", "target_id": 11, "content": "Repaired heading"},
                {"target_kind": "shape", "target_id": 12, "content": "Repaired body"},
            ]
        return SimpleNamespace(text=json.dumps({"title": "Untrusted model title", "slides": [{
            "slide_number": 1, "layout_name": "Blank", "targets": targets,
        }]}))


def test_ambiguous_missing_ids_get_one_repair_and_preserve_valid_key(tmp_path):
    watsonx = MissingIdThenRepairWatson()
    pipeline = GenerationPipeline(None, watsonx, tmp_path, tmp_path)
    content, warnings, calls = pipeline._generate_validated(
        model_id="model", template=_template(),
        message_args={"topic": "Test", "source_content": "Facts", "audience": "All",
                      "tone": "Clear", "additional_instructions": ""},
    )
    values = {(target.target_kind, target.target_id): target.content
              for target in content.slides[0].targets}
    assert calls == 2
    assert values[("shape", 10)] == "Keep this title"
    assert values[("shape", 11)] == "Repaired heading"
    assert values[("shape", 12)] == "Repaired body"
    repair = json.loads(watsonx.calls[1][-1]["content"])
    assert "slides.0.targets.1.target_id: Field required" in repair["validation_errors"]
    assert "preserve every already-valid target value exactly" in repair["instruction"]
    assert any("controlled structural repair" in warning for warning in warnings)
