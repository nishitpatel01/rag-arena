"""LLM-as-a-judge providers (spec §6.5): Claude Opus on Vertex rawPredict (default) or a distinct Gemini judge.

All HTTP is mocked (``httpx.post``) and ADC is mocked (``get_access_token``); no GCP call can happen.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.core.models import EngineResult
from app.core.telemetry import RecordingStageEmitter
from app.evaluation.evaluator import evaluate_engine_result, run_evaluation
from app.evaluation.judges import (
    AnthropicVertexJudge,
    GeminiJudge,
    JudgeHTTPError,
    JudgeResponseError,
    build_judge,
    judge_service_label,
    parse_judge_json,
)
from fakes import FakeGenaiClient, anthropic_message, fake_clients, live_settings, run

_VALID = '{"groundedness": 0.9, "qa_relevance": 0.75, "context_precision": 0.5, "rationale": "All claims cited."}'


def _response(status: int, body: dict[str, Any] | None = None, text: str = "") -> httpx.Response:
    request = httpx.Request("POST", "https://aiplatform.googleapis.com/v1/x:rawPredict")
    if body is not None:
        return httpx.Response(status, json=body, request=request)
    return httpx.Response(status, text=text, request=request)


def _raised(fn: Any, exc_type: type[BaseException]) -> BaseException:
    """Return the exception ``fn()`` raises (the offline pytest shim's ``raises`` exposes no excinfo)."""
    try:
        fn()
    except exc_type as exc:
        return exc
    raise AssertionError(f"DID NOT RAISE {exc_type.__name__}")


def _judge(settings: Any = None, sleeps: list[float] | None = None) -> AnthropicVertexJudge:
    record = sleeps if sleeps is not None else []
    return AnthropicVertexJudge(
        settings or live_settings(), token_provider=lambda: "fake-token", sleep=record.append
    )


# ---------------------------------------------------------------- strict JSON parsing


def test_parse_judge_json_accepts_plain_fenced_and_prose_wrapped_objects() -> None:
    for text in (
        _VALID,
        f"```json\n{_VALID}\n```",
        f"Here is my assessment:\n{_VALID}\nThanks.",
    ):
        metrics, rationale = parse_judge_json(text)
        assert (metrics.groundedness, metrics.qa_relevance, metrics.context_precision) == (0.9, 0.75, 0.5)
        assert rationale == "All claims cited."


def test_parse_judge_json_rejects_missing_keys_out_of_range_and_non_json() -> None:
    bad = (
        '{"groundedness": 0.9, "qa_relevance": 0.7, "rationale": "x"}',  # missing context_precision
        '{"groundedness": 0.9, "qa_relevance": 0.7, "context_precision": 0.5}',  # missing rationale
        '{"groundedness": 1.2, "qa_relevance": 0.7, "context_precision": 0.5, "rationale": "x"}',
        '{"groundedness": -0.1, "qa_relevance": 0.7, "context_precision": 0.5, "rationale": "x"}',
        '{"groundedness": true, "qa_relevance": 0.7, "context_precision": 0.5, "rationale": "x"}',
        '{"groundedness": "0.9", "qa_relevance": 0.7, "context_precision": 0.5, "rationale": "x"}',
        '{"groundedness": 0.9, "qa_relevance": 0.7, "context_precision": 0.5, "rationale": ""}',
        "I cannot score this answer.",
        "",
        "[0.9, 0.7, 0.5]",
    )
    for text in bad:
        with pytest.raises(JudgeResponseError):
            parse_judge_json(text)


# ---------------------------------------------------------------- Anthropic on Vertex (rawPredict)


def test_anthropic_judge_posts_raw_predict_to_the_global_endpoint_and_captures_usage() -> None:
    judge = _judge()
    with patch("app.evaluation.judges.httpx.post", return_value=_response(200, anthropic_message(_VALID))) as post:
        verdict = judge.score("RUBRIC", "CASE")
    url = post.call_args.args[0]
    assert url == (
        "https://aiplatform.googleapis.com/v1/projects/test-project/locations/global/"
        "publishers/anthropic/models/claude-opus-5-5:rawPredict"
    )
    kwargs = post.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer fake-token"
    body = kwargs["json"]
    assert body["anthropic_version"] == "vertex-2023-10-16" and body["max_tokens"] == 1024
    assert body["system"] == "RUBRIC" and body["messages"] == [{"role": "user", "content": "CASE"}]
    assert "model" not in body  # the model is in the rawPredict URL
    assert "temperature" not in body  # Opus 5.5 on Vertex rejects it: "`temperature` is deprecated for this model."
    assert verdict.metrics.groundedness == 0.9 and verdict.rationale == "All claims cited."
    assert (verdict.input_tokens, verdict.output_tokens) == (1500, 80)
    assert verdict.response_id == "msg_vrtx_fake01" and verdict.attempts == 1


def test_anthropic_judge_uses_regional_host_for_a_regional_location() -> None:
    judge = _judge(live_settings(judge_location="us-east5"))
    assert judge.endpoint_url.startswith("https://us-east5-aiplatform.googleapis.com/v1/projects/test-project/locations/us-east5/")


def test_anthropic_judge_retries_only_retryable_codes_and_is_bounded_to_three_attempts() -> None:
    sleeps: list[float] = []
    judge = _judge(sleeps=sleeps)
    replies = [_response(429, text="quota"), _response(503, text="unavailable"), _response(500, text="boom")]
    with patch("app.evaluation.judges.httpx.post", side_effect=replies) as post:
        error = _raised(lambda: judge.score("RUBRIC", "CASE"), JudgeHTTPError)
    assert post.call_count == 3 and error.status_code == 500
    assert sleeps == [1.0, 2.0]  # exponential backoff between attempts, none after the last


def test_anthropic_judge_recovers_after_a_retryable_failure_and_retries_timeouts() -> None:
    sleeps: list[float] = []
    judge = _judge(sleeps=sleeps)
    replies = [httpx.ReadTimeout("slow"), _response(429, text="quota"), _response(200, anthropic_message(_VALID))]
    with patch("app.evaluation.judges.httpx.post", side_effect=replies) as post:
        verdict = judge.score("RUBRIC", "CASE")
    assert post.call_count == 3 and verdict.attempts == 3 and sleeps == [1.0, 2.0]


def test_anthropic_judge_does_not_retry_400_and_redacts_the_project_id() -> None:
    sleeps: list[float] = []
    judge = _judge(sleeps=sleeps)
    reply = _response(400, text='{"error": "bad request for projects/test-project/locations/global"}')
    with patch("app.evaluation.judges.httpx.post", return_value=reply) as post:
        error = _raised(lambda: judge.score("RUBRIC", "CASE"), JudgeHTTPError)
    assert post.call_count == 1 and sleeps == [] and error.status_code == 400
    assert "test-project" not in str(error) and "${GCP_PROJECT_ID}" in str(error)


def test_anthropic_judge_rejects_unparseable_model_output_without_retrying() -> None:
    judge = _judge()
    with patch("app.evaluation.judges.httpx.post", return_value=_response(200, anthropic_message("No JSON here."))) as post:
        with pytest.raises(JudgeResponseError):
            judge.score("RUBRIC", "CASE")
    assert post.call_count == 1


# ---------------------------------------------------------------- factory, labels, evaluator wiring


def test_build_judge_selects_provider_from_settings() -> None:
    assert isinstance(build_judge(live_settings()), AnthropicVertexJudge)
    gemini = build_judge(live_settings(judge_provider="gemini", judge_model="gemini-4-argon-judge"))
    assert isinstance(gemini, GeminiJudge) and gemini.model == "gemini-4-argon-judge"
    assert judge_service_label(live_settings()) == "Vertex AI · Claude Opus 5.5 (judge)"


def _engine_result() -> EngineResult:
    return EngineResult(
        engine_id="level1_naive", engine_label="L1", answer="CLIN-0004-AB is $210.00/hr.", citations=[],
        retrieved_chunks=[], agent_steps=[], latency_ms=10, cost_usd=0.01,
    )


def test_evaluate_engine_result_returns_judge_provenance_tokens_and_cost() -> None:
    with fake_clients():
        outcome = evaluate_engine_result("What is the rate?", _engine_result(), "Ref answer", live_settings())
    assert outcome.judge_model == "claude-opus-5-5" and outcome.judge_provider == "anthropic_vertex"
    assert outcome.judge_location == "global" and outcome.reference_answer_used is True
    assert (outcome.input_tokens, outcome.output_tokens) == (1500, 80)
    assert outcome.judge_cost_usd == pytest.approx(1500 / 1e6 * 4.0 + 80 / 1e6 * 20.0)
    assert outcome.rationale and len(outcome.rationale) <= 600


def test_evaluate_stage_request_and_payload_carry_judge_fields() -> None:
    emitter = RecordingStageEmitter(run_id="run_x", engine="level1_naive")
    long_rationale = "R" * 900
    text = (
        '{"groundedness": 0.81, "qa_relevance": 0.64, "context_precision": 0.58, "rationale": "%s"}' % long_rationale
    )
    with fake_clients(judge_text=text):
        run(run_evaluation("Q?", _engine_result(), question_id="q_exact_clin_lookup", settings=live_settings(), emitter=emitter))
    started, completed = emitter.events[0], emitter.events[-1]
    assert started["event"] == "stage_started" and started["service"] == "Vertex AI · Claude Opus 5.5 (judge)"
    request = started["request"]
    assert request["judge_provider"] == "anthropic_vertex" and request["judge_model"] == "claude-opus-5-5"
    assert request["judge_location"] == "global" and request["reference_answer_used"] is True
    assert "temperature" not in request and request["max_tokens"] == 1024
    payload = completed["payload"]
    for key in ("judge_provider", "judge_model", "judge_location", "input_tokens", "output_tokens", "rationale", "judge_cost_usd"):
        assert key in payload, key
    assert payload["judge_model"] == "claude-opus-5-5" and payload["input_tokens"] == 1500
    assert len(payload["rationale"]) == 600 and payload["groundedness"] == 0.81


def test_claude_failure_fails_the_evaluate_stage_with_no_silent_gemini_fallback() -> None:
    emitter = RecordingStageEmitter(run_id="run_x", engine="level1_naive")
    genai = FakeGenaiClient()
    with fake_clients(genai=genai, judge_status=403):
        with pytest.raises(JudgeHTTPError):
            run(run_evaluation("Q?", _engine_result(), question_id=None, settings=live_settings(), emitter=emitter))
    assert [e["event"] for e in emitter.events] == ["stage_started", "stage_failed"]
    assert emitter.events[-1]["error_type"] == "JudgeHTTPError"
    assert genai.calls == []  # Gemini was never asked to judge


def test_health_exposes_the_judge_provider_and_model() -> None:
    from starlette.testclient import TestClient

    from app.core.config import ArenaSettings
    from app.main import create_app

    health = TestClient(create_app(settings=ArenaSettings(demo_mode="replay"))).get("/api/v1/health").json()
    assert health["judge_provider"] == "anthropic_vertex" and health["judge_model"] == "claude-opus-5-5"
    assert health["judge_location"] == "global"


def test_battle_stream_surfaces_judge_failure_as_engine_error_at_evaluate() -> None:
    import json as _json

    from starlette.testclient import TestClient

    from app.main import create_app

    genai = FakeGenaiClient()
    app = create_app(settings=live_settings())
    with fake_clients(genai=genai, judge_status=400), TestClient(app) as client:
        resp = client.post("/api/v1/battle/stream", json={"query": "What is the CLIN rate?", "question_id": "q_exact_clin_lookup", "engines": ["level1_naive"]})
    events = [_json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]
    failed = [e for e in events if e["event"] == "stage_failed"]
    assert failed and failed[-1]["stage"] == "evaluate"
    error = next(e for e in events if e["event"] == "engine_error")
    assert error["failed_stage"] == "evaluate" and error["error_type"] == "JudgeHTTPError"
    assert not [e for e in events if e["event"] == "eval_complete"]
    assert [name for name, _ in genai.calls] == ["embed_content", "generate_content"]  # synthesis only, no judge call


def test_eval_complete_reports_judge_cost_and_run_cost() -> None:
    import json as _json

    from starlette.testclient import TestClient

    from app.main import create_app

    app = create_app(settings=live_settings())
    with fake_clients(), TestClient(app) as client:
        resp = client.post("/api/v1/battle/stream", json={"query": "What is the CLIN rate?", "question_id": "q_exact_clin_lookup", "engines": ["level1_naive"]})
    events = [_json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]
    answer = next(e for e in events if e["event"] == "answer_complete")
    evaluation = next(e for e in events if e["event"] == "eval_complete")
    assert evaluation["judge_model"] == "claude-opus-5-5" and evaluation["judge_cost_usd"] > 0
    assert evaluation["run_cost_usd"] == pytest.approx(answer["cost_usd"] + evaluation["judge_cost_usd"])
