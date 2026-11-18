"""TDD suite for Slice #4: Vertex AI GenAI Evaluation Service (:evaluateInstances) + Split Engine vs. Judge Economics.

All HTTP calls are mocked (``httpx.post``) and ADC is mocked; zero live GCP traffic occurs.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from starlette.testclient import TestClient

from app.core.config import ArenaSettings
from app.core.models import BoundingBox, DocumentChunk, EngineResult, EvalMetrics
from app.core.models_catalog import (
    ModelSelectionError,
    PROVIDER_VERTEX_GENAI_EVAL,
    VERTEX_GENAI_EVAL_LABEL,
    VERTEX_GENAI_EVAL_MODEL_ID,
    load_catalog,
)
from app.core.pricing import PRICE_TABLE
from app.core.telemetry import RecordingStageEmitter
from app.evaluation.evaluator import evaluate_engine_result, run_evaluation
from app.evaluation.judges import (
    JudgeHTTPError,
    JudgeResponseError,
    VertexGenAIEvalJudge,
    build_judge,
    judge_display_name,
    judge_service_label,
)
from app.evaluation.leaderboard import (
    LeaderboardFilters,
    RunSample,
    aggregate_leaderboard,
    leaderboard_from_recordings,
)
from app.evaluation.scoring import battle_score, efficiency_score, resolve_engine_cost_usd
from app.evaluation.vertex_eval import (
    EVAL_METRIC_NAMES,
    JudgeResult,
    VertexEvalClient,
    VertexEvalResult,
    build_evaluate_instance_requests,
    evaluate_instances_url,
    normalize_likert_score,
)
from app.main import create_app
from app.recording.recorder import build_recording, record_run
from app.recording.store import RecordingStore
from fakes import live_settings, run, write_verification_file

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RECORDINGS = _PROJECT_ROOT / "data" / "recordings"


def _raised(fn: Any, exc_type: type[BaseException]) -> BaseException:
    try:
        fn()
    except exc_type as exc:
        return exc
    raise AssertionError(f"Expected {exc_type.__name__} to be raised")


def _sample_chunk(chunk_id: str = "c1", content: str = "CLIN-0004-AB is $245.00/hr under Amendment 0001.") -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        tier_profile="hybrid",
        solicitation_id="solicitation_01",
        doc_name="06_Amendment_0001_Scope_Update.pdf",
        doc_type="AMENDMENT",
        element_type="narrative_text",
        section_id="AMD-0001-A",
        amendment_number=1,
        effective_date="2026-02-10",
        amends_section="CLIN-0004-AB",
        page_number=1,
        bbox=BoundingBox(page=1, x0=0.1, y0=0.2, x1=0.9, y1=0.4),
        content=content,
        score=0.91,
    )


def _sample_engine_result(*, with_chunks: bool = True, cost_usd: float = 0.0085) -> EngineResult:
    return EngineResult(
        engine_id="level2_hybrid",
        engine_label="Level 2: BigQuery Hybrid + Reranker",
        answer="Amendment 0001 sets CLIN-0004-AB to $245.00/hr.",
        citations=[],
        retrieved_chunks=[_sample_chunk()] if with_chunks else [],
        agent_steps=[],
        latency_ms=9800,
        cost_usd=cost_usd,
    )


def _eval_http_router(
    *,
    groundedness: float = 0.95,
    qa_likert: float = 4.6,
    fulfillment_likert: float = 4.2,
    usage_tokens: tuple[int, int] | None = (620, 48),
) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _post(url: str, **kwargs: Any) -> httpx.Response:
        body = kwargs.get("json") or {}
        calls.append((url, body))
        req = httpx.Request("POST", url)
        usage = (
            {"usageMetadata": {"promptTokenCount": usage_tokens[0], "candidatesTokenCount": usage_tokens[1]}}
            if usage_tokens is not None
            else {}
        )
        if "groundednessInput" in body:
            payload = {
                "groundednessResult": {
                    "score": groundedness,
                    "explanation": "Every numeric claim traces directly to Amendment 0001.",
                    "confidence": 0.98,
                },
                **usage,
            }
        elif "questionAnsweringQualityInput" in body:
            payload = {
                "questionAnsweringQualityResult": {
                    "score": qa_likert,
                    "explanation": "Directly answers the CLIN rate question and matches the reference.",
                    "confidence": 0.94,
                },
                **usage,
            }
        elif "fulfillmentInput" in body:
            payload = {
                "fulfillmentResult": {
                    "score": fulfillment_likert,
                    "explanation": "Retrieved excerpts focus on CLIN-0004-AB with minimal noise.",
                    "confidence": 0.91,
                },
                **usage,
            }
        else:
            return httpx.Response(400, json={"error": "unknown evaluateInstances metric input"}, request=req)
        return httpx.Response(200, json=payload, request=req)

    return _post, calls


# ---------------------------------------------------------------- 1. Split Engine vs. Judge Economics


def test_battle_score_efficiency_excludes_judge_overhead_and_exposes_breakdown_costs() -> None:
    metrics = EvalMetrics(groundedness=0.98, qa_relevance=0.95, context_precision=0.80)
    cheap_judge = battle_score(metrics, latency_ms=10_000, cost_usd=0.0085, judge_cost_usd=0.0012)
    expensive_opus_judge = battle_score(metrics, latency_ms=10_000, cost_usd=0.0085, judge_cost_usd=0.0485)

    # Identical engine performance + quality MUST yield identical Battle Score regardless of judge token price!
    assert cheap_judge.efficiency == expensive_opus_judge.efficiency == efficiency_score(10_000, 0.0085)
    assert cheap_judge.battle_score == expensive_opus_judge.battle_score
    # Previously, adding $0.0485 judge spend penalized efficiency from 0.895 -> 0.774 (-1.2 Battle Score pts).
    distorted = battle_score(metrics, latency_ms=10_000, cost_usd=0.0085 + 0.0485)
    assert cheap_judge.battle_score > distorted.battle_score

    d = expensive_opus_judge.to_dict()
    assert d["cost_usd"] == pytest.approx(0.0085)
    assert d["engine_cost_usd"] == pytest.approx(0.0085)
    assert d["judge_cost_usd"] == pytest.approx(0.0485)
    assert d["total_cost_usd"] == pytest.approx(0.0570)

    with pytest.raises(ValueError):
        battle_score(metrics, latency_ms=10_000, cost_usd=0.01, judge_cost_usd=-0.001)


def test_resolve_engine_cost_usd_handles_explicit_subtracted_and_legacy_inputs() -> None:
    assert resolve_engine_cost_usd(0.0476, engine_cost_usd=0.0110, judge_cost_usd=0.0366) == pytest.approx(0.0110)
    # Fallback on total - judge when engine_cost_usd is omitted
    assert resolve_engine_cost_usd(0.0476, judge_cost_usd=0.0366) == pytest.approx(0.0110)
    # Clamps tiny floating-point subtraction underflow cleanly to 0.0
    assert resolve_engine_cost_usd(0.0200, judge_cost_usd=0.0200001) == 0.0
    # Legacy query_runs row with only total cost_usd
    assert resolve_engine_cost_usd(0.0500) == pytest.approx(0.0500)
    assert resolve_engine_cost_usd(None) is None
    with pytest.raises(ValueError):
        resolve_engine_cost_usd(-0.01)


def test_leaderboard_splits_engine_and_judge_costs_across_all_135_official_recordings() -> None:
    board = leaderboard_from_recordings(RecordingStore(_RECORDINGS))
    rows = [r for g in board["groups"] for r in g["rows"]]
    assert sum(r["runs"] for r in rows) == 135

    total_engine_spend = 0.0
    total_judge_spend = 0.0
    for row in rows:
        eng = row["avg_engine_cost_usd"]
        jud = row["avg_judge_cost_usd"]
        tot = row["avg_cost_usd"]
        assert isinstance(eng, float) and isinstance(jud, float) and isinstance(tot, float)
        assert eng > 0.0 and jud > 0.0
        assert eng + jud == pytest.approx(tot, abs=2e-5)
        total_engine_spend += eng * row["runs"]
        total_judge_spend += jud * row["runs"]

    # Confirm the 53.65% benchmark judge-spend distortion is surfaced transparently, never hidden
    judge_share = total_judge_spend / (total_engine_spend + total_judge_spend)
    assert 0.53 <= judge_share <= 0.54

    # Check L6 Flash specifically: ~$0.0110 engine cost vs ~$0.0366 judge cost
    flash_group = next(
        g for g in board["groups"] if g["synthesis_model"] == "gemini-3.8-flash" and g["judge_model"] == "claude-opus-5-5"
    )
    l6_flash = next(r for r in flash_group["rows"] if r["engine"] == "rag_engine")
    assert l6_flash["avg_engine_cost_usd"] == pytest.approx(0.0110, abs=0.001)
    assert l6_flash["avg_judge_cost_usd"] == pytest.approx(0.0366, abs=0.001)

    # Verify fallback when RunSample only carries cost_usd + judge_cost_usd (without explicit engine_cost_usd)
    fallback_sample = RunSample(
        engine="rag_engine",
        synthesis_model="gemini-3.8-flash",
        judge_model="claude-opus-5-5",
        question_id="q1",
        run_at="2026-10-25T00:00:00Z",
        git_sha="abc1234",
        demo_mode="record",
        latency_ms=15_000,
        cost_usd=0.0476,
        metrics=EvalMetrics(groundedness=0.99, qa_relevance=0.96, context_precision=0.46),
        engine_cost_usd=None,
        judge_cost_usd=0.0366,
    )
    agg = aggregate_leaderboard([fallback_sample], source="recordings", filters=LeaderboardFilters(), level_labels={})
    agg_row = agg["groups"][0]["rows"][0]
    assert agg_row["avg_engine_cost_usd"] == pytest.approx(0.0110)
    assert agg_row["avg_judge_cost_usd"] == pytest.approx(0.0366)
    assert agg_row["avg_cost_usd"] == pytest.approx(0.0476)
    expected_bs = battle_score(fallback_sample.metrics, 15_000, 0.0110).battle_score  # type: ignore[arg-type]
    assert agg_row["avg_battle_score"] == expected_bs


# ---------------------------------------------------------------- 2. Vertex AI GenAI Evaluation Service


def test_evaluate_instances_url_and_payload_builders_follow_v1beta1_contract() -> None:
    assert evaluate_instances_url("acme-eval", "us-central1") == (
        "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/acme-eval/locations/us-central1:evaluateInstances"
    )
    assert evaluate_instances_url("acme-eval", "global") == (
        "https://aiplatform.googleapis.com/v1beta1/projects/acme-eval/locations/global:evaluateInstances"
    )
    for bad_proj, bad_loc in (("", "us-central1"), ("acme/evil", "us-central1"), ("acme", "us central1")):
        with pytest.raises(ValueError):
            evaluate_instances_url(bad_proj, bad_loc)

    assert EVAL_METRIC_NAMES == ("GROUNDEDNESS", "QUESTION_ANSWERING_QUALITY", "FULFILLMENT")
    reqs_with_ref = build_evaluate_instance_requests(
        query="What is the CLIN-0004-AB rate?",
        context="Amendment 0001 sets CLIN-0004-AB to $245/hr.",
        candidate_answer="CLIN-0004-AB is $245/hr.",
        reference_answer="$245.00/hr under Amendment 0001.",
    )
    assert list(reqs_with_ref) == ["GROUNDEDNESS", "QUESTION_ANSWERING_QUALITY", "FULFILLMENT"]
    assert reqs_with_ref["GROUNDEDNESS"]["groundednessInput"]["instance"] == {
        "prediction": "CLIN-0004-AB is $245/hr.",
        "context": "Amendment 0001 sets CLIN-0004-AB to $245/hr.",
    }
    qa_in = reqs_with_ref["QUESTION_ANSWERING_QUALITY"]["questionAnsweringQualityInput"]
    assert qa_in["metricSpec"] == {"useReference": True}
    assert qa_in["instance"]["reference"] == "$245.00/hr under Amendment 0001."
    assert "fulfillmentInput" in reqs_with_ref["FULFILLMENT"]
    assert set(reqs_with_ref["FULFILLMENT"]["fulfillmentInput"]["instance"]) == {"instruction", "prediction"}

    reqs_no_ref = build_evaluate_instance_requests(
        query="What is the CLIN-0004-AB rate?",
        context="",
        candidate_answer="CLIN-0004-AB is $245/hr.",
        reference_answer=None,
    )
    qa_no_ref = reqs_no_ref["QUESTION_ANSWERING_QUALITY"]["questionAnsweringQualityInput"]
    assert qa_no_ref["metricSpec"] == {"useReference": False}
    assert "reference" not in qa_no_ref["instance"]
    # Empty context uses an explicit placeholder rather than sending an empty string that 400s on Vertex
    assert reqs_no_ref["GROUNDEDNESS"]["groundednessInput"]["instance"]["context"] == "[no retrieved context]"


def test_likert_normalization_and_vertex_eval_client_happy_path() -> None:
    assert normalize_likert_score(1.0, "FULFILLMENT") == 0.0
    assert normalize_likert_score(3.0, "FULFILLMENT") == 0.5
    assert normalize_likert_score(5.0, "FULFILLMENT") == 1.0
    for invalid in (0.5, 5.1, True, "4.0"):
        with pytest.raises(JudgeResponseError):
            normalize_likert_score(invalid, "FULFILLMENT")  # type: ignore[arg-type]

    router, calls = _eval_http_router(groundedness=0.92, qa_likert=4.6, fulfillment_likert=3.8, usage_tokens=(500, 40))
    client = VertexEvalClient(
        project_id="acme-eval",
        location="us-central1",
        attempt_timeout_seconds=30.0,
        budget_seconds=90.0,
        token_provider=lambda: "fake-ya29-token",
        autorater_price=PRICE_TABLE["gemini-3.8-flash"],
    )
    with patch("app.evaluation.vertex_eval.httpx.post", side_effect=router):
        res = client.evaluate(
            query="What is the CLIN-0004-AB rate?",
            context="Amendment 0001 revises CLIN-0004-AB to $245.00/hr.",
            candidate_answer="CLIN-0004-AB is $245.00/hr.",
            reference_answer="$245.00/hr",
        )
    assert isinstance(res, VertexEvalResult) and JudgeResult is VertexEvalResult
    assert len(calls) == 3
    assert res.groundedness == 0.92
    assert res.qa_relevance == pytest.approx((4.6 - 1.0) / 4.0)
    assert res.context_precision == pytest.approx((3.8 - 1.0) / 4.0)
    assert (res.input_tokens, res.output_tokens) == (1500, 120)
    expected_cost = (1500 / 1e6) * 0.75 + (120 / 1e6) * 3.75
    assert res.eval_cost_usd == pytest.approx(expected_cost)
    assert "[Groundedness]" in res.rationale and "[QA Quality]" in res.rationale
    assert len(res.rationale) <= 600 and res.latency_ms >= 1 and res.attempts == 3


def test_vertex_eval_client_estimates_tokens_when_usage_metadata_is_absent() -> None:
    router, _ = _eval_http_router(groundedness=1.0, qa_likert=5.0, fulfillment_likert=5.0, usage_tokens=None)
    client = VertexEvalClient(
        project_id="acme-eval",
        location="us-central1",
        attempt_timeout_seconds=30.0,
        budget_seconds=90.0,
        token_provider=lambda: "fake-ya29-token",
        autorater_price=PRICE_TABLE["gemini-3.8-flash"],
    )
    with patch("app.evaluation.vertex_eval.httpx.post", side_effect=router):
        res = client.evaluate(
            query="What is the CLIN rate?",
            context="CLIN-0004-AB is $245.00/hr.",
            candidate_answer="$245.00/hr",
            reference_answer=None,
        )
    assert (res.groundedness, res.qa_relevance, res.context_precision) == (1.0, 1.0, 1.0)
    assert res.input_tokens > 0 and res.output_tokens > 0 and res.eval_cost_usd > 0.0


def test_vertex_eval_retries_transient_errors_and_redacts_project_ids_and_numbers() -> None:
    sleeps: list[float] = []
    router, _ = _eval_http_router()
    url = "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/acme-eval/locations/us-central1:evaluateInstances"
    req = httpx.Request("POST", url)
    calls = 0

    def _flaky(u: str, **kwargs: Any) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timed out")
        if calls == 2:
            return httpx.Response(503, text="service unavailable", request=req)
        return router(u, **kwargs)

    client = VertexEvalClient(
        project_id="acme-eval",
        location="us-central1",
        attempt_timeout_seconds=15.0,
        budget_seconds=90.0,
        token_provider=lambda: "tok",
        sleep=sleeps.append,
    )
    with patch("app.evaluation.vertex_eval.httpx.post", side_effect=_flaky):
        res = client.evaluate(query="Q?", context="ctx", candidate_answer="ans", reference_answer=None)
    assert calls == 5 and res.attempts == 5 and sleeps == [1.0, 2.0]

    # Non-retryable 403 fails immediately and redacts both project_id and 6+ digit project numbers
    dyn_pnum = "8" * 9
    err_text = f"Permission denied on projects/acme-eval and projects/{dyn_pnum}/locations/us-central1"
    sleeps.clear()
    with patch(
        "app.evaluation.vertex_eval.httpx.post",
        return_value=httpx.Response(403, text=err_text, request=req),
    ) as post_403:
        err = _raised(
            lambda: client.evaluate(query="Q?", context="ctx", candidate_answer="ans", reference_answer=None),
            JudgeHTTPError,
        )
    assert post_403.call_count == 1 and sleeps == [] and err.status_code == 403
    assert "acme-eval" not in str(err) and "${GCP_PROJECT_ID}" in str(err)
    assert dyn_pnum not in str(err) and "projects/${GCP_PROJECT_NUMBER}" in str(err)


def test_catalog_registers_vertex_genai_eval_as_judge_only_and_gates_replay_honestly() -> None:
    assert VERTEX_GENAI_EVAL_MODEL_ID == "vertex-genai-eval"
    assert VERTEX_GENAI_EVAL_LABEL == "Vertex AI GenAI Eval Service"
    assert judge_display_name("vertex-genai-eval") == "Vertex AI GenAI Eval Service"

    # 1. Live mode: vertex-genai-eval is available as judge_only (never synthesis)
    live_cat = load_catalog(live_settings())
    spec = live_cat.get("vertex-genai-eval")
    assert spec.provider == PROVIDER_VERTEX_GENAI_EVAL
    assert spec.roles == ("judge",)
    assert spec.location == "us-central1"
    assert spec.available is True and spec.unavailable_reason is None
    assert spec.input_usd_per_1m == PRICE_TABLE["gemini-3.8-flash"].input_usd_per_1m

    with pytest.raises(ModelSelectionError):
        live_cat.require("vertex-genai-eval", "synthesis")
    # Distinct from every synthesis model -> judge != synthesis always holds
    for synth_id in ("gemini-4-argon", "gemini-3.8-flash", "claude-opus-5-5"):
        pair = live_cat.select(synth_id, "vertex-genai-eval")
        assert pair.pair == (synth_id, "vertex-genai-eval")

    # 2. Replay mode against official data/recordings (0 vertex-genai-eval recordings): honestly unavailable!
    replay_cat = load_catalog(
        ArenaSettings(demo_mode="replay", model_verification_file=str(write_verification_file()))
    )
    replay_spec = replay_cat.get("vertex-genai-eval")
    assert replay_spec.available is False
    assert replay_spec.unavailable_reason and "recordings" in replay_spec.unavailable_reason.lower()

    # 3. Replay mode when a recording DOES exist for vertex-genai-eval: available=True!
    with tempfile.TemporaryDirectory() as tmpdir:
        rec_root = Path(tmpdir)
        router, _ = _eval_http_router()
        rec_settings = live_settings(demo_mode="record", recordings_dir=str(rec_root))
        models = load_catalog(rec_settings).select("gemini-3.8-flash", "vertex-genai-eval")
        emitter = RecordingStageEmitter(run_id="r1", engine="level2_hybrid")
        with (
            patch("app.evaluation.vertex_eval.get_access_token", lambda s: "tok"),
            patch("app.evaluation.vertex_eval.httpx.post", side_effect=router),
        ):
            outcome = run(
                run_evaluation(
                    "What is CLIN-0004-AB?",
                    _sample_engine_result(),
                    question_id="q_exact_clin_lookup",
                    settings=rec_settings,
                    emitter=emitter,
                    models=models,
                )
            )
        recording = build_recording(
            settings=rec_settings,
            engine_id="level2_hybrid",
            question_id="q_exact_clin_lookup",
            query="What is CLIN-0004-AB?",
            run_id="r1",
            events=emitter.events,
            result=_sample_engine_result(),
            evaluation=outcome,
            models=models,
        )
        record_run(RecordingStore(rec_root), recording)

        replay_with_rec = ArenaSettings(
            demo_mode="replay",
            recordings_dir=str(rec_root),
            model_verification_file=str(write_verification_file()),
        )
        avail_spec = load_catalog(replay_with_rec).get("vertex-genai-eval")
        assert avail_spec.available is True and avail_spec.unavailable_reason is None
        models_body = TestClient(create_app(settings=replay_with_rec)).get("/api/v1/models").json()
        assert any(p["judge_model"] == "vertex-genai-eval" for p in models_body["recorded_pairs"])


def test_evaluator_and_judges_run_vertex_genai_eval_end_to_end_and_respect_upload_scope() -> None:
    from app.evaluation.judges import judge_service_label_for

    settings = live_settings()
    spec = load_catalog(settings).get("vertex-genai-eval")
    assert judge_service_label_for(spec.label) == "Vertex AI · Vertex AI GenAI Eval Service (judge)"
    judge = build_judge(settings, spec)
    assert isinstance(judge, VertexGenAIEvalJudge)

    router, calls = _eval_http_router(groundedness=0.88, qa_likert=4.2, fulfillment_likert=4.0)
    emitter = RecordingStageEmitter(run_id="run_eval_1", engine="level2_hybrid")
    models = load_catalog(settings).select("gemini-3.8-flash", "vertex-genai-eval")
    with (
        patch("app.evaluation.vertex_eval.get_access_token", lambda s: "tok"),
        patch("app.evaluation.vertex_eval.httpx.post", side_effect=router),
    ):
        outcome = run(
            run_evaluation(
                "What is the base hourly rate for CLIN-0004-AB?",
                _sample_engine_result(),
                question_id="q_exact_clin_lookup",
                solicitation_filter="upl_0123456789ab",
                settings=settings,
                emitter=emitter,
                models=models,
            )
        )
    assert outcome.judge_model == "vertex-genai-eval"
    assert outcome.judge_provider == PROVIDER_VERTEX_GENAI_EVAL
    assert outcome.judge_location == "us-central1"
    assert outcome.reference_answer_used is False
    assert outcome.metrics == EvalMetrics(groundedness=0.88, qa_relevance=0.8, context_precision=0.75)
    assert outcome.judge_cost_usd > 0.0

    # Upload scope must force reference-free evaluation in QUESTION_ANSWERING_QUALITY
    qa_body = next(b for _, b in calls if "questionAnsweringQualityInput" in b)
    assert qa_body["questionAnsweringQualityInput"]["metricSpec"]["useReference"] is False
    assert "reference" not in qa_body["questionAnsweringQualityInput"]["instance"]

    started, completed = emitter.events[0], emitter.events[-1]
    assert started["service"] == "Vertex AI · Vertex AI GenAI Eval Service (judge)"
    assert started["request"]["metrics"] == ["GROUNDEDNESS", "QUESTION_ANSWERING_QUALITY", "FULFILLMENT"]
    assert completed["payload"]["judge_provider"] == PROVIDER_VERTEX_GENAI_EVAL
    assert completed["payload"]["groundedness"] == 0.88

    # Direct Judge.score(system_prompt, user_prompt) protocol call also works cleanly
    with (
        patch("app.evaluation.vertex_eval.get_access_token", lambda s: "tok"),
        patch("app.evaluation.vertex_eval.httpx.post", side_effect=router),
    ):
        direct_outcome = evaluate_engine_result(
            "What is CLIN-0004-AB?", _sample_engine_result(), "Ref $245/hr", settings, judge
        )
    assert direct_outcome.reference_answer_used is True
    assert direct_outcome.metrics.groundedness == 0.88
