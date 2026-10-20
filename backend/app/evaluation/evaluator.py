"""RAG Triad evaluation (spec §6.5): the LLM judge is the ONLY source of metrics.

* ``live``/``record``: structured-output judge (``gemini-4-argon``, ``thinking_level=LOW``, ``temperature=0``).
* ``replay``: the recorded judge outcome is replayed (``replay_evaluation``); nothing is re-scored locally.

There is no token-overlap or heuristic scorer anymore (audit §A.2).
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
import json
from pathlib import Path
import time
from typing import Any

from app.core.config import ArenaSettings, get_settings
from app.core.gcp_clients import get_genai_client
from app.core.models import EngineResult, EvalMetrics, EvalOutcome
from app.core.telemetry import StageEmitter
from app.engines.replay_engine import recording_store_for, replay_evaluation

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BENCHMARK_PATH = _PROJECT_ROOT / "data" / "golden_benchmarks" / "procurement_benchmark.json"
_MAX_QUERY_CHARS = 4000
_MAX_CONTEXT_CHUNKS = 6
_MAX_CHUNK_CHARS = 2400
_MAX_BENCHMARK_QUESTIONS = 200
_METRIC_KEYS: tuple[str, ...] = ("groundedness", "qa_relevance", "context_precision")
_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {k: {"type": "number"} for k in _METRIC_KEYS},
    "required": list(_METRIC_KEYS),
}


class JudgeResponseError(ValueError):
    """Raised when the judge model returns something that is not a complete metric triplet."""


def _clamp_score(value: float) -> float:
    """Clamp an evaluation metric score to [0.0, 1.0] rounded to 3 decimal places."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise JudgeResponseError(f"Metric value must be numeric, got {value!r}")
    return round(min(1.0, max(0.0, float(value))), 3)


@lru_cache(maxsize=4)
def _load_reference_answers(benchmark_path: str) -> dict[str, str]:
    """Load ``question_id -> reference_answer`` from the golden benchmark; malformed files raise."""
    path = Path(benchmark_path)
    if not path.is_file():
        raise FileNotFoundError(f"Benchmark catalog not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data.get("questions")
    if not isinstance(questions, list):
        raise ValueError(f"Benchmark catalog {path} has no 'questions' list")
    out: dict[str, str] = {}
    for item in questions[:_MAX_BENCHMARK_QUESTIONS]:
        if not isinstance(item, dict) or not item.get("question_id"):
            raise ValueError(f"Benchmark catalog {path} contains a question without question_id")
        ref = item.get("reference_answer") or item.get("ground_truth_answer")
        if ref:
            out[str(item["question_id"]).strip()] = str(ref).strip()
    return out


def lookup_reference_answer(question_id: str | None, benchmark_path: Path = DEFAULT_BENCHMARK_PATH) -> str | None:
    """Return the golden reference answer for ``question_id`` (None for ad-hoc questions)."""
    if not isinstance(question_id, str) or not question_id.strip():
        return None
    return _load_reference_answers(str(benchmark_path)).get(question_id.strip())


def _build_judge_prompt(query: str, result: EngineResult, reference_answer: str | None) -> str:
    chunk_texts = [c.content[:_MAX_CHUNK_CHARS] for c in result.retrieved_chunks[:_MAX_CONTEXT_CHUNKS]]
    agent_texts = [f"[ADK Tool {s.tool_name}]: {s.result_summary}" for s in result.agent_steps[:8]]
    context_summary = "\n---\n".join([*chunk_texts, *agent_texts])
    return (
        "You are an enterprise RAG evaluation judge. Score the response on three metrics in [0.0, 1.0]:\n"
        "1. groundedness: Are all claims supported by the retrieved context without hallucination?\n"
        "2. qa_relevance: Does the answer directly and completely address the user query"
        + (" and agree with the reference answer?" if reference_answer else "?")
        + "\n3. context_precision: Are the retrieved chunks focused and relevant to the query?\n"
        f"Query: {query}\n"
        f"Reference Answer: {reference_answer or 'N/A'}\n"
        f"Retrieved Context:\n{context_summary}\n"
        f"Candidate Answer:\n{result.answer}\n"
        'Return strict JSON: {"groundedness": float, "qa_relevance": float, "context_precision": float}'
    )


def _parse_judge_response(raw_text: str) -> EvalMetrics:
    try:
        parsed = json.loads(raw_text)
    except ValueError as exc:
        raise JudgeResponseError(f"Judge returned non-JSON output: {raw_text[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise JudgeResponseError("Judge JSON must be an object")
    missing = [k for k in _METRIC_KEYS if k not in parsed]
    if missing:
        raise JudgeResponseError(f"Judge JSON is missing metrics {missing}")
    return EvalMetrics(**{k: _clamp_score(parsed[k]) for k in _METRIC_KEYS})


def evaluate_engine_result(
    query: str,
    result: EngineResult,
    reference_answer: str | None = None,
    settings: ArenaSettings | None = None,
) -> EvalOutcome:
    """Judge an EngineResult with Gemini (live/record only). Returns ``(metrics, judge_model, reference_answer_used, duration_ms)``."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if len(query.strip()) > _MAX_QUERY_CHARS:
        raise ValueError(f"query exceeds maximum length of {_MAX_QUERY_CHARS} characters")
    if not isinstance(result, EngineResult):
        raise TypeError("result must be an EngineResult instance")
    if reference_answer is not None and not isinstance(reference_answer, str):
        raise TypeError("reference_answer must be a string or None")
    resolved = get_settings() if settings is None else settings
    if not isinstance(resolved, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance or None")
    if not resolved.is_live:
        raise RuntimeError("evaluate_engine_result judges live runs only; replay uses the recorded evaluation")

    client = get_genai_client(resolved)
    judge_model = resolved.gemini_fast_model
    t0 = time.perf_counter()
    response = client.models.generate_content(
        model=judge_model,
        contents=_build_judge_prompt(query.strip(), result, reference_answer),
        config={
            "temperature": 0.0,
            "response_mime_type": "application/json",
            "response_schema": _JUDGE_SCHEMA,
            "thinking_config": {"thinking_level": "LOW"},
        },
    )
    metrics = _parse_judge_response(str(getattr(response, "text", "") or "").strip())
    return EvalOutcome(
        metrics=metrics,
        judge_model=judge_model,
        reference_answer_used=bool(reference_answer and reference_answer.strip()),
        duration_ms=max(1, int((time.perf_counter() - t0) * 1000)),
    )


async def run_evaluation(
    query: str,
    result: EngineResult,
    *,
    question_id: str | None,
    settings: ArenaSettings,
    emitter: StageEmitter,
) -> EvalOutcome:
    """Emit the ``evaluate`` stage around the judge (live/record) or replay the recorded judge (replay)."""
    if not isinstance(settings, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance")
    if settings.demo_mode == "replay":
        return await replay_evaluation(
            recording_store_for(settings),
            engine_id=result.engine_id,
            question_id=question_id,
            emitter=emitter,
            replay_speed=settings.replay_speed,
        )
    reference = lookup_reference_answer(question_id)
    judge_model = settings.gemini_fast_model
    handle = await emitter.started(
        "evaluate",
        label="LLM-as-a-judge",
        service=f"Vertex AI · {judge_model}",
        request={"judge_model": judge_model, "thinking_level": "LOW", "temperature": 0.0, "reference_answer_used": bool(reference)},
    )
    try:
        outcome = await asyncio.to_thread(evaluate_engine_result, query, result, reference, settings)
    except BaseException as exc:
        await emitter.failed(handle, error=exc)
        raise
    await emitter.completed(
        handle,
        payload={"judge_model": outcome.judge_model, **outcome.metrics.to_dict(), "reference_answer_used": outcome.reference_answer_used},
    )
    return outcome


__all__ = [
    "DEFAULT_BENCHMARK_PATH",
    "JudgeResponseError",
    "evaluate_engine_result",
    "lookup_reference_answer",
    "run_evaluation",
]
