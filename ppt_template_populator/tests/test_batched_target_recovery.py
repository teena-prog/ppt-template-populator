import json
from types import SimpleNamespace

from src.pipeline import GenerationPipeline


def _large_recovery_template(target_count=27):
    return {
        "template_id": "large-recovery",
        "slide_count": 1,
        "slides": [
            {
                "slide_number": 1,
                "layout_name": "Blank",
                "targets": [
                    {
                        "slide_number": 1,
                        "target_kind": "shape",
                        "target_id": target_id,
                        "role": "body",
                        "existing_text": f"Original {target_id}",
                        "approximate_max_characters": 100,
                        "max_content_length": 100,
                        "required": True,
                        "replaceable": True,
                        "position": {
                            "left": 0,
                            "top": target_id * 10,
                            "width": 100,
                            "height": 20,
                        },
                    }
                    for target_id in range(1, target_count + 1)
                ],
            }
        ],
    }


class PartialBatchWatson:
    def __init__(self):
        self.recovery_requests = []
        self.recovery_call_number = 0

    def chat(self, model_id, messages, **kwargs):
        payload = json.loads(messages[-1]["content"].split("\n", 1)[-1])
        if "ordered_targets" not in payload:
            return SimpleNamespace(
                text=json.dumps(
                    {
                        "slides": [
                            {
                                "slide_number": 1,
                                "layout_name": "Blank",
                                "targets": [
                                    {
                                        "target_kind": "shape",
                                        "target_id": 1,
                                        "content": "Preserved initial content",
                                    }
                                ],
                            }
                        ]
                    }
                )
            )

        requested = payload["ordered_targets"]
        self.recovery_requests.append(requested)
        self.recovery_call_number += 1
        if self.recovery_call_number % 2 == 1 and len(requested) > 1:
            emitted = requested[: max(1, len(requested) // 2)]
        else:
            emitted = requested
        return SimpleNamespace(
            text=json.dumps({"contents": [f"Recovered {item['target_id']}" for item in emitted]})
        )


def test_many_targets_are_batched_and_partial_batches_retry_only_unresolved(tmp_path):
    watson = PartialBatchWatson()
    pipeline = GenerationPipeline(
        None,
        watson,
        tmp_path,
        tmp_path,
        max_recovery_targets_per_batch=6,
        max_recovery_attempts=2,
    )

    content, warnings, calls = pipeline._generate_validated(
        model_id="model",
        template=_large_recovery_template(),
        message_args={
            "topic": "Services",
            "source_content": "Facts",
            "audience": "Customers",
            "tone": "Professional",
            "additional_instructions": "",
        },
    )

    assert all(len(request) <= 6 for request in watson.recovery_requests)
    assert calls == 11
    assert len(content.slides[0].targets) == 27
    generated = {item.target_id: item.content for item in content.slides[0].targets}
    assert generated[1] == "Preserved initial content"
    assert generated[27] == "Recovered 27"
    assert any("bounded batches" in warning for warning in warnings)


class UnknownTargetWatson:
    def __init__(self): self.calls = 0

    def chat(self, model_id, messages, **kwargs):
        self.calls += 1
        payload = json.loads(messages[-1]["content"].split("\n", 1)[-1])
        if "ordered_targets" in payload:
            requested = payload["ordered_targets"]
            return SimpleNamespace(
                text=json.dumps({"contents": [f"Recovered {item['target_id']}" for item in requested]})
            )
        return SimpleNamespace(
            text=json.dumps(
                {
                    "slides": [
                        {
                            "slide_number": 1,
                            "layout_name": "Blank",
                            "targets": [
                                {
                                    "target_kind": "shape",
                                    "target_id": 999,
                                    "content": "Invented target",
                                }
                            ],
                        }
                    ]
                }
            )
        )


def test_unknown_model_target_is_discarded_and_required_targets_recover(tmp_path):
    watson = UnknownTargetWatson()
    pipeline = GenerationPipeline(None, watson, tmp_path, tmp_path)

    content, warnings, calls = pipeline._generate_validated(
        model_id="model",
        template=_large_recovery_template(target_count=2),
        message_args={
            "topic": "Services",
            "source_content": "Facts",
            "audience": "Customers",
            "tone": "Professional",
            "additional_instructions": "",
        },
    )

    assert calls == 2
    assert {item.target_id for item in content.slides[0].targets} == {1, 2}
    assert any("Discarded unknown" in warning for warning in warnings)
