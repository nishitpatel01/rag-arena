"""Battle Score + diagnostics derived ONLY from flags, stage_log and judge metrics (spec §6.5).

Battle Score (0–100) = 40% QA Relevance + 35% Groundedness + 15% Context Precision + 10% Efficiency.
No per-engine static strings exist here: every diagnostic sentence cites a measured value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.models import EngineFlags, EvalMetrics, StageRecord
from app.recording.codec import eval_outcome_from_dict, stage_record_from_dict
from app.recording.store import RecordingStore

SCORE_WEIGHTS: dict[str, float] = {
    "qa_relevance": 0.40,
    "groundedness": 0.35,
    "context_precision": 0.15,
    "efficiency": 0.10,
}
_LATENCY_REFERENCE_MS = 5000.0
_COST_REFERENCE_USD = 0.01
_LOW_METRIC_THRESHOLD = 0.6
_MAX_DIAGNOSTICS = 8


@dataclass(frozen=True)
class ScoreBreakdown:
    """Composite score with its measured components."""

    battle_score: float
    efficiency: float
    qa_relevance: float
    groundedness: float
    context_precision: float
    latency_ms: int
    cost_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "battle_score": self.battle_score,
            "efficiency": self.efficiency,
            "qa_relevance": self.qa_relevance,
            "groundedness": self.groundedness,
            "context_precision": self.context_precision,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
        }


def efficiency_score(latency_ms: int, cost_usd: float) -> float:
    """Efficiency in [0, 1]: 1 minus half-weight latency (vs 5 s) minus cost share (vs $0.01), clamped."""
    if not isinstance(latency_ms, int) or isinstance(latency_ms, bool) or latency_ms < 0:
        raise ValueError("latency_ms must be an integer >= 0")
    if not isinstance(cost_usd, (int, float)) or cost_usd < 0:
        raise ValueError("cost_usd must be a number >= 0")
    penalty = (latency_ms / _LATENCY_REFERENCE_MS) * 0.5 + min(0.5, float(cost_usd) / _COST_REFERENCE_USD * 0.5)
    return round(max(0.0, min(1.0, 1.0 - penalty)), 3)


def battle_score(metrics: EvalMetrics, latency_ms: int, cost_usd: float) -> ScoreBreakdown:
    """Weighted composite of judge metrics and efficiency, scaled to 0–100."""
    if not isinstance(metrics, EvalMetrics):
        raise TypeError("metrics must be an EvalMetrics instance")
    eff = efficiency_score(latency_ms, cost_usd)
    score = (
        metrics.qa_relevance * SCORE_WEIGHTS["qa_relevance"]
        + metrics.groundedness * SCORE_WEIGHTS["groundedness"]
        + metrics.context_precision * SCORE_WEIGHTS["context_precision"]
        + eff * SCORE_WEIGHTS["efficiency"]
    ) * 100.0
    return ScoreBreakdown(
        battle_score=round(score, 1),
        efficiency=eff,
        qa_relevance=metrics.qa_relevance,
        groundedness=metrics.groundedness,
        context_precision=metrics.context_precision,
        latency_ms=latency_ms,
        cost_usd=float(cost_usd),
    )


def _flag_diagnostics(flags: EngineFlags) -> list[str]:
    lines = [
        "Reranker ran (Discovery Engine Ranking API)." if flags.reranker_used else "No reranking stage ran.",
        "Google ADK runner produced the tool trace." if flags.adk_runner_used else "No ADK runner turn was executed.",
    ]
    if flags.layout_source:
        lines.append(f"Chunks were parsed with layout_source='{flags.layout_source}'.")
    return lines


def _stage_diagnostics(stage_log: tuple[StageRecord, ...]) -> list[str]:
    lines: list[str] = []
    failed = [s for s in stage_log if s.status == "failed"]
    skipped = [s for s in stage_log if s.status == "skipped"]
    completed = [s for s in stage_log if s.status == "completed"]
    if failed:
        lines.append("Failed stages: " + ", ".join(f"{s.stage} ({s.error_type})" for s in failed[:4]) + ".")
    if skipped:
        lines.append("Skipped stages: " + ", ".join(s.stage for s in skipped[:6]) + ".")
    if completed:
        slowest = max(completed, key=lambda s: s.duration_ms)
        total = sum(s.duration_ms for s in completed)
        share = round(100.0 * slowest.duration_ms / total) if total > 0 else 0
        lines.append(f"Slowest stage: {slowest.stage} at {slowest.duration_ms} ms ({share}% of {total} ms measured).")
    return lines


def _metric_diagnostics(metrics: EvalMetrics) -> list[str]:
    lines: list[str] = []
    for name in ("groundedness", "qa_relevance", "context_precision"):
        value = getattr(metrics, name)
        if value < _LOW_METRIC_THRESHOLD:
            lines.append(f"Low {name}: judge scored {value:.2f} (< {_LOW_METRIC_THRESHOLD:.1f}).")
    return lines


def diagnostics(flags: EngineFlags, stage_log: tuple[StageRecord, ...], metrics: EvalMetrics) -> tuple[str, ...]:
    """Diagnostic sentences generated from measured inputs only (bounded)."""
    if not isinstance(flags, EngineFlags) or not isinstance(metrics, EvalMetrics):
        raise TypeError("flags must be EngineFlags and metrics must be EvalMetrics")
    if not isinstance(stage_log, tuple):
        raise TypeError("stage_log must be a tuple of StageRecord")
    lines = [*_flag_diagnostics(flags), *_stage_diagnostics(stage_log), *_metric_diagnostics(metrics)]
    return tuple(lines[:_MAX_DIAGNOSTICS])


def _leaderboard_row_key(rec: Any) -> tuple[str, str, str]:
    synthesis, judge = rec.pair
    return rec.engine, synthesis, judge


def leaderboard_from_recordings(store: RecordingStore) -> dict[str, Any]:
    """Aggregate recorded (live-measured) runs per (engine, synthesis_model, judge_model); never across pairs.

    Every row carries its provenance (spec §7.4). Two pairs on the same engine are two rows: their judges differ,
    so averaging them would mix incomparable scales. Synthetic dev fixtures (header ``synthetic_dev_fixture: true``)
    are never aggregated, in any mode — they may still be replayed when ``ALLOW_DEV_FIXTURES=1``.
    """
    if not isinstance(store, RecordingStore):
        raise TypeError("store must be a RecordingStore")
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for rec in store.list_recordings(include_dev=store.allow_dev_fixtures):
        if rec.evaluation is None or rec.is_dev_fixture:
            continue
        outcome = eval_outcome_from_dict(rec.evaluation)
        latency = int(rec.result.get("latency_ms", 0))
        cost = float(rec.result.get("cost_usd", 0.0)) + outcome.judge_cost_usd  # run cost includes the judge
        breakdown = battle_score(outcome.metrics, latency, cost)
        key = _leaderboard_row_key(rec)
        row = rows.setdefault(
            key,
            {"engine": key[0], "synthesis_model": key[1], "judge_model": key[2], "runs": 0,
             "sum": {k: 0.0 for k in breakdown.to_dict()}, "provenance": []},
        )
        row["runs"] += 1
        for metric, value in breakdown.to_dict().items():
            row["sum"][metric] += float(value)
        row["provenance"].append(
            {
                "question_id": rec.question_id,
                "recorded_at": rec.header.get("recorded_at"),
                "git_sha": rec.header.get("git_sha"),
                "model_ids": rec.header.get("model_ids"),
                "synthetic_dev_fixture": rec.is_dev_fixture,
            }
        )
    entries = []
    for row in rows.values():
        n = max(1, row["runs"])
        entries.append(
            {
                "engine": row["engine"],
                "synthesis_model": row["synthesis_model"],
                "judge_model": row["judge_model"],
                "runs": row["runs"],
                **{f"avg_{k}": round(v / n, 3) for k, v in row["sum"].items()},
                "provenance": row["provenance"][:50],
            }
        )
    entries.sort(key=lambda r: r["avg_battle_score"], reverse=True)
    return {"source": "recordings", "grouped_by": ["engine", "synthesis_model", "judge_model"], "engines": entries}


def stage_log_from_dicts(raw: list[dict[str, Any]]) -> tuple[StageRecord, ...]:
    """Helper for callers holding serialized stage logs (query_runs rows, recordings)."""
    return tuple(stage_record_from_dict(item) for item in list(raw)[:100])


__all__ = [
    "SCORE_WEIGHTS",
    "ScoreBreakdown",
    "battle_score",
    "diagnostics",
    "efficiency_score",
    "leaderboard_from_recordings",
    "stage_log_from_dicts",
]
