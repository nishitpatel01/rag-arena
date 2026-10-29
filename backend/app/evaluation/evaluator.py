"""RAG Triad evaluation (spec §6.5): the LLM judge is the ONLY source of metrics.

* ``live``/``record``: the run's selected judge model (catalog ``RunModels.judge``; default ``JUDGE_MODEL``, Claude
  Opus 5.5 on Vertex AI) scores the answer; it always differs from the run's synthesis model. Provider-agnostic:
  a Gemini judge may grade a Claude answer and vice versa. See ``app.evaluation.judges``.
* ``replay``: the recorded judge outcome of the selected (synthesis, judge) pair is replayed
  (``replay_evaluation``); nothing is re-scored locally.

There is no token-overlap or heuristic scorer, and no fallback judge: a judge failure fails the ``evaluate``
stage (``stage_failed`` → ``engine_error``).
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
import json
from pathlib import Path
import time

from app.core.config import ArenaSettings, get_settings
from app.core.models import EngineResult, EvalOutcome
from app.core.models_catalog import ModelSpec, RunModels
from app.core.pricing import judge_cost_usd
from app.core.telemetry import StageEmitter
from app.engines.replay_engine import recording_store_for, replay_evaluation, replay_pair
from app.evaluation.judges import (
    MAX_RATIONALE_CHARS,
    PROVIDER_ANTHROPIC_VERTEX,
    Judge,
    JudgeResponseError,
    build_judge,
    judge_service_label,
    judge_service_label_for,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BENCHMARK_PATH = _PROJECT_ROOT / "data" / "golden_benchmarks" / "procurement_benchmark.json"
_MAX_QUERY_CHARS = 4000
_MAX_CONTEXT_CHUNKS = 24  # Levels 1–3 pass top-5; Level 4 passes every tool-returned chunk (deduped, ≤ 24)
_MAX_CHUNK_CHARS = 2400
_MAX_BENCHMARK_QUESTIONS = 200


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


def build_judge_system_prompt(has_reference: bool) -> str:
    """The judge rubric (system prompt): three metrics in [0, 1] and a strict single-JSON-object reply."""
    return (
        "You are an enterprise RAG evaluation judge. Score the response on three metrics in [0.0, 1.0]:\n"
        "1. groundedness: Are all claims supported by the retrieved context without hallucination?\n"
        "2. qa_relevance: Does the answer directly and completely address the user query"
        + (" and agree with the reference answer?" if has_reference else "?")
        + "\n3. context_precision: Are the retrieved chunks focused and relevant to the query?\n"
        "Reply with ONLY one JSON object and nothing else:\n"
        '{"groundedness": float, "qa_relevance": float, "context_precision": float, '
        '"rationale": "<= 3 sentences explaining the scores"}'
    )


def build_judge_user_prompt(query: str, result: EngineResult, reference_answer: str | None) -> str:
    """The case under evaluation (user message): query, reference, retrieved context and candidate answer."""
    chunk_texts = [c.content[:_MAX_CHUNK_CHARS] for c in result.retrieved_chunks[:_MAX_CONTEXT_CHUNKS]]
    agent_texts = [f"[ADK Tool {s.tool_name}]: {s.result_summary}" for s in result.agent_steps[:8]]
    context_summary = "\n---\n".join([*chunk_texts, *agent_texts])
    return (
        f"Query: {query}\n"
        f"Reference Answer: {reference_answer or 'N/A'}\n"
        f"Retrieved Context:\n{context_summary}\n"
        f"Candidate Answer:\n{result.answer}\n"
    )


def _validate_evaluate_inputs(query: str, result: EngineResult, reference_answer: str | None) -> None:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if len(query.strip()) > _MAX_QUERY_CHARS:
        raise ValueError(f"query exceeds maximum length of {_MAX_QUERY_CHARS} characters")
    if not isinstance(result, EngineResult):
        raise TypeError("result must be an EngineResult instance")
    if reference_answer is not None and not isinstance(reference_answer, str):
        raise TypeError("reference_answer must be a string or None")


def evaluate_engine_result(
    query: str,
    result: EngineResult,
    reference_answer: str | None = None,
    settings: ArenaSettings | None = None,
    judge: Judge | None = None,
) -> EvalOutcome:
    """Judge an EngineResult with the configured judge (live/record only); returns metrics + judge provenance."""
    _validate_evaluate_inputs(query, result, reference_answer)
    resolved = get_settings() if settings is None else settings
    if not isinstance(resolved, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance or None")
    if not resolved.is_live:
        raise RuntimeError("evaluate_engine_result judges live runs only; replay uses the recorded evaluation")
    active = build_judge(resolved) if judge is None else judge
    has_reference = bool(reference_answer and reference_answer.strip())
    t0 = time.perf_counter()
    verdict = active.score(
        build_judge_system_prompt(has_reference), build_judge_user_prompt(query.strip(), result, reference_answer)
    )
    return EvalOutcome(
        metrics=verdict.metrics,
        judge_model=active.model,
        reference_answer_used=has_reference,
        duration_ms=max(1, int((time.perf_counter() - t0) * 1000)),
        judge_provider=active.provider,
        judge_location=active.location,
        input_tokens=verdict.input_tokens,
        output_tokens=verdict.output_tokens,
        judge_cost_usd=judge_cost_usd(active.model, input_tokens=verdict.input_tokens, output_tokens=verdict.output_tokens),
        rationale=verdict.rationale[:MAX_RATIONALE_CHARS],
        judge_response_id=verdict.response_id,
    )


def _evaluate_request(judge: Judge, settings: ArenaSettings, has_reference: bool) -> dict[str, object]:
    """The judge request summary exactly as sent (Claude: no temperature; Gemini: temperature 0, thinking LOW)."""
    is_claude = judge.provider == PROVIDER_ANTHROPIC_VERTEX
    request: dict[str, object] = {
        "model": judge.model,
        "provider": judge.provider,
        "judge_provider": judge.provider,
        "judge_model": judge.model,
        "judge_location": judge.location,
        "max_tokens": settings.judge_max_tokens,
        "reference_answer_used": has_reference,
    }
    if not is_claude:
        request.update({"temperature": 0.0, "thinking_level": "LOW"})
    return request


def _evaluate_payload(outcome: EvalOutcome) -> dict[str, object]:
    return {
        "judge_provider": outcome.judge_provider,
        "judge_model": outcome.judge_model,
        "judge_location": outcome.judge_location,
        **outcome.metrics.to_dict(),
        "reference_answer_used": outcome.reference_answer_used,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "judge_cost_usd": outcome.judge_cost_usd,
        "rationale": outcome.rationale[:MAX_RATIONALE_CHARS],
        "judge_response_id": outcome.judge_response_id,
    }


def _judge_for_run(settings: ArenaSettings, judge_spec: ModelSpec | None) -> tuple[Judge, str]:
    """(judge, evaluate-stage service label) for the run's selected judge model, else the JUDGE_* settings judge."""
    if judge_spec is None:
        return build_judge(settings), judge_service_label(settings)
    return build_judge(settings, judge_spec), judge_service_label_for(judge_spec.label)


async def run_evaluation(
    query: str,
    result: EngineResult,
    *,
    question_id: str | None,
    settings: ArenaSettings,
    emitter: StageEmitter,
    models: RunModels | None = None,
    solicitation_filter: str | None = None,
) -> EvalOutcome:
    """Emit the ``evaluate`` stage around the run's judge (live/record) or replay the recorded pair's judge."""
    if not isinstance(settings, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance")
    if models is not None and not isinstance(models, RunModels):
        raise TypeError("models must be a RunModels or None")
    if settings.demo_mode == "replay":
        return await replay_evaluation(
            recording_store_for(settings),
            engine_id=result.engine_id,
            question_id=question_id,
            emitter=emitter,
            replay_speed=settings.replay_speed,
            pair=replay_pair(settings, models),
        )
    if models is not None and result.usage.model and result.usage.model == models.judge.id:
        raise ValueError(f"judge model {models.judge.id} also synthesized this answer; an LLM must not grade itself")
    judge, service = _judge_for_run(settings, models.judge if models else None)
    is_upload = isinstance(solicitation_filter, str) and solicitation_filter.startswith("upl_")
    reference = None if is_upload else lookup_reference_answer(question_id)
    handle = await emitter.started(
        "evaluate", label="LLM-as-a-judge", service=service, request=_evaluate_request(judge, settings, bool(reference))
    )
    try:
        outcome = await asyncio.to_thread(evaluate_engine_result, query, result, reference, settings, judge)
    except BaseException as exc:
        await emitter.failed(handle, error=exc)
        raise
    await emitter.completed(handle, payload=_evaluate_payload(outcome))
    return outcome


__all__ = [
    "DEFAULT_BENCHMARK_PATH",
    "JudgeResponseError",
    "evaluate_engine_result",
    "lookup_reference_answer",
    "run_evaluation",
]
