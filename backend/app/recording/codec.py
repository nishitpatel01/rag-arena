"""JSON codec for EngineResult / StageRecord round-trips used by recordings (spec §6.4)."""

from __future__ import annotations

from typing import Any

from app.core.models import (
    AgentStep,
    BoundingBox,
    Citation,
    DocumentChunk,
    EngineFlags,
    EngineResult,
    EvalMetrics,
    EvalOutcome,
    StageRecord,
    TokenUsage,
)

_MAX_LIST_ITEMS = 100


class RecordingCodecError(ValueError):
    """Raised when a recording document does not deserialize into valid models."""


def _require_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecordingCodecError(f"{name} must be a JSON object, got {type(value).__name__}")
    return value


def _bbox_from_dict(raw: Any) -> BoundingBox:
    d = _require_dict(raw, "bbox")
    return BoundingBox(page=int(d["page"]), x0=float(d["x0"]), y0=float(d["y0"]), x1=float(d["x1"]), y1=float(d["y1"]))


def _chunk_from_dict(raw: Any) -> DocumentChunk:
    d = _require_dict(raw, "retrieved_chunks[]")
    return DocumentChunk(
        chunk_id=str(d["chunk_id"]),
        tier_profile=str(d["tier_profile"]),
        solicitation_id=str(d["solicitation_id"]),
        doc_name=str(d["doc_name"]),
        doc_type=str(d["doc_type"]),
        element_type=str(d["element_type"]),
        section_id=str(d["section_id"]) if d.get("section_id") else None,
        amendment_number=int(d.get("amendment_number", 0)),
        effective_date=str(d["effective_date"]) if d.get("effective_date") else None,
        amends_section=str(d["amends_section"]) if d.get("amends_section") else None,
        page_number=int(d["page_number"]),
        bbox=_bbox_from_dict(d["bbox"]),
        content=str(d["content"]),
        score=float(d.get("score", 0.0)),
    )


def _citation_from_dict(raw: Any) -> Citation:
    d = _require_dict(raw, "citations[]")
    return Citation(
        chunk_id=str(d["chunk_id"]),
        doc_name=str(d["doc_name"]),
        solicitation_id=str(d["solicitation_id"]),
        page_number=int(d["page_number"]),
        section_id=str(d["section_id"]) if d.get("section_id") else None,
        element_type=str(d["element_type"]),
        bbox=_bbox_from_dict(d["bbox"]),
        excerpt=str(d.get("excerpt", "")),
        layout_source=str(d.get("layout_source") or ""),
        bbox_source=str(d.get("bbox_source") or ""),
    )


def _agent_step_from_dict(raw: Any) -> AgentStep:
    d = _require_dict(raw, "agent_steps[]")
    return AgentStep(
        step_number=int(d["step_number"]),
        tool_name=str(d["tool_name"]),
        tool_args=dict(d.get("tool_args") or {}),
        result_summary=str(d.get("result_summary", "")),
        latency_ms=int(d.get("latency_ms", 0)),
    )


def stage_record_from_dict(raw: Any) -> StageRecord:
    """Deserialize one StageRecord."""
    d = _require_dict(raw, "stage_log[]")
    return StageRecord(
        stage=str(d["stage"]),
        label=str(d.get("label", d["stage"])),
        service=str(d.get("service", "")),
        status=str(d["status"]),
        started_at=str(d.get("started_at", "")),
        duration_ms=int(d.get("duration_ms", 0)),
        request=dict(d.get("request") or {}),
        payload=dict(d.get("payload") or {}),
        error_type=str(d["error_type"]) if d.get("error_type") else None,
        error=str(d["error"]) if d.get("error") else None,
    )


def engine_result_from_dict(raw: Any) -> EngineResult:
    """Deserialize an ``EngineResult.to_dict()`` document; raises ``RecordingCodecError`` on malformed input."""
    d = _require_dict(raw, "result")
    try:
        usage_d = _require_dict(d.get("usage", {}), "usage")
        flags_d = _require_dict(d.get("flags", {}), "flags")
        return EngineResult(
            engine_id=str(d["engine_id"]),
            engine_label=str(d["engine_label"]),
            answer=str(d["answer"]),
            citations=[_citation_from_dict(c) for c in list(d.get("citations", []))[:_MAX_LIST_ITEMS]],
            retrieved_chunks=[_chunk_from_dict(c) for c in list(d.get("retrieved_chunks", []))[:_MAX_LIST_ITEMS]],
            agent_steps=[_agent_step_from_dict(s) for s in list(d.get("agent_steps", []))[:_MAX_LIST_ITEMS]],
            latency_ms=int(d["latency_ms"]),
            cost_usd=float(d["cost_usd"]),
            stage_log=tuple(stage_record_from_dict(s) for s in list(d.get("stage_log", []))[:_MAX_LIST_ITEMS]),
            usage=TokenUsage(**{k: usage_d[k] for k in TokenUsage.__dataclass_fields__ if k in usage_d}),
            flags=EngineFlags(**{k: flags_d[k] for k in EngineFlags.__dataclass_fields__ if k in flags_d}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RecordingCodecError(f"Malformed EngineResult document: {exc}") from exc


def eval_outcome_from_dict(raw: Any) -> EvalOutcome:
    """Deserialize a recorded evaluation (``metrics``, ``judge_model``, ``reference_answer_used``, ``duration_ms`` + judge usage)."""
    d = _require_dict(raw, "evaluation")
    try:
        m = _require_dict(d["metrics"], "evaluation.metrics")
        return EvalOutcome(
            metrics=EvalMetrics(
                groundedness=float(m["groundedness"]),
                qa_relevance=float(m["qa_relevance"]),
                context_precision=float(m["context_precision"]),
            ),
            judge_model=str(d["judge_model"]),
            reference_answer_used=bool(d["reference_answer_used"]),
            duration_ms=int(d["duration_ms"]),
            # judge provenance/usage keys are absent in recordings made before the Claude judge
            judge_provider=str(d.get("judge_provider", "")),
            judge_location=str(d.get("judge_location", "")),
            input_tokens=int(d.get("input_tokens", 0)),
            output_tokens=int(d.get("output_tokens", 0)),
            judge_cost_usd=float(d.get("judge_cost_usd", 0.0)),
            rationale=str(d.get("rationale", "")),
            judge_response_id=str(d.get("judge_response_id", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RecordingCodecError(f"Malformed evaluation document: {exc}") from exc


def eval_outcome_to_dict(outcome: EvalOutcome) -> dict[str, Any]:
    """Serialize an EvalOutcome for recordings and SSE ``eval_complete`` frames."""
    if not isinstance(outcome, EvalOutcome):
        raise TypeError("outcome must be an EvalOutcome")
    return {
        "metrics": outcome.metrics.to_dict(),
        "judge_model": outcome.judge_model,
        "reference_answer_used": outcome.reference_answer_used,
        "duration_ms": outcome.duration_ms,
        "judge_provider": outcome.judge_provider,
        "judge_location": outcome.judge_location,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "judge_cost_usd": outcome.judge_cost_usd,
        "rationale": outcome.rationale,
        "judge_response_id": outcome.judge_response_id,
    }


__all__ = [
    "RecordingCodecError",
    "engine_result_from_dict",
    "eval_outcome_from_dict",
    "eval_outcome_to_dict",
    "stage_record_from_dict",
]
