"""Record → replay round trip (spec §6.4): recordings carry provenance; replay streams them with timing.

``test_generate_dev_fixture`` builds ``dev_q01`` from a mocked live run into a temp store and checks that the
committed ``data/recordings/_dev/level1_naive/dev_q01.json`` has the same shape (so it matches what
``DEMO_MODE=record`` writes) without rewriting it. Set ``REGENERATE_DEV_FIXTURES=1`` to deliberately refresh the
committed fixture. It is clearly labelled synthetic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time

import pytest

from app.core.config import ArenaSettings
from app.core.models import EngineResult, EvalOutcome
from app.core.telemetry import RecordingStageEmitter
from app.engines.level1_naive import Level1NaiveEngine
from app.engines.replay_engine import (
    ReplayRAGEngine,
    ReplayUnavailableError,
    replay_evaluation,
    split_engine_and_eval_events,
)
from app.evaluation.evaluator import run_evaluation
from app.recording.recorder import build_recording, read_git_sha, record_run
from app.recording.store import (
    RecordingInvalid,
    RecordingNotFound,
    RecordingRefused,
    RecordingStore,
    validate_recording_document,
)
from fakes import fake_clients, live_settings, run

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEV_FIXTURE = _PROJECT_ROOT / "data" / "recordings" / "_dev" / "level1_naive" / "dev_q01.json"
_DEV_QUERY = "What is the hourly labor rate for CLIN-0004-AB and did an amendment change it?"


def _mocked_live_run(question_id: str) -> tuple[EngineResult, EvalOutcome, RecordingStageEmitter, ArenaSettings]:
    settings = live_settings(demo_mode="record")
    emitter = RecordingStageEmitter(run_id="run_rec", engine="level1_naive")

    async def scenario():
        engine = Level1NaiveEngine(settings=settings)
        result = await engine.run_query(_DEV_QUERY, question_id=question_id, solicitation_filter=None, emitter=emitter)
        outcome = await run_evaluation(_DEV_QUERY, result, question_id=question_id, settings=settings, emitter=emitter)
        return result, outcome

    with fake_clients():
        result, outcome = run(scenario())
    return result, outcome, emitter, settings


def _shape(doc: dict) -> dict:
    """Run-independent structure of a recording (no timestamps, durations, run ids or git sha)."""
    volatile = {"recorded_at", "git_sha", "run_id"}
    return {
        "header": {k: v for k, v in doc["header"].items() if k not in volatile},
        "events": [(e["event"], e.get("stage")) for e in doc["events"]],
        "result_keys": sorted(doc["result"]),
        "answer": doc["result"]["answer"],
        "citations": [c["chunk_id"] for c in doc["result"]["citations"]],
        "evaluation": {k: v for k, v in doc["evaluation"].items() if k != "duration_ms"},
    }


def test_generate_dev_fixture_from_mocked_live_run() -> None:
    result, outcome, emitter, settings = _mocked_live_run("dev_q01")
    recording = build_recording(
        settings=settings, engine_id="level1_naive", question_id="dev_q01", query=_DEV_QUERY, run_id="run_rec",
        events=emitter.events, result=result, evaluation=outcome, synthetic_dev_fixture=True,
    )
    regenerate = os.environ.get("REGENERATE_DEV_FIXTURES", "").strip() == "1"
    root = DEV_FIXTURE.parents[2] if regenerate else Path(tempfile.mkdtemp(prefix="rag_arena_devfix_"))
    path = record_run(RecordingStore(root, allow_dev_fixtures=True), recording)
    assert path == root / "_dev" / "level1_naive" / "dev_q01.json" and path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["header"]["synthetic_dev_fixture"] is True
    for key in ("recorded_at", "project_id", "demo_mode", "model_ids", "git_sha", "app_version"):
        assert key in doc["header"]
    assert doc["header"]["demo_mode"] == "record" and doc["header"]["project_id"] == "${GCP_PROJECT_ID}"
    assert [e["event"] for e in doc["events"]][:2] == ["stage_started", "stage_completed"]
    assert doc["events"][-1]["event"] == "stage_completed" and doc["events"][-1]["stage"] == "evaluate"
    assert all(isinstance(e["t_rel_ms"], int) for e in doc["events"])
    assert "eval_metrics" not in doc["result"]
    assert doc["evaluation"]["judge_model"] == "gemini-4-argon"
    assert doc["evaluation"]["reference_answer_used"] is False
    committed = json.loads(DEV_FIXTURE.read_text(encoding="utf-8"))
    assert _shape(committed) == _shape(doc), "committed dev fixture is stale; rerun with REGENERATE_DEV_FIXTURES=1"


def test_recording_roundtrip_to_tmp_store_and_replay_with_timing() -> None:
    result, outcome, emitter, settings = _mocked_live_run("q_roundtrip")
    root = Path(tempfile.mkdtemp(prefix="rag_arena_rec_"))
    store = RecordingStore(root, allow_dev_fixtures=False)
    recording = build_recording(
        settings=settings, engine_id="level1_naive", question_id="q_roundtrip", query=_DEV_QUERY, run_id="run_rec",
        events=emitter.events, result=result, evaluation=outcome,
    )
    record_run(store, recording)
    loaded = store.load("level1_naive", "q_roundtrip")
    assert loaded.header["git_sha"] == read_git_sha()
    engine_events, eval_events = split_engine_and_eval_events(loaded.events)
    assert {e["stage"] for e in eval_events} == {"evaluate"} and len(engine_events) == 8

    replay_settings = ArenaSettings(demo_mode="replay", recordings_dir=str(root), replay_speed=50.0)
    replay_emitter = RecordingStageEmitter(run_id="run_replay", engine="level1_naive")
    engine = ReplayRAGEngine(
        engine_id="level1_naive", engine_label="Level 1: Naive Vector RAG", stages=Level1NaiveEngine.stages,
        tier_profile="naive", settings=replay_settings,
    )
    t0 = time.perf_counter()
    replayed = run(engine.run_query(_DEV_QUERY, question_id="q_roundtrip", solicitation_filter=None, emitter=replay_emitter))
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0
    assert replayed.answer == result.answer and replayed.cost_usd == result.cost_usd
    assert replayed.usage == result.usage and replayed.flags == result.flags
    frames = replay_emitter.events
    assert [(f["event"], f["stage"]) for f in frames] == [(e["event"], e["stage"]) for e in engine_events]
    assert all(f["replayed"] is True and f["run_id"] == "run_replay" for f in frames)
    recorded_durations = [e["duration_ms"] for e in engine_events if e["event"] == "stage_completed"]
    assert [f["duration_ms"] for f in frames if f["event"] == "stage_completed"] == recorded_durations
    assert [s.stage for s in replayed.stage_log] == ["query_prep", "embed_query", "retrieve", "synthesize"]

    eval_outcome = run(
        replay_evaluation(store, engine_id="level1_naive", question_id="q_roundtrip", emitter=replay_emitter, replay_speed=50.0)
    )
    assert eval_outcome.metrics == outcome.metrics and eval_outcome.judge_model == outcome.judge_model
    assert replay_emitter.events[-1]["stage"] == "evaluate"


def test_replay_refuses_unknown_questions_and_dev_fixtures_without_opt_in() -> None:
    root = DEV_FIXTURE.parents[2]
    strict = RecordingStore(root, allow_dev_fixtures=False)
    with pytest.raises(RecordingRefused):
        strict.load("level1_naive", "dev_q01")
    with pytest.raises(RecordingNotFound):
        strict.load("level1_naive", "q_does_not_exist")
    engine = ReplayRAGEngine(
        engine_id="level1_naive", engine_label="L1", stages=Level1NaiveEngine.stages, tier_profile="naive",
        settings=ArenaSettings(demo_mode="replay", recordings_dir=str(root)),
    )
    emitter = RecordingStageEmitter(run_id="r", engine="level1_naive")
    with pytest.raises(ReplayUnavailableError, match="No recording for this question in replay mode"):
        run(engine.run_query("adhoc question", question_id="q_missing", solicitation_filter=None, emitter=emitter))
    with pytest.raises(ReplayUnavailableError, match="question_id missing"):
        run(engine.run_query("adhoc question", question_id=None, solicitation_filter=None, emitter=emitter))
    assert emitter.events == ()
    permissive = RecordingStore(root, allow_dev_fixtures=True)
    assert permissive.load("level1_naive", "dev_q01").is_dev_fixture is True


def test_recording_documents_are_validated() -> None:
    with pytest.raises(RecordingInvalid):
        validate_recording_document({"events": []})
    with pytest.raises(RecordingInvalid):
        validate_recording_document(
            {"header": {k: "x" for k in ("format_version", "recorded_at", "project_id", "demo_mode", "model_ids", "git_sha", "app_version", "engine", "question_id")},
             "events": [{"event": "stage_started", "t_rel_ms": 5}, {"event": "stage_completed", "t_rel_ms": 1}], "result": {}}
        )
    with pytest.raises(ValueError):
        RecordingStore(Path("/tmp")).path_for("../etc", "q")


def test_pytest_run_leaves_the_committed_dev_fixture_untouched() -> None:
    """Running the generator test must not rewrite the committed fixture (``git status`` stays clean)."""
    import hashlib

    before = hashlib.sha256(DEV_FIXTURE.read_bytes()).hexdigest()
    test_generate_dev_fixture_from_mocked_live_run()
    assert hashlib.sha256(DEV_FIXTURE.read_bytes()).hexdigest() == before
