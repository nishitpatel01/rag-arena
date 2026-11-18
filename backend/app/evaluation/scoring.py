"""Battle Score + diagnostics derived ONLY from flags, stage_log and judge metrics (spec §6.5).

Battle Score (0–100) = 40% QA Relevance + 35% Groundedness + 15% Context Precision + 10% Efficiency.
Efficiency is computed from measured engine latency (vs 60 s) and operational **Engine Cost** (vs $0.20),
excluding external LLM-judge / evaluation overhead so expensive autoraters never penalize fast architectures.
No per-engine static strings exist here: every diagnostic sentence cites a measured value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.models import EngineFlags, EvalMetrics, StageRecord
from app.recording.codec import stage_record_from_dict

SCORE_WEIGHTS: dict[str, float] = {
    "qa_relevance": 0.40,
    "groundedness": 0.35,
    "context_precision": 0.15,
    "efficiency": 0.10,
}
_LATENCY_REFERENCE_MS = 60_000.0  # owner decision 2026-10-23: real runs take 15–57 s
_COST_REFERENCE_USD = 0.20  # owner decision 2026-10-23: real runs cost $0.003–$0.18 engine USD
_LOW_METRIC_THRESHOLD = 0.6
_MAX_DIAGNOSTICS = 8


def _validate_non_negative_number(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) < 0.0:
        raise ValueError(f"{name} must be a number >= 0")
    return float(value)


def resolve_engine_cost_usd(
    cost_usd: float | None,
    *,
    engine_cost_usd: float | None = None,
    judge_cost_usd: float | None = None,
) -> float | None:
    """Resolve scored operational engine cost, falling back to ``max(0, cost_usd - judge_cost_usd)`` when needed."""
    if engine_cost_usd is not None:
        return round(_validate_non_negative_number(engine_cost_usd, "engine_cost_usd"), 8)
    if cost_usd is None:
        return None
    total = _validate_non_negative_number(cost_usd, "cost_usd")
    if judge_cost_usd is not None:
        judge = _validate_non_negative_number(judge_cost_usd, "judge_cost_usd")
        return round(max(0.0, total - judge), 8)
    return round(total, 8)


@dataclass(frozen=True)
class ScoreBreakdown:
    """Composite score with its measured components (``cost_usd`` = scored operational engine cost)."""

    battle_score: float
    efficiency: float
    qa_relevance: float
    groundedness: float
    context_precision: float
    latency_ms: int
    cost_usd: float
    engine_cost_usd: float | None = None
    judge_cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        engine_cost = round(float(self.cost_usd if self.engine_cost_usd is None else self.engine_cost_usd), 8)
        out: dict[str, Any] = {
            "battle_score": self.battle_score,
            "efficiency": self.efficiency,
            "qa_relevance": self.qa_relevance,
            "groundedness": self.groundedness,
            "context_precision": self.context_precision,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "engine_cost_usd": engine_cost,
        }
        if self.judge_cost_usd is not None:
            judge_cost = round(float(self.judge_cost_usd), 8)
            out["judge_cost_usd"] = judge_cost
            out["total_cost_usd"] = round(engine_cost + judge_cost, 8)
        return out


def efficiency_score(latency_ms: int, cost_usd: float) -> float:
    """Efficiency in [0, 1]: 1 minus half-weight latency (vs 60 s) minus engine cost share (vs $0.20), clamped."""
    if not isinstance(latency_ms, int) or isinstance(latency_ms, bool) or latency_ms < 0:
        raise ValueError("latency_ms must be an integer >= 0")
    engine_cost = _validate_non_negative_number(cost_usd, "cost_usd")
    penalty = (latency_ms / _LATENCY_REFERENCE_MS) * 0.5 + min(0.5, engine_cost / _COST_REFERENCE_USD * 0.5)
    return round(max(0.0, min(1.0, 1.0 - penalty)), 3)


def battle_score(
    metrics: EvalMetrics,
    latency_ms: int,
    cost_usd: float,
    *,
    judge_cost_usd: float | None = None,
) -> ScoreBreakdown:
    """Weighted composite of judge metrics and engine-only operational efficiency, scaled to 0–100."""
    if not isinstance(metrics, EvalMetrics):
        raise TypeError("metrics must be an EvalMetrics instance")
    engine_cost = _validate_non_negative_number(cost_usd, "cost_usd")
    judge_cost = (
        None if judge_cost_usd is None else _validate_non_negative_number(judge_cost_usd, "judge_cost_usd")
    )
    eff = efficiency_score(latency_ms, engine_cost)
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
        cost_usd=engine_cost,
        engine_cost_usd=engine_cost,
        judge_cost_usd=judge_cost,
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


def stage_log_from_dicts(raw: list[dict[str, Any]]) -> tuple[StageRecord, ...]:
    """Helper for callers holding serialized stage logs (query_runs rows, recordings)."""
    return tuple(stage_record_from_dict(item) for item in list(raw)[:100])


__all__ = [
    "SCORE_WEIGHTS",
    "ScoreBreakdown",
    "battle_score",
    "diagnostics",
    "efficiency_score",
    "resolve_engine_cost_usd",
    "stage_log_from_dicts",
]
