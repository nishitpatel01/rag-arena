"""v3 SSE protocol (spec §6.3) through the real app in replay mode, plus REST endpoints."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from app.api.routes import _stream_battle_events
from app.core.config import ArenaSettings
from app.core.models import EngineResult
from app.engines import get_engine_registry
from app.engines.base import BaseRAGEngine
from app.main import create_app
from app.ingestion import session_store

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SAMPLE_PDF = _PROJECT_ROOT / "data" / "sample_docs" / "procurement_corpus" / "solicitation_01_cloud_infrastructure" / "01_RFP_Base_Solicitation.pdf"
_REPLAY_SETTINGS = ArenaSettings(demo_mode="replay", enabled_levels=("level1_naive",), allow_dev_fixtures=True, replay_speed=50.0)
_V3_ENGINE_STAGE_SEQUENCE = [
    ("stage_started", "query_prep"), ("stage_completed", "query_prep"),
    ("stage_started", "embed_query"), ("stage_completed", "embed_query"),
    ("stage_started", "retrieve"), ("stage_completed", "retrieve"),
    ("stage_started", "synthesize"), ("stage_completed", "synthesize"),
]


def _parse_sse_events(raw_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in raw_text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:"):].strip()))
    return events


def _client(settings: ArenaSettings = _REPLAY_SETTINGS) -> TestClient:
    return TestClient(create_app(settings=settings))


def test_health_levels_and_benchmarks_endpoints() -> None:
    client = _client()
    health = client.get("/api/v1/health").json()
    assert health["status"] == "ok" and health["demo_mode"] == "replay"
    assert {"project_id", "gcs_bucket", "enabled_levels", "demo_mode", "app_version"} <= set(health)
    assert health["enabled_levels"] == ["level1_naive"]
    bench = client.get("/api/v1/benchmarks").json()
    assert len(bench["questions"]) >= 8 and {"question_id", "question", "failure_mode_category"} <= set(bench["questions"][0])
    golden = json.loads((_PROJECT_ROOT / "data" / "golden_benchmarks" / "procurement_benchmark.json").read_text(encoding="utf-8"))
    assert bench["question_count"] == len(golden["questions"]) == len(bench["questions"])  # file is the source of truth (8 today, not 12)
    levels = client.get("/api/v1/levels").json()
    assert [lvl["enabled"] for lvl in levels["levels"]] == [True, False, False, False, False]


def test_replay_battle_stream_forwards_recorded_stage_events_in_v3_envelope() -> None:
    client = _client()
    resp = client.post(
        "/api/v1/battle/stream",
        json={"query": "What is the hourly labor rate for CLIN-0004-AB?", "question_id": "dev_q01", "engines": ["level1_naive"]},
    )
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse_events(resp.text)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run_started" and kinds[-1] == "battle_complete"
    run_id = events[0]["run_id"]
    assert events[0]["engines"] == ["level1_naive"] and events[0]["demo_mode"] == "replay" and events[0]["question_id"] == "dev_q01"
    assert all(e["run_id"] == run_id for e in events) and all(e["ts"].endswith("Z") for e in events)
    stage_events = [(e["event"], e["stage"]) for e in events if e["event"].startswith("stage_")]
    assert stage_events == _V3_ENGINE_STAGE_SEQUENCE + [("stage_started", "evaluate"), ("stage_completed", "evaluate")]
    for e in events:
        if e["event"].startswith("stage_"):
            assert e["engine"] == "level1_naive" and e["replayed"] is True
    completed = {e["stage"]: e for e in events if e["event"] == "stage_completed"}
    assert isinstance(completed["retrieve"]["duration_ms"], int) and completed["retrieve"]["payload"]["rows_returned"] == 2
    assert completed["embed_query"]["payload"]["dimensions"] == 3072
    started = {e["stage"]: e for e in events if e["event"] == "stage_started"}
    assert "sql" in started["retrieve"]["request"] and started["retrieve"]["service"] == "BigQuery"
    answer = next(e for e in events if e["event"] == "answer_complete")
    assert {"answer", "citations", "latency_ms", "cost_usd", "usage", "flags"} <= set(answer)
    assert answer["usage"]["prompt_tokens"] == 1842 and answer["flags"]["reranker_used"] is False
    assert len(answer["citations"]) == 2 and "bbox" in answer["citations"][0]
    evaluated = next(e for e in events if e["event"] == "eval_complete")
    assert set(evaluated["metrics"]) == {"groundedness", "qa_relevance", "context_precision"}
    assert evaluated["judge_model"] == "claude-opus-5-5" and evaluated["reference_answer_used"] is False
    assert isinstance(evaluated["duration_ms"], int)
    assert kinds.index("answer_complete") < kinds.index("eval_complete") < kinds.index("battle_complete")
    assert events[-1] == {"event": "battle_complete", "run_id": run_id, "engines_completed": ["level1_naive"], "engines_failed": [], "ts": events[-1]["ts"]}
    assert "pipeline_stage" not in kinds and "battlefield_summary" not in kinds


def test_replay_adhoc_question_yields_engine_error_not_an_improvised_answer() -> None:
    client = _client()
    events = _parse_sse_events(client.post("/api/v1/battle/stream", json={"query": "Who signed the MSA?", "question_id": None}).text)
    kinds = [e["event"] for e in events]
    assert kinds == ["run_started", "engine_error", "battle_complete"]
    err = events[1]
    assert err["engine"] == "level1_naive" and err["error_type"] == "ReplayUnavailableError"
    assert err["error"].startswith("No recording for this question in replay mode") and err["failed_stage"] is None
    assert events[-1]["engines_completed"] == [] and events[-1]["engines_failed"] == ["level1_naive"]
    strict = _client(ArenaSettings(demo_mode="replay", enabled_levels=("level1_naive",), allow_dev_fixtures=False))
    strict_events = _parse_sse_events(strict.post("/api/v1/battle/stream", json={"query": "q", "question_id": "dev_q01"}).text)
    assert strict_events[1]["event"] == "engine_error" and "ALLOW_DEV_FIXTURES" in strict_events[1]["error"]


def test_battle_stream_rejects_invalid_payloads_and_disabled_engines() -> None:
    client = _client()
    assert client.post("/api/v1/battle/stream", json={"query": "   "}).status_code == 400
    assert client.post("/api/v1/battle/stream", json={"query": "q", "engines": []}).status_code == 400
    resp = client.post("/api/v1/battle/stream", json={"query": "q", "engines": ["level2_hybrid"]})
    assert resp.status_code == 400 and "disabled" in resp.json()["error"]
    assert client.post("/api/v1/battle/stream", content=b"{not json", headers={"content-type": "application/json"}).status_code == 400


def test_stream_isolates_a_failing_engine_and_reports_failed_stage() -> None:
    class _FailingEngine(BaseRAGEngine):
        engine_id = "level2_hybrid"
        engine_label = "Failing Engine"
        tier_profile = "hybrid"
        stages = ("query_prep", "embed_query", "retrieve", "rerank", "synthesize", "evaluate")

        async def run_query(self, query, *, question_id, solicitation_filter, emitter) -> EngineResult:  # noqa: ANN001
            async with self.stage(emitter, "query_prep", label="prep", service="local", request={}):
                pass
            async with self.stage(emitter, "retrieve", label="BQ", service="BigQuery", request={"sql": "SELECT 1"}):
                raise RuntimeError("Simulated BigQuery 403")

    settings = ArenaSettings(demo_mode="replay", enabled_levels=("level1_naive", "level2_hybrid"), allow_dev_fixtures=True, replay_speed=50.0)
    registry = get_engine_registry(settings)
    registry["level2_hybrid"] = _FailingEngine(settings=settings)

    async def collect() -> list[dict[str, Any]]:
        frames: list[str] = []
        async for frame in _stream_battle_events(
            query="What is the amended hourly rate for CLIN-0004-AB?", question_id="dev_q01",
            selected_engines=["level1_naive", "level2_hybrid"], solicitation_filter=None, registry=registry, settings=settings,
        ):
            frames.append(frame)
        return _parse_sse_events("".join(frames))

    events = asyncio.run(collect())
    errs = [e for e in events if e["event"] == "engine_error"]
    assert len(errs) == 1 and errs[0]["engine"] == "level2_hybrid"
    assert errs[0]["error"] == "Simulated BigQuery 403" and errs[0]["error_type"] == "RuntimeError" and errs[0]["failed_stage"] == "retrieve"
    failed_stage_events = [e for e in events if e["event"] == "stage_failed"]
    assert len(failed_stage_events) == 1 and failed_stage_events[0]["stage"] == "retrieve" and "duration_ms" in failed_stage_events[0]
    assert [e["engine"] for e in events if e["event"] == "eval_complete"] == ["level1_naive"]
    assert events[-1]["engines_completed"] == ["level1_naive"] and events[-1]["engines_failed"] == ["level2_hybrid"]


def test_stream_emits_stream_warning_when_max_sse_frames_is_reached() -> None:
    settings = ArenaSettings(demo_mode="replay", enabled_levels=("level1_naive",), allow_dev_fixtures=True, replay_speed=50.0, max_sse_frames=10)
    events = _parse_sse_events(_client(settings).post("/api/v1/battle/stream", json={"query": "q", "question_id": "dev_q01"}).text)
    kinds = [e["event"] for e in events]
    assert len(events) <= 10 and "stream_warning" in kinds and kinds[-1] == "battle_complete"
    assert "MAX_SSE_FRAMES" in next(e for e in events if e["event"] == "stream_warning")["message"]


def test_leaderboard_endpoint_aggregates_recordings_with_provenance() -> None:
    body = _client().get("/api/v1/leaderboard").json()
    assert body["source"] == "recordings"
    l1 = next(row for row in body["engines"] if row["engine"] == "level1_naive")
    assert l1["runs"] >= 1 and l1["provenance"][0]["synthetic_dev_fixture"] is True


def test_upload_endpoint_parses_without_claiming_indexing() -> None:
    client = _client()
    payload = {"filename": "upload_test.pdf", "content_base64": base64.b64encode(_SAMPLE_PDF.read_bytes()).decode("ascii")}
    resp = client.post("/api/v1/ingest/upload", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "parsed" and body["indexed"] is False and body["solicitation_id"].startswith("upload_")
    assert body["chunks_parsed"]["naive"] > 0
    bad = client.post("/api/v1/ingest/upload", json={"filename": "x.pdf", "content_base64": base64.b64encode(b"not a pdf").decode("ascii")})
    assert bad.status_code == 400
    session_store.clear_uploaded_sessions()
