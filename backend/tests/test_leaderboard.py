"""Leaderboard (Phase 6): both sources share one shape; per-pair grouping; query_runs read is parameterized and
redacted; replay rejects query_runs; NULL-model rows isolated; hand-typed precomputed metrics are gone.
BigQuery is always mocked (offline)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.api import query_runs_reader as qr
from app.core.config import ArenaSettings
from app.core.models import EvalMetrics
from app.engines import engine_labels
from app.evaluation.leaderboard import (
    UNKNOWN_MODEL,
    LeaderboardFilters,
    RunSample,
    aggregate_leaderboard,
    leaderboard_from_recordings,
    parse_filters,
)
from app.evaluation.scoring import battle_score
from app.main import create_app
from app.recording.store import RecordingStore

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RECORDINGS = _PROJECT_ROOT / "data" / "recordings"
_PROJECT = "acme-proj-1"
_ROW_KEYS = {
    "engine", "level_label", "runs", "avg_battle_score", "avg_efficiency", "avg_groundedness", "avg_qa_relevance",
    "avg_context_precision", "avg_latency_ms", "avg_cost_usd", "questions_covered", "failures", "first_run_at",
    "last_run_at", "provenance",
}
_TOP_KEYS = {"source", "generated_at", "filters", "notes", "stats", "groups"}
_GROUP_KEYS = {"synthesis_model", "judge_model", "pair_known", "note", "rows"}


def _live() -> ArenaSettings:
    return ArenaSettings(demo_mode="live", gcp_project_id=_PROJECT, bq_dataset="rag_arena", enabled_levels=("level1_naive",))


def _bq_row(engine: str, synth: str | None, judge: str | None, g: float | None = 0.9, qa: float | None = 0.8,
            cp: float | None = 0.7, *, latency: int = 30_000, cost: float = 0.05, qid: str = "q1",
            mode: str = "live", sha: str = "abc123", day: int = 22) -> dict[str, Any]:
    return {
        "engine": engine, "question_id": qid, "synthesis_model": synth, "judge_model": judge, "latency_ms": latency,
        "cost_usd": cost, "groundedness": g, "qa_relevance": qa, "context_precision": cp,
        "started_at": datetime(2026, 10, day, 12, 0, tzinfo=timezone.utc), "demo_mode": mode, "git_sha": sha,
        "rows_matching": 0,
    }


def _fake_client(rows: list[dict[str, Any]], *, error: Exception | None = None) -> MagicMock:
    for r in rows:
        r["rows_matching"] = len(rows)
    job = MagicMock()
    job.total_bytes_processed = 4096
    if error is not None:
        job.result.side_effect = error
    else:
        job.result.return_value = rows
    client = MagicMock()
    client.query.return_value = job
    return client


def _rows(board: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for g in board["groups"] for row in g["rows"]]


def test_both_sources_return_the_same_shape() -> None:
    recordings = leaderboard_from_recordings(RecordingStore(_RECORDINGS), LeaderboardFilters(), engine_labels())
    client = _fake_client([_bq_row("level1_naive", "gemini-4-argon", "claude-opus-5-5")])
    live = qr.leaderboard_from_query_runs(_live(), LeaderboardFilters(), engine_labels(), client=client)
    for board in (recordings, live):
        assert set(board) == _TOP_KEYS
        assert board["groups"] and all(set(g) == _GROUP_KEYS for g in board["groups"])
        assert all(set(r) == _ROW_KEYS for r in _rows(board))
    assert recordings["source"] == "recordings" and live["source"] == "query_runs"
    rec_prov = _rows(recordings)[0]["provenance"]
    assert rec_prov["kind"] == "recording" and {"files", "recorded_at", "git_shas"} <= set(rec_prov)
    live_prov = _rows(live)[0]["provenance"]
    assert live_prov == {"kind": "query_runs", "table": "rag_arena.query_runs", "demo_modes": {"live": 1}, "git_shas": ["abc123"]}
    assert _PROJECT not in str(live)
    assert live["stats"] == {"rows_read": 1, "rows_matching": 1, "max_rows": qr.QUERY_RUNS_MAX_ROWS, "bytes_processed": 4096}
    assert _rows(live)[0]["level_label"] == engine_labels()["level1_naive"]


def test_pairs_are_never_averaged_and_rows_sort_by_battle_score() -> None:
    rows = [
        _bq_row("level1_naive", "gemini-4-argon", "claude-opus-5-5", 0.5, 0.5, 0.5),
        _bq_row("level2_hybrid", "gemini-4-argon", "claude-opus-5-5", 1.0, 1.0, 1.0),
        _bq_row("level1_naive", "gemini-4-argon", "gemini-3.8-flash", 0.1, 0.1, 0.1),  # other judge → other group
    ]
    board = qr.leaderboard_from_query_runs(_live(), LeaderboardFilters(), {}, client=_fake_client(rows))
    pairs = [(g["synthesis_model"], g["judge_model"], [r["engine"] for r in g["rows"]]) for g in board["groups"]]
    assert pairs == [("gemini-4-argon", "claude-opus-5-5", ["level2_hybrid", "level1_naive"]),
                     ("gemini-4-argon", "gemini-3.8-flash", ["level1_naive"])]
    other = board["groups"][1]["rows"][0]
    assert other["runs"] == 1 and other["avg_groundedness"] == 0.1  # not blended with the 0.5 run of the other judge


def test_battle_score_per_run_matches_scoring_and_is_averaged_per_row() -> None:
    rows = [
        _bq_row("level1_naive", "s", "j", 0.9, 0.8, 0.7, latency=20_000, cost=0.04, qid="q1", day=21),
        _bq_row("level1_naive", "s", "j", 0.6, 0.5, 0.4, latency=50_000, cost=0.12, qid="q2", day=23),
        _bq_row("level1_naive", "s", "j", None, None, None, qid="q3", day=22),  # failure: no metrics
    ]
    row = _rows(qr.leaderboard_from_query_runs(_live(), LeaderboardFilters(), {}, client=_fake_client(rows)))[0]
    expected = (battle_score(EvalMetrics(0.9, 0.8, 0.7), 20_000, 0.04).battle_score
                + battle_score(EvalMetrics(0.6, 0.5, 0.4), 50_000, 0.12).battle_score) / 2
    assert row["avg_battle_score"] == round(expected, 1)
    assert (row["runs"], row["failures"], row["questions_covered"]) == (2, 1, 2)
    assert row["avg_latency_ms"] == 35_000.0 and row["avg_cost_usd"] == 0.08
    assert row["first_run_at"].startswith("2026-10-21") and row["last_run_at"].startswith("2026-10-23")


def test_null_model_rows_go_to_an_unknown_group_never_into_a_real_pair() -> None:
    rows = [_bq_row("level1_naive", "s", "j", 0.9, 0.9, 0.9), _bq_row("level1_naive", None, None, 0.1, 0.1, 0.1),
            _bq_row("level1_naive", "s", None, 0.2, 0.2, 0.2)]
    board = qr.leaderboard_from_query_runs(_live(), LeaderboardFilters(), {}, client=_fake_client(rows))
    known, unknown = board["groups"]
    assert (known["synthesis_model"], known["pair_known"], known["rows"][0]["runs"]) == ("s", True, 1)
    assert known["rows"][0]["avg_groundedness"] == 0.9 and known["note"] is None
    assert (unknown["synthesis_model"], unknown["judge_model"], unknown["pair_known"]) == (UNKNOWN_MODEL, UNKNOWN_MODEL, False)
    assert unknown["rows"][0]["runs"] == 2 and "not comparable" in unknown["note"]


def test_filters_are_query_parameters_never_interpolated() -> None:
    client = _fake_client([])
    flt = parse_filters("q_vpc_failover_diagram", "level2_hybrid")
    board = qr.leaderboard_from_query_runs(_live(), flt, {}, client=client)
    sql = client.query.call_args.args[0]
    config = client.query.call_args.kwargs["job_config"]
    assert "q_vpc_failover_diagram" not in sql and "level2_hybrid" not in sql
    assert "@question_id" in sql and "@engine" in sql and f"LIMIT {qr.QUERY_RUNS_MAX_ROWS}" in sql
    assert f"`{_PROJECT}.rag_arena.query_runs`" in sql
    params = {p.name: (p.type_, p.value) for p in config.query_parameters}
    assert params == {
        "question_id": ("STRING", "q_vpc_failover_diagram"),
        "engine": ("STRING", "level2_hybrid"),
        "scope": ("STRING", "golden"),
    }
    assert config.maximum_bytes_billed == qr.QUERY_RUNS_MAX_BYTES_BILLED
    assert client.query.return_value.result.call_args.kwargs["timeout"] == qr.QUERY_RUNS_TIMEOUT_SECONDS
    assert board["filters"] == {
        "question_id": "q_vpc_failover_diagram",
        "engine": "level2_hybrid",
        "scope": "golden",
    } and board["groups"] == []
    for bad in (("x' OR 1=1 --", None), (None, "Level1; DROP")):
        try:
            parse_filters(*bad)
        except ValueError:
            continue
        raise AssertionError(f"filter {bad} should be rejected")
    try:
        qr.build_query_runs_sql("proj`; DROP", "rag_arena")
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe project identifier should be rejected")


def test_endpoint_defaults_and_replay_rejects_query_runs() -> None:
    replay = TestClient(create_app(settings=ArenaSettings(demo_mode="replay", enabled_levels=("level1_naive",))))
    assert replay.get("/api/v1/leaderboard").json()["source"] == "recordings"
    rejected = replay.get("/api/v1/leaderboard?source=query_runs")
    assert rejected.status_code == 400 and "DEMO_MODE=live or record" in rejected.json()["error"]
    assert replay.get("/api/v1/leaderboard?source=nope").status_code == 400
    assert replay.get("/api/v1/leaderboard?engine=Bad%20Engine").status_code == 400
    filtered = replay.get("/api/v1/leaderboard?engine=level2_hybrid").json()
    assert {r["engine"] for r in _rows(filtered)} <= {"level2_hybrid"} and filtered["filters"]["engine"] == "level2_hybrid"
    live = TestClient(create_app(settings=_live()))
    with patch.object(qr, "get_bigquery_client", return_value=_fake_client([_bq_row("level1_naive", "s", "j")])):
        body = live.get("/api/v1/leaderboard").json()
    assert body["source"] == "query_runs" and _rows(body)[0]["runs"] == 1
    with patch.object(qr, "get_bigquery_client", side_effect=AssertionError("recordings must not touch BigQuery")):
        assert live.get("/api/v1/leaderboard?source=recordings").json()["source"] == "recordings"


def test_bigquery_errors_are_502_verbatim_and_redacted() -> None:
    fake_pnum = "9" * 12
    err = RuntimeError(f"403 Access Denied: Table {_PROJECT}:rag_arena.query_runs (projects/{fake_pnum}/jobs/x)")
    live = TestClient(create_app(settings=_live()))
    with patch.object(qr, "get_bigquery_client", return_value=_fake_client([], error=err)):
        resp = live.get("/api/v1/leaderboard?source=query_runs")
    assert resp.status_code == 502
    message = resp.json()["error"]
    assert message.startswith("BigQuery query_runs read failed: RuntimeError: 403 Access Denied: Table ${GCP_PROJECT_ID}:")
    assert _PROJECT not in message and fake_pnum not in message and "projects/${GCP_PROJECT_NUMBER}" in message


def test_recordings_failures_and_unknown_pairs_use_the_same_aggregator() -> None:
    samples = [
        RunSample("level1_naive", "s", "j", "q1", "2026-10-21T00:00:00Z", "a1", "record", 30_000, 0.05, EvalMetrics(1, 1, 1)),
        RunSample("level1_naive", "s", "j", "q2", "2026-10-22T00:00:00Z", "a2", "record", None, None, None),
    ]
    board = aggregate_leaderboard(samples, source="recordings", filters=LeaderboardFilters(), level_labels={})
    row = board["groups"][0]["rows"][0]
    assert (row["runs"], row["failures"], row["level_label"]) == (1, 1, "level1_naive")
    assert row["provenance"] == {"kind": "recording", "files": 2, "git_shas": ["a1", "a2"],
                                 "recorded_at": {"first": "2026-10-21T00:00:00Z", "last": "2026-10-22T00:00:00Z"}}
    empty_root = Path(tempfile.mkdtemp(prefix="rag_arena_lb_empty_"))
    assert leaderboard_from_recordings(RecordingStore(empty_root))["groups"] == []


def test_hand_typed_precomputed_runs_are_gone() -> None:
    assert not (_PROJECT_ROOT / "data" / "golden_benchmarks" / "precomputed_runs.json").exists()
    for base in (_PROJECT_ROOT / "backend" / "app", _PROJECT_ROOT / "frontend" / "src"):
        for path in base.rglob("*"):
            if path.suffix in {".py", ".ts", ".tsx"}:
                assert "precomputed_runs" not in path.read_text(encoding="utf-8"), path


def test_leaderboard_scope_separates_golden_and_uploads_without_averaging() -> None:
    assert parse_filters(None, None).scope == "golden"
    assert parse_filters(None, None, "uploads").scope == "uploads"
    try:
        parse_filters(None, None, "mixed")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid scope should raise ValueError")

    sql = qr.build_query_runs_sql(_PROJECT, "rag_arena")
    assert "@scope" in sql
    assert "STARTS_WITH(solicitation_id, 'upl_')" in sql

    live = TestClient(create_app(settings=_live()))
    assert live.get("/api/v1/leaderboard?scope=invalid").status_code == 400

    # Recordings cover the fixed golden benchmark corpus only; scope=uploads returns empty groups with an explicit note.
    rec_uploads = live.get("/api/v1/leaderboard?source=recordings&scope=uploads").json()
    assert rec_uploads["groups"] == [] and rec_uploads["filters"]["scope"] == "uploads"
    assert any("golden" in n.lower() for n in rec_uploads["notes"])

    # Live query_runs passes @scope='uploads' parameter to BigQuery.
    fake_bq = _fake_client([_bq_row("level1_naive", "gemini-4-argon", "claude-opus-5-5", qid=None)])
    with patch.object(qr, "get_bigquery_client", return_value=fake_bq):
        upl_board = live.get("/api/v1/leaderboard?source=query_runs&scope=uploads").json()
    params = {p.name: p.value for p in fake_bq.query.call_args.kwargs["job_config"].query_parameters}
    assert params["scope"] == "uploads"
    assert upl_board["filters"]["scope"] == "uploads" and len(upl_board["groups"]) == 1

