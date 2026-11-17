"""Vertex AI GenAI Evaluation Service (:evaluateInstances) client and judge adapter.

Calls the official regional Vertex AI REST endpoint:
``https://{location}-aiplatform.googleapis.com/v1beta1/projects/{project}/locations/{location}:evaluateInstances``
across the three managed RAG evaluation rubrics:
1. ``GROUNDEDNESS`` (``groundednessInput`` -> ``groundednessResult``, [0, 1]) -> ``groundedness``
2. ``QUESTION_ANSWERING_QUALITY`` (``questionAnsweringQualityInput`` -> ``questionAnsweringQualityResult``, Likert 1–5) -> ``qa_relevance``
3. ``FULFILLMENT`` (``fulfillmentInput`` -> ``fulfillmentResult``, Likert 1–5 over retrieved evidence) -> ``context_precision``

Billed at the underlying Vertex AI Gemini Flash autorater token rate ($0 platform surcharge).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
from typing import Any, Callable

import httpx

from app.core.anthropic_vertex import (
    BACKOFF_BASE_SECONDS,
    MAX_ATTEMPTS,
    RETRYABLE_STATUS,
    RawPredictHTTPError,
    redact_project,
)
from app.core.config import ArenaSettings
from app.core.gcp_clients import get_access_token
from app.core.models import EvalMetrics
from app.core.models_catalog import (
    PROVIDER_VERTEX_GENAI_EVAL,
    VERTEX_GENAI_EVAL_MODEL_ID,
    location_env_suffix,
)
from app.core.pricing import ModelPrice, PRICE_TABLE
from app.recording.redaction import PROJECT_NUMBER_PLACEHOLDER

MAX_RATIONALE_CHARS = 600
EVAL_METRIC_NAMES: tuple[str, ...] = (
    "GROUNDEDNESS",
    "QUESTION_ANSWERING_QUALITY",
    "FULFILLMENT",
)
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PROJECT_NUMBER_RE = re.compile(r"projects/\d{6,}")
_MIN_ATTEMPT_SECONDS = 1.0
_MAX_ERROR_BODY_CHARS = 500
_EMPTY_CONTEXT_PLACEHOLDER = "[no retrieved context]"
_PROMPT_PARSE_RE = re.compile(
    r"Query:\s*(?P<query>.*?)\n"
    r"Reference Answer:\s*(?P<reference>.*?)\n"
    r"Retrieved Context:\n<untrusted_retrieved_context>\n(?P<context>.*?)\n</untrusted_retrieved_context>\n"
    r"Candidate Answer:\n<untrusted_candidate_answer>\n(?P<answer>.*?)\n</untrusted_candidate_answer>",
    re.DOTALL,
)


class JudgeResponseError(ValueError):
    """Raised when the judge or evaluation service returns malformed or out-of-range metric output."""


class JudgeHTTPError(RawPredictHTTPError):
    """Raised when the judge or evaluation endpoint answers with a non-2xx status (after bounded retries)."""


@dataclass(frozen=True)
class JudgeVerdict:
    """One judge call: strict metrics + rationale + measured usage and provenance."""

    metrics: EvalMetrics
    rationale: str
    input_tokens: int
    output_tokens: int
    response_id: str
    attempts: int
    cost_usd: float | None = None


@dataclass(frozen=True)
class VertexEvalResult:
    """Structured result from Vertex AI GenAI Evaluation Service (:evaluateInstances)."""

    metrics: EvalMetrics
    groundedness: float
    qa_relevance: float
    context_precision: float
    rationale: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    eval_cost_usd: float
    metric_scores: dict[str, float]
    attempts: int
    response_id: str = ""

    def to_verdict(self) -> JudgeVerdict:
        return JudgeVerdict(
            metrics=self.metrics,
            rationale=self.rationale[:MAX_RATIONALE_CHARS],
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            response_id=self.response_id,
            attempts=self.attempts,
            cost_usd=self.eval_cost_usd,
        )


# Explicit alias requested by Slice #4 contract.
JudgeResult = VertexEvalResult


def evaluate_instances_url(project_id: str, location: str) -> str:
    """Build the regional (or global) Vertex AI v1beta1 ``:evaluateInstances`` REST URL."""
    for name, value in (("project_id", project_id), ("location", location)):
        if not isinstance(value, str) or not value.strip() or not _SAFE_SEGMENT_RE.fullmatch(value.strip()):
            raise ValueError(f"{name} must be a non-empty safe GCP identifier, got {value!r}")
    proj = project_id.strip()
    loc = location.strip()
    host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
    return f"https://{host}/v1beta1/projects/{proj}/locations/{loc}:evaluateInstances"


def redact_eval_error(message: str, project_id: str) -> str:
    """Redact both ``project_id`` and 6+ digit ``projects/<number>`` resource identifiers."""
    redacted = redact_project(message, project_id)
    return _PROJECT_NUMBER_RE.sub(PROJECT_NUMBER_PLACEHOLDER, redacted)


def normalize_groundedness_score(raw_score: Any) -> float:
    """Validate and round a ``GROUNDEDNESS`` score in [0.0, 1.0]."""
    if not isinstance(raw_score, (int, float)) or isinstance(raw_score, bool):
        raise JudgeResponseError(f"GROUNDEDNESS score must be numeric, got {raw_score!r}")
    val = float(raw_score)
    if not (0.0 <= val <= 1.0):
        raise JudgeResponseError(f"GROUNDEDNESS score must be in [0.0, 1.0], got {val}")
    return round(val, 3)


def normalize_likert_score(raw_score: Any, metric_name: str) -> float:
    """Normalize a Vertex AI Likert [1.0, 5.0] rubric score linearly onto [0.0, 1.0]."""
    if not isinstance(raw_score, (int, float)) or isinstance(raw_score, bool):
        raise JudgeResponseError(f"{metric_name} score must be numeric, got {raw_score!r}")
    val = float(raw_score)
    if not (1.0 <= val <= 5.0):
        raise JudgeResponseError(f"{metric_name} score must be in Likert [1.0, 5.0], got {val}")
    return round((val - 1.0) / 4.0, 3)


def build_evaluate_instance_requests(
    *,
    query: str,
    context: str,
    candidate_answer: str,
    reference_answer: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Build the three ``EvaluateInstancesRequest`` JSON bodies (one per ``oneof`` metric input)."""
    q = query.strip()
    ans = candidate_answer.strip()
    if not q:
        raise ValueError("query must be non-empty")
    if not ans:
        raise ValueError("candidate_answer must be non-empty")
    ctx = context.strip() or _EMPTY_CONTEXT_PLACEHOLDER
    ref = reference_answer.strip() if isinstance(reference_answer, str) and reference_answer.strip() else None
    has_ref = ref is not None

    qa_instance: dict[str, Any] = {
        "instruction": q,
        "context": ctx,
        "prediction": ans,
    }
    if has_ref:
        qa_instance["reference"] = ref

    retrieval_instruction = (
        "Provide focused, directly relevant source excerpts without extraneous off-topic noise "
        f"required to answer the user query: {q}"
    )
    return {
        "GROUNDEDNESS": {
            "groundednessInput": {
                "metricSpec": {},
                "instance": {
                    "prediction": ans,
                    "context": ctx,
                },
            }
        },
        "QUESTION_ANSWERING_QUALITY": {
            "questionAnsweringQualityInput": {
                "metricSpec": {"useReference": has_ref},
                "instance": qa_instance,
            }
        },
        "FULFILLMENT": {
            "fulfillmentInput": {
                "metricSpec": {},
                "instance": {
                    "instruction": retrieval_instruction,
                    "prediction": ctx,
                },
            }
        },
    }


def _estimate_tokens(text: str) -> int:
    cleaned = text.strip()
    if not cleaned:
        return 0
    return max(1, math.ceil(len(cleaned) / 4))


def _extract_usage_tokens(doc: dict[str, Any], request_body: dict[str, Any], explanation: str) -> tuple[int, int]:
    usage = doc.get("usageMetadata") or doc.get("usage_metadata")
    if isinstance(usage, dict):
        inp = int(usage.get("promptTokenCount") or usage.get("input_tokens") or 0)
        out = int(usage.get("candidatesTokenCount") or usage.get("output_tokens") or 0) + int(
            usage.get("thoughtsTokenCount") or 0
        )
        if inp > 0 or out > 0:
            return max(0, inp), max(0, out)
    # Vertex v1beta1 :evaluateInstances often omits usageMetadata; compute deterministic autorater token footprint.
    return _estimate_tokens(str(request_body)), _estimate_tokens(explanation)


def _extract_result_block(doc: Any, camel_key: str, snake_key: str, metric_name: str) -> tuple[float, str]:
    if not isinstance(doc, dict):
        raise JudgeResponseError(f"evaluateInstances ({metric_name}) response must be a JSON object")
    block = doc.get(camel_key) if camel_key in doc else doc.get(snake_key)
    if not isinstance(block, dict):
        raise JudgeResponseError(f"evaluateInstances ({metric_name}) missing '{camel_key}' object")
    raw_score = block.get("score")
    score = (
        normalize_groundedness_score(raw_score)
        if metric_name == "GROUNDEDNESS"
        else normalize_likert_score(raw_score, metric_name)
    )
    explanation = str(block.get("explanation") or "").strip()
    return score, explanation


class VertexEvalClient:
    """Bounded-retry REST client for Vertex AI GenAI Evaluation Service (``:evaluateInstances``)."""

    def __init__(
        self,
        *,
        project_id: str,
        location: str,
        attempt_timeout_seconds: float,
        budget_seconds: float,
        token_provider: Callable[[], str],
        autorater_price: ModelPrice | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if attempt_timeout_seconds <= 0 or budget_seconds <= 0:
            raise ValueError("attempt_timeout_seconds and budget_seconds must be > 0")
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        self.project_id = project_id.strip()
        self.location = location.strip()
        self.endpoint_url = evaluate_instances_url(self.project_id, self.location)
        self._attempt_timeout = float(attempt_timeout_seconds)
        self._budget = float(budget_seconds)
        self._token_provider = token_provider
        self._price = autorater_price or PRICE_TABLE["gemini-3.8-flash"]
        self._sleep = sleep
        self._clock = clock

    def _post_once(self, body: dict[str, Any], timeout: float) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._token_provider()}",
            "Content-Type": "application/json; charset=utf-8",
        }
        return httpx.post(self.endpoint_url, headers=headers, json=body, timeout=httpx.Timeout(timeout))

    def _post_with_retries(self, metric_name: str, body: dict[str, Any], deadline: float) -> tuple[dict[str, Any], int]:
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = deadline - self._clock()
            if remaining < _MIN_ATTEMPT_SECONDS:
                break
            try:
                response = self._post_once(body, min(self._attempt_timeout, remaining))
            except httpx.TimeoutException as exc:
                last_error = exc
            else:
                if response.status_code == 200:
                    try:
                        parsed = response.json()
                    except ValueError as exc:
                        raise JudgeResponseError(f"evaluateInstances ({metric_name}) returned invalid JSON") from exc
                    if not isinstance(parsed, dict):
                        raise JudgeResponseError(f"evaluateInstances ({metric_name}) returned non-object JSON")
                    return parsed, attempt
                redacted = redact_eval_error(response.text[:_MAX_ERROR_BODY_CHARS], self.project_id)
                last_error = JudgeHTTPError(
                    response.status_code,
                    f"Vertex AI evaluateInstances ({metric_name}) HTTP {response.status_code}: {redacted}",
                )
                if response.status_code not in RETRYABLE_STATUS:
                    raise last_error
            if attempt < MAX_ATTEMPTS:
                self._sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        if last_error is None:
            raise TimeoutError(f"evaluateInstances budget ({self._budget:.0f} s) exhausted before completing {metric_name}")
        raise last_error

    def evaluate(
        self,
        *,
        query: str,
        context: str,
        candidate_answer: str,
        reference_answer: str | None = None,
    ) -> VertexEvalResult:
        """Execute ``GROUNDEDNESS``, ``QUESTION_ANSWERING_QUALITY``, and ``FULFILLMENT`` via ``:evaluateInstances``."""
        t0 = time.perf_counter()
        deadline = self._clock() + self._budget
        requests = build_evaluate_instance_requests(
            query=query,
            context=context,
            candidate_answer=candidate_answer,
            reference_answer=reference_answer,
        )
        key_map = {
            "GROUNDEDNESS": ("groundednessResult", "groundedness_result", "Groundedness"),
            "QUESTION_ANSWERING_QUALITY": (
                "questionAnsweringQualityResult",
                "question_answering_quality_result",
                "QA Quality",
            ),
            "FULFILLMENT": ("fulfillmentResult", "fulfillment_result", "Context Fulfillment"),
        }
        scores: dict[str, float] = {}
        explanations: list[str] = []
        total_in = 0
        total_out = 0
        total_attempts = 0
        for metric_name in EVAL_METRIC_NAMES:
            body = requests[metric_name]
            doc, attempts = self._post_with_retries(metric_name, body, deadline)
            total_attempts += attempts
            camel_key, snake_key, label = key_map[metric_name]
            norm_score, explanation = _extract_result_block(doc, camel_key, snake_key, metric_name)
            scores[metric_name] = norm_score
            if explanation:
                explanations.append(f"[{label}] {explanation}")
            inp, out = _extract_usage_tokens(doc, body, explanation)
            total_in += inp
            total_out += out

        if not explanations:
            raise JudgeResponseError("evaluateInstances returned empty explanations across all metrics")
        rationale = " ".join(explanations)[:MAX_RATIONALE_CHARS]
        metrics = EvalMetrics(
            groundedness=scores["GROUNDEDNESS"],
            qa_relevance=scores["QUESTION_ANSWERING_QUALITY"],
            context_precision=scores["FULFILLMENT"],
        )
        eval_cost = round(
            (total_in / 1_000_000.0) * self._price.input_usd_per_1m
            + (total_out / 1_000_000.0) * self._price.output_usd_per_1m,
            8,
        )
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        return VertexEvalResult(
            metrics=metrics,
            groundedness=metrics.groundedness,
            qa_relevance=metrics.qa_relevance,
            context_precision=metrics.context_precision,
            rationale=rationale,
            latency_ms=latency_ms,
            input_tokens=total_in,
            output_tokens=total_out,
            eval_cost_usd=eval_cost,
            metric_scores=scores,
            attempts=total_attempts,
            response_id=f"vertex-eval-{self.location}",
        )


def parse_case_from_judge_prompts(system_prompt: str, user_prompt: str) -> tuple[str, str, str, str | None]:
    """Extract ``(query, context, candidate_answer, reference_answer)`` from ``build_judge_user_prompt`` output."""
    match = _PROMPT_PARSE_RE.search(user_prompt)
    if match:
        q = match.group("query").strip()
        ref_raw = match.group("reference").strip()
        ref = None if not ref_raw or ref_raw == "N/A" else ref_raw
        ctx = match.group("context").strip()
        ans = match.group("answer").strip()
        return q, ctx, ans, ref
    return system_prompt.strip(), "", user_prompt.strip(), None


class VertexGenAIEvalJudge:
    """``Judge`` adapter backed by Vertex AI GenAI Evaluation Service (``:evaluateInstances``)."""

    provider = PROVIDER_VERTEX_GENAI_EVAL

    def __init__(
        self,
        settings: ArenaSettings,
        *,
        model: str | None = None,
        location: str | None = None,
        token_provider: Callable[[], str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(settings, ArenaSettings):
            raise TypeError("settings must be an ArenaSettings instance")
        self._settings = settings
        self.model = model or VERTEX_GENAI_EVAL_MODEL_ID
        locations = dict(settings.model_locations)
        default_loc = locations.get(location_env_suffix(self.model), settings.gcp_location or "us-central1")
        self.location = location or default_loc
        autorater_price = PRICE_TABLE.get("gemini-3.8-flash") or PRICE_TABLE.get(settings.gemini_fast_model)
        self._client = VertexEvalClient(
            project_id=settings.gcp_project_id,
            location=self.location,
            attempt_timeout_seconds=float(settings.judge_timeout_seconds),
            budget_seconds=float(settings.query_timeout_seconds),
            token_provider=token_provider or (lambda: get_access_token(settings)),
            autorater_price=autorater_price,
            sleep=sleep,
            clock=clock,
        )

    @property
    def endpoint_url(self) -> str:
        return self._client.endpoint_url

    def score_case(
        self,
        *,
        query: str,
        context: str,
        candidate_answer: str,
        reference_answer: str | None = None,
    ) -> JudgeVerdict:
        """Structured entrypoint used by ``evaluator.evaluate_engine_result``."""
        return self._client.evaluate(
            query=query,
            context=context,
            candidate_answer=candidate_answer,
            reference_answer=reference_answer,
        ).to_verdict()

    def score(self, system_prompt: str, user_prompt: str) -> JudgeVerdict:
        """``Judge`` protocol entrypoint (parses structured prompt fields or evaluates raw prompt/response)."""
        if not system_prompt.strip() or not user_prompt.strip():
            raise ValueError("system_prompt and user_prompt must be non-empty")
        query, context, answer, reference = parse_case_from_judge_prompts(system_prompt, user_prompt)
        return self.score_case(
            query=query,
            context=context,
            candidate_answer=answer,
            reference_answer=reference,
        )


__all__ = [
    "EVAL_METRIC_NAMES",
    "JudgeHTTPError",
    "JudgeResponseError",
    "JudgeResult",
    "JudgeVerdict",
    "MAX_RATIONALE_CHARS",
    "PROVIDER_VERTEX_GENAI_EVAL",
    "VertexEvalClient",
    "VertexEvalResult",
    "VertexGenAIEvalJudge",
    "build_evaluate_instance_requests",
    "evaluate_instances_url",
    "normalize_groundedness_score",
    "normalize_likert_score",
    "parse_case_from_judge_prompts",
    "redact_eval_error",
]
