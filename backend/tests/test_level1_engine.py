"""Level 1 engine (live, mocked SDK): real stage emission, measured usage/cost, fail-loud paths."""

from __future__ import annotations

import pytest

from app.core.models import EngineFlags, EngineResult, TokenUsage
from app.core.pricing import cost_from_usage
from app.core.telemetry import RecordingStageEmitter
from app.engines.level1_naive import Level1NaiveEngine, build_vector_search_sql
from fakes import EMBED_DIMS, FakeBigQueryClient, FakeGenaiClient, fake_clients, live_settings, run

_QUERY = "What is the hourly labor rate for CLIN-0004-AB?"


def _run_level1(genai: FakeGenaiClient | None = None, bq: FakeBigQueryClient | None = None):
    settings = live_settings()
    emitter = RecordingStageEmitter(run_id="run_l1", engine="level1_naive")
    with fake_clients(genai, bq) as (genai_client, bq_client):
        engine = Level1NaiveEngine(settings=settings)
        result = run(engine.run_query(_QUERY, question_id="q_test", solicitation_filter=None, emitter=emitter))
    return result, emitter, genai_client, bq_client


def test_level1_emits_canonical_stages_in_order_with_measured_payloads() -> None:
    result, emitter, genai_client, bq_client = _run_level1()
    kinds = [(e["event"], e.get("stage")) for e in emitter.events]
    assert kinds == [
        ("stage_started", "query_prep"), ("stage_completed", "query_prep"),
        ("stage_started", "embed_query"), ("stage_completed", "embed_query"),
        ("stage_started", "retrieve"), ("stage_completed", "retrieve"),
        ("stage_started", "synthesize"), ("stage_completed", "synthesize"),
    ]
    completed = {e["stage"]: e for e in emitter.events if e["event"] == "stage_completed"}
    assert completed["query_prep"]["payload"] == {"original_query": _QUERY, "prepared_query": _QUERY, "expansions": []}
    embed = completed["embed_query"]["payload"]
    assert embed == {"model": "gemini-embedding-2", "dimensions": EMBED_DIMS, "token_count": 14}
    retrieve = completed["retrieve"]["payload"]
    assert retrieve["rows_returned"] == 2 and retrieve["top_k"] == 5
    assert retrieve["scores"] == [0.8123, 0.7791]
    assert retrieve["bytes_processed"] == 12_582_912 and retrieve["job_id"] == "bqjob_fake_0001"
    assert "VECTOR_SEARCH" in retrieve["sql"] and "@query_embedding" in retrieve["sql"]
    assert retrieve["params"]["query_embedding"]["dimensions"] == EMBED_DIMS
    synth = completed["synthesize"]["payload"]
    assert synth["model"] == "gemini-4-argon" and synth["thinking_level"] == "HIGH"
    assert synth["prompt_tokens"] == 1842 and synth["output_tokens"] == 96 and synth["thinking_tokens"] == 230
    assert synth["answer"] == result.answer
    started = {e["stage"]: e for e in emitter.events if e["event"] == "stage_started"}
    assert "prompt" in started["synthesize"]["request"] and "sql" in started["retrieve"]["request"]
    assert all(isinstance(e["duration_ms"], int) and e["duration_ms"] >= 0 for e in completed.values())
    assert [c[0] for c in genai_client.calls] == ["embed_content", "generate_content"]
    assert len(bq_client.queries) == 1


def test_level1_result_carries_usage_flags_cost_and_stage_log_without_eval_metrics() -> None:
    result, emitter, _, _ = _run_level1()
    assert isinstance(result, EngineResult)
    assert not hasattr(result, "eval_metrics")
    assert result.usage == TokenUsage(
        prompt_tokens=1842, output_tokens=96, embedding_tokens=14, thinking_tokens=230,
        model="gemini-4-argon", embedding_model="gemini-embedding-2",
    )
    assert result.cost_usd == cost_from_usage(result.usage, "gemini-4-argon") > 0
    assert result.flags == EngineFlags(reranker_used=False, adk_runner_used=False, layout_source="pymupdf")
    assert [s.stage for s in result.stage_log] == ["query_prep", "embed_query", "retrieve", "synthesize"]
    assert result.stage_log == emitter.records
    assert len(result.citations) == 2 and result.citations[0].chunk_id == result.retrieved_chunks[0].chunk_id
    assert result.retrieved_chunks[0].score == 0.8123


def test_level1_bigquery_failure_emits_stage_failed_and_raises() -> None:
    class _BrokenBQ(FakeBigQueryClient):
        def query(self, sql: str, job_config=None):  # noqa: ANN001
            raise RuntimeError("403 Access Denied: BigQuery BigQuery: Permission denied")

    with pytest.raises(RuntimeError, match="Access Denied"):
        _run_level1(bq=_BrokenBQ())
    emitter = RecordingStageEmitter(run_id="run_l1b", engine="level1_naive")
    with fake_clients(None, _BrokenBQ()):
        engine = Level1NaiveEngine(settings=live_settings())
        with pytest.raises(RuntimeError):
            run(engine.run_query(_QUERY, question_id=None, solicitation_filter=None, emitter=emitter))
    failed = [e for e in emitter.events if e["event"] == "stage_failed"]
    assert len(failed) == 1 and failed[0]["stage"] == "retrieve" and failed[0]["error_type"] == "RuntimeError"
    assert emitter.records[-1].status == "failed"


def test_level1_refuses_replay_mode_and_upload_filters_and_missing_usage_metadata() -> None:
    from app.core.config import ArenaSettings

    emitter = RecordingStageEmitter(run_id="run_l1c", engine="level1_naive")
    with pytest.raises(RuntimeError, match="replay"):
        run(Level1NaiveEngine(settings=ArenaSettings(demo_mode="replay")).run_query(_QUERY, question_id=None, solicitation_filter=None, emitter=emitter))
    with fake_clients():
        with pytest.raises(ValueError, match="Uploaded sessions"):
            run(Level1NaiveEngine(settings=live_settings()).run_query(_QUERY, question_id=None, solicitation_filter="upload_abc", emitter=emitter))

    class _NoUsage(FakeGenaiClient):
        def _generate(self, **kwargs):  # noqa: ANN003
            from types import SimpleNamespace

            return SimpleNamespace(text="answer without usage")

    with pytest.raises(ValueError, match="usage_metadata"):
        _run_level1(genai=_NoUsage())


def test_build_vector_search_sql_is_parameterized_and_prefiltered() -> None:
    sql = build_vector_search_sql("proj", "rag_arena", "document_chunks", top_k=5, include_solicitation_filter=True)
    assert "tier_profile = 'naive'" in sql and "@solicitation_id" in sql and "distance_type => 'COSINE'" in sql
    with pytest.raises(ValueError):
        build_vector_search_sql("proj; DROP", "rag_arena")
    with pytest.raises(ValueError):
        build_vector_search_sql("proj", "rag_arena", top_k=0)
