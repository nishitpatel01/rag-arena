"""query_runs provenance (spec §5/§6.4): one row per live/record engine run; write failures are visible, never fatal."""

from __future__ import annotations

import json
from typing import Any

from starlette.testclient import TestClient

from app.core.config import ArenaSettings
from app.ingestion.jobs import unavailable_backends
from app.ingestion.live_backends import LiveQueryRunWriter, live_backends
from app.main import create_app
from fakes import fake_clients, live_settings

_QID = "q_exact_clin_lookup"
_QUERY = "What is the base hourly rate for CLIN-0004-AB?"


class _CapturingWriter:
    def __init__(self, fail: bool = False) -> None:
        self.rows: list[dict[str, Any]] = []
        self._fail = fail

    def write_query_run(self, row: dict[str, Any]) -> None:
        if self._fail:
            raise RuntimeError("403 Access Denied: Table query_runs: Permission bigquery.tables.updateData denied")
        self.rows.append(row)


def _events(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


def _battle(settings: ArenaSettings, writer: Any) -> list[dict[str, Any]]:
    app = create_app(settings=settings, query_run_writer=writer)
    with fake_clients(), TestClient(app) as client:
        resp = client.post("/api/v1/battle/stream", json={"query": _QUERY, "question_id": _QID, "engines": ["level1_naive"]})
        assert resp.status_code == 200
        return _events(resp.text)


def test_live_run_writes_one_query_runs_row_with_full_provenance() -> None:
    writer = _CapturingWriter()
    events = _battle(live_settings(), writer)
    assert events[-1]["event"] == "battle_complete" and events[-1]["engines_completed"] == ["level1_naive"]
    assert not [e for e in events if e["event"] == "stream_warning"]
    assert len(writer.rows) == 1
    row = writer.rows[0]
    answer = next(e for e in events if e["event"] == "answer_complete")
    evaluation = next(e for e in events if e["event"] == "eval_complete")
    assert row["run_id"] == events[0]["run_id"] and row["engine"] == "level1_naive" and row["question_id"] == _QID
    assert row["query"] == _QUERY and row["answer"] == answer["answer"] and row["demo_mode"] == "live"
    assert row["latency_ms"] == answer["latency_ms"] and row["cost_usd"] == answer["cost_usd"]
    assert row["groundedness"] == evaluation["metrics"]["groundedness"]
    assert row["qa_relevance"] == evaluation["metrics"]["qa_relevance"]
    assert row["context_precision"] == evaluation["metrics"]["context_precision"]
    assert [s["stage"] for s in row["stage_log"]] == ["query_prep", "embed_query", "retrieve", "synthesize", "evaluate"]
    assert all(isinstance(s["duration_ms"], int) for s in row["stage_log"])
    assert len(row["citations"]) == len(answer["citations"]) >= 1 and row["citations"][0]["chunk_id"]
    assert isinstance(row["git_sha"], str) and row["git_sha"]
    assert row["started_at"].endswith("Z")


def test_query_runs_write_failure_emits_visible_warning_but_keeps_the_answer() -> None:
    events = _battle(live_settings(), _CapturingWriter(fail=True))
    kinds = [e["event"] for e in events]
    assert "answer_complete" in kinds and "eval_complete" in kinds
    warnings = [e for e in events if e["event"] == "stream_warning"]
    assert len(warnings) == 1 and "query_runs" in warnings[0]["message"] and "Access Denied" in warnings[0]["message"]
    assert warnings[0]["engine"] == "level1_naive"
    assert events[-1]["engines_completed"] == ["level1_naive"] and "engine_error" not in kinds


def test_replay_runs_never_write_query_runs() -> None:
    writer = _CapturingWriter()
    settings = ArenaSettings(demo_mode="replay", enabled_levels=("level1_naive",), allow_dev_fixtures=True, replay_speed=50.0)
    app = create_app(settings=settings, query_run_writer=writer)
    with TestClient(app) as client:
        resp = client.post("/api/v1/battle/stream", json={"query": "x", "question_id": "dev_q01", "engines": ["level1_naive"]})
    assert _events(resp.text)[-1]["event"] == "battle_complete" and writer.rows == []


def test_create_app_wires_live_backends_and_lazy_writer_only_in_live_modes() -> None:
    live = create_app(settings=live_settings(bq_table="document_chunks_v3"))
    assert live.state.ingest_runner.backend_factory is live_backends
    assert isinstance(live.state.query_run_writer, LiveQueryRunWriter) and live.state.query_run_writer.loaded is False
    replay = create_app(settings=ArenaSettings(demo_mode="replay"))
    assert replay.state.ingest_runner.backend_factory is unavailable_backends
    assert replay.state.query_run_writer is None


def test_read_git_sha_supports_reftable_repositories_via_bounded_git_call() -> None:
    import subprocess
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.recording import recorder

    root = Path(tempfile.mkdtemp(prefix="rag_arena_reftable_"))
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/.invalid\n", encoding="utf-8")
    (root / ".git" / "refs").write_text("", encoding="utf-8")  # reftable layout: refs/ is not a directory
    calls: list[dict[str, Any]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        calls.append({"cmd": cmd, **kwargs})
        return SimpleNamespace(stdout="502d72336dd8081ec3ab311918237111d671759f\n", returncode=0)

    with patch.object(subprocess, "run", _fake_run):
        assert recorder.read_git_sha(root) == "502d72336dd8081ec3ab311918237111d671759f"
    assert calls[0]["cmd"][-2:] == ["rev-parse", "HEAD"] and calls[0]["timeout"] <= 10 and calls[0]["check"] is True
    assert recorder.read_git_sha(Path(tempfile.mkdtemp())) == "unknown"  # not a git checkout
