from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Any, Callable
from .response_validator import ResponseValidationError, extract_json, validate_response

WEIGHTS = {"json_schema": .30, "coverage": .25, "quality": .20, "instruction": .15, "latency": .05, "tokens": .05}


@dataclass
class EvaluationResult:
    model_id: str; score: float; valid_json_rate: float; schema_success_rate: float
    coverage_rate: float; unexpected_placeholders: int; instruction_score: float
    repetition_rate: float; text_limit_violations: int; average_latency_seconds: float
    average_tokens: float | None; api_errors: int


DEFAULT_CASES = [
    {"topic": "Quarterly business review", "source_content": "Revenue grew 12%; margin improved 2 points.", "audience": "executives", "tone": "concise"},
    {"topic": "Product launch", "source_content": "Launch includes onboarding, analytics, and governance.", "audience": "customers", "tone": "confident"},
    {"topic": "Cybersecurity awareness", "source_content": "Use MFA, report phishing, and update devices.", "audience": "employees", "tone": "clear"},
]


def evaluate_models(model_ids: list[str], template: dict[str, Any], request: Callable[[str, dict[str, str]], Any], cases: list[dict[str, str]] | None = None) -> list[EvaluationResult]:
    results = []
    expected = sum(1 for slide in template["slides"] for shape in slide["shapes"] if shape["is_placeholder"] and shape["has_text_frame"] and shape["placeholder_idx"] is not None)
    for model_id in model_ids:
        rows = []
        for case in (cases or DEFAULT_CASES):
            started = time.perf_counter(); json_ok = schema_ok = 0; errors = unexpected = violations = 0; repetition = 0.0; tokens = None
            try:
                result = request(model_id, case); elapsed = time.perf_counter() - started
                parsed = extract_json(result.text); json_ok = 1
                supplied = sum(len(slide.get("placeholders", [])) for slide in parsed.get("slides", []))
                coverage = min(1.0, supplied / max(expected, 1))
                try:
                    _, warnings = validate_response(parsed, template); schema_ok = 1
                    repetition = 1.0 if any("Duplicate content" in warning for warning in warnings) else 0.0
                except ResponseValidationError as exc:
                    unexpected = sum("Unknown placeholder" in item for item in exc.errors); violations = sum("exceeds" in item for item in exc.errors)
                tokens = (result.input_tokens or 0) + (result.output_tokens or 0) or None
            except Exception:
                elapsed = time.perf_counter() - started; coverage = 0.0; errors = 1
            quality = max(0.0, coverage - repetition * .3 - violations * .1)
            instruction = schema_ok
            rows.append((json_ok, schema_ok, coverage, unexpected, instruction, repetition, violations, elapsed, tokens, errors, quality))
        latency = mean(row[7] for row in rows); token_values = [row[8] for row in rows if row[8] is not None]
        latency_score = 1 / (1 + latency / 10); token_score = 1 / (1 + (mean(token_values) if token_values else 4000) / 4000)
        score = 100 * (WEIGHTS["json_schema"] * mean((r[0] + r[1]) / 2 for r in rows) + WEIGHTS["coverage"] * mean(r[2] for r in rows) + WEIGHTS["quality"] * mean(r[10] for r in rows) + WEIGHTS["instruction"] * mean(r[4] for r in rows) + WEIGHTS["latency"] * latency_score + WEIGHTS["tokens"] * token_score)
        results.append(EvaluationResult(model_id, round(score, 2), mean(r[0] for r in rows), mean(r[1] for r in rows), mean(r[2] for r in rows), sum(r[3] for r in rows), mean(r[4] for r in rows), mean(r[5] for r in rows), sum(r[6] for r in rows), round(latency, 3), round(mean(token_values), 1) if token_values else None, sum(r[9] for r in rows)))
    return sorted(results, key=lambda item: item.score, reverse=True)


def recommendations(results: list[EvaluationResult]) -> dict[str, str | None]:
    reliable = [r for r in results if r.schema_success_rate >= .8]
    pool = reliable or results
    return {"best_overall": results[0].model_id if results else None, "fastest_reliable": min(pool, key=lambda r: r.average_latency_seconds).model_id if pool else None, "most_token_efficient": min((r for r in pool if r.average_tokens is not None), key=lambda r: r.average_tokens, default=None).model_id if pool else None}


def results_as_dicts(results: list[EvaluationResult]) -> list[dict[str, Any]]: return [asdict(item) for item in results]
