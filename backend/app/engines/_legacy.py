"""Shared helper for v2 engine bodies that are not yet rebuilt stage-by-stage (Levels 2–5, Phases 2–5).

A legacy engine declares its canonical ``stages`` (spec §3) but cannot yet emit them individually. To stay
honest (spec P5) it emits ``stage_skipped`` for every canonical stage with the reason, then runs the whole
v2 body under one measured ``legacy`` stage. Nothing is labelled as a service that did not run.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Awaitable, Callable

from app.core.models import EngineResult, TokenUsage
from app.core.telemetry import StageEmitter

LEGACY_STAGE_ID = "legacy"


def usage_from_generate_response(gen_resp: Any, model: str, embedding_tokens: int, embedding_model: str) -> TokenUsage:
    """Build TokenUsage strictly from ``response.usage_metadata`` (fail loud when absent)."""
    meta = getattr(gen_resp, "usage_metadata", None)
    if meta is None:
        raise ValueError("generate_content response carries no usage_metadata; cannot compute measured cost")
    return TokenUsage(
        prompt_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
        output_tokens=int(getattr(meta, "candidates_token_count", 0) or 0),
        thinking_tokens=int(getattr(meta, "thoughts_token_count", 0) or 0),
        embedding_tokens=embedding_tokens,
        model=model,
        embedding_model=embedding_model,
    )


async def run_legacy_body(
    engine: Any,
    emitter: StageEmitter,
    *,
    phase: str,
    query: str,
    body: Callable[[], Awaitable[EngineResult]],
) -> EngineResult:
    """Emit skipped canonical stages + one measured ``legacy`` stage around ``body``; attach the stage log."""
    if not isinstance(phase, str) or not phase.strip():
        raise ValueError("phase must be a non-empty string")
    reason = f"v2 implementation — per-stage telemetry lands in {phase}"
    for stage_id in engine.stages:
        if stage_id != "evaluate":
            await emitter.skipped(stage_id, reason=reason)
    handle = await emitter.started(
        LEGACY_STAGE_ID,
        label="v2 implementation (single measured stage)",
        service=f"{engine.engine_label} (legacy body)",
        request={"query": query},
    )
    try:
        result = await body()
    except BaseException as exc:
        await emitter.failed(handle, error=exc)
        raise
    await emitter.completed(
        handle,
        payload={"answer": result.answer, "retrieved_chunks": len(result.retrieved_chunks), "flags": result.flags.to_dict()},
    )
    return replace(result, stage_log=engine.stage_log_of(emitter))


__all__ = ["LEGACY_STAGE_ID", "run_legacy_body", "usage_from_generate_response"]
