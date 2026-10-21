"""``query_runs`` provenance rows (spec §5): one row per live/record engine run, written with a bounded load job."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from app.core.config import ArenaSettings
from app.core.models import EngineResult, EvalOutcome
from app.core.telemetry import BaseStageEmitter, utc_now_iso
from app.recording.recorder import read_git_sha

QUERY_RUN_WRITE_TIMEOUT_SECONDS = 90.0
_MAX_CITATIONS = 20


class QueryRunWriter(Protocol):
    """Sink for ``query_runs`` rows (live: BigQuery load job; tests: fake)."""

    def write_query_run(self, row: dict[str, Any]) -> None: ...


def run_cost_usd(result: EngineResult, outcome: EvalOutcome) -> float:
    """A run's total cost: the engine's measured cost plus the LLM judge's measured cost."""
    if not isinstance(result, EngineResult) or not isinstance(outcome, EvalOutcome):
        raise TypeError("result must be an EngineResult and outcome an EvalOutcome")
    return round(float(result.cost_usd) + float(outcome.judge_cost_usd), 8)


def build_query_run_row(
    *,
    settings: ArenaSettings,
    run_id: str,
    question_id: str | None,
    query: str,
    started_at: str,
    emitter: BaseStageEmitter,
    result: EngineResult,
    outcome: EvalOutcome,
) -> dict[str, Any]:
    """Assemble the spec §5 row from measured values only (stage_log includes the judge's ``evaluate`` stage)."""
    if not isinstance(result, EngineResult) or not isinstance(outcome, EvalOutcome):
        raise TypeError("result must be an EngineResult and outcome an EvalOutcome")
    if not run_id or not started_at:
        raise ValueError("run_id and started_at are required")
    return {
        "run_id": run_id,
        "engine": result.engine_id,
        "question_id": question_id,
        "query": query,
        "started_at": started_at,
        "latency_ms": int(result.latency_ms),
        "cost_usd": run_cost_usd(result, outcome),
        "groundedness": outcome.metrics.groundedness,
        "qa_relevance": outcome.metrics.qa_relevance,
        "context_precision": outcome.metrics.context_precision,
        "stage_log": [r.to_dict() for r in emitter.records],
        "answer": result.answer,
        "citations": [c.to_dict() for c in result.citations[:_MAX_CITATIONS]],
        "demo_mode": settings.demo_mode,
        "git_sha": read_git_sha(),
        # The pair that actually ran (measured provenance, not the request): synthesis from usage, judge from outcome.
        "synthesis_model": result.usage.model or None,
        "judge_model": outcome.judge_model or None,
    }


async def write_query_run_or_warn(
    writer: QueryRunWriter | None, row: dict[str, Any], *, queue: asyncio.Queue, run_id: str, engine_id: str
) -> bool:
    """Bounded write; a failure becomes a visible ``stream_warning`` (the user's answer is already delivered)."""
    if writer is None:
        return False
    try:
        await asyncio.wait_for(asyncio.to_thread(writer.write_query_run, row), timeout=QUERY_RUN_WRITE_TIMEOUT_SECONDS)
    except Exception as exc:  # boundary: surfaced to the client, never swallowed
        message = str(exc).strip() or type(exc).__name__
        await queue.put(
            {
                "event": "stream_warning", "run_id": run_id, "engine": engine_id,
                "message": f"query_runs write failed ({type(exc).__name__}): {message}"[:2000], "ts": utc_now_iso(),
            }
        )
        return False
    return True


__all__ = [
    "QUERY_RUN_WRITE_TIMEOUT_SECONDS",
    "QueryRunWriter",
    "build_query_run_row",
    "run_cost_usd",
    "write_query_run_or_warn",
]
