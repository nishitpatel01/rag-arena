"""Battle Score + diagnostics derive only from measured inputs (spec §6.5); no static per-engine strings."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.models import EngineFlags, EvalMetrics, StageRecord
from app.evaluation.scoring import (
    SCORE_WEIGHTS,
    battle_score,
    diagnostics,
    efficiency_score,
    leaderboard_from_recordings,
)
from app.recording.store import RecordingStore

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_battle_score_weights_sum_to_one_and_scale_to_100() -> None:
    assert sum(SCORE_WEIGHTS.values()) == pytest.approx(1.0)
    perfect = battle_score(EvalMetrics(1.0, 1.0, 1.0), latency_ms=0, cost_usd=0.0)
    assert perfect.battle_score == 100.0 and perfect.efficiency == 1.0
    zero = battle_score(EvalMetrics(0.0, 0.0, 0.0), latency_ms=10_000, cost_usd=1.0)
    assert zero.battle_score == 0.0 and zero.efficiency == 0.0
    mid = battle_score(EvalMetrics(groundedness=0.8, qa_relevance=0.6, context_precision=0.5), latency_ms=2500, cost_usd=0.0)
    expected = (0.6 * 0.40 + 0.8 * 0.35 + 0.5 * 0.15 + 0.75 * 0.10) * 100
    assert mid.battle_score == pytest.approx(round(expected, 1))
    assert efficiency_score(2500, 0.0) == 0.75
    with pytest.raises(ValueError):
        efficiency_score(-1, 0.0)


def test_diagnostics_only_mention_measured_facts() -> None:
    flags = EngineFlags(reranker_used=False, adk_runner_used=False, layout_source="pymupdf")
    log = (
        StageRecord("embed_query", "Embed", "Vertex AI", "completed", "t", 120),
        StageRecord("retrieve", "BQ", "BigQuery", "completed", "t", 880),
        StageRecord("rerank", "rerank", "", "skipped", "t", 0, payload={"reason": "n/a"}),
    )
    lines = diagnostics(flags, log, EvalMetrics(groundedness=0.55, qa_relevance=0.9, context_precision=0.7))
    text = " ".join(lines)
    assert "No reranking stage ran." in lines
    assert "No ADK runner turn was executed." in lines
    assert "layout_source='pymupdf'" in text
    assert "Skipped stages: rerank." in lines
    assert "Slowest stage: retrieve at 880 ms (88% of 1000 ms measured)." in lines
    assert "Low groundedness: judge scored 0.55 (< 0.6)." in lines
    assert "qa_relevance" not in text
    for forbidden in ("Discovery Engine cross-encoder reranking", "Google ADK invoked", "Arena Champion"):
        assert forbidden not in text
    used = diagnostics(EngineFlags(reranker_used=True, adk_runner_used=True), (), EvalMetrics(1, 1, 1))
    assert used[0].startswith("Reranker ran") and used[1].startswith("Google ADK runner")


def test_leaderboard_from_recordings_carries_provenance_and_skips_dev_fixtures_by_default() -> None:
    root = _PROJECT_ROOT / "data" / "recordings"
    strict = leaderboard_from_recordings(RecordingStore(root, allow_dev_fixtures=False))
    assert strict["source"] == "recordings"
    assert all(not p["synthetic_dev_fixture"] for row in strict["engines"] for p in row["provenance"])
    permissive = leaderboard_from_recordings(RecordingStore(root, allow_dev_fixtures=True))
    l1 = next(row for row in permissive["engines"] if row["engine"] == "level1_naive")
    assert l1["runs"] >= 1 and 0.0 <= l1["avg_battle_score"] <= 100.0
    prov = l1["provenance"][0]
    assert {"question_id", "recorded_at", "git_sha", "model_ids", "synthetic_dev_fixture"} <= set(prov)
    assert any(p["synthetic_dev_fixture"] for p in l1["provenance"])
