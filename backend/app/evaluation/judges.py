"""LLM-as-a-judge providers (spec §6.5).

The judge must be a different model than the one that synthesized the answer (owner decision 2026-10-21):

* ``AnthropicVertexJudge`` (default): Claude Opus on Vertex AI via the partner-model ``:rawPredict`` REST
  endpoint (Anthropic Messages body, ``anthropic_version=vertex-2023-10-16``) with an ADC bearer token.
  Retries are bounded (≤ 3 attempts, exponential backoff) and happen ONLY on 429/500/503/timeouts, inside the
  run's query budget. Any other failure raises — there is no fallback to another judge.
* ``GeminiJudge``: a structured-output Gemini judge (must still differ from every synthesis model).

Both return a ``JudgeVerdict`` parsed strictly from a single JSON object
``{groundedness, qa_relevance, context_precision, rationale}`` with metrics in [0, 1].
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from typing import Any, Callable, Protocol

import httpx  # noqa: F401 - tests patch ``app.evaluation.judges.httpx.post``; the call lives in core.anthropic_vertex

from app.core.anthropic_vertex import (
    ANTHROPIC_VERTEX_VERSION,
    BACKOFF_BASE_SECONDS,
    MAX_ATTEMPTS,
    RETRYABLE_STATUS,
    AnthropicVertexClient,
    RawPredictHTTPError,
    message_text,
    message_usage,
    redact_project,
)
from app.core.config import ArenaSettings
from app.core.gcp_clients import get_access_token, get_genai_client
from app.core.models import EvalMetrics

METRIC_KEYS: tuple[str, ...] = ("groundedness", "qa_relevance", "context_precision")
JUDGE_KEYS: tuple[str, ...] = (*METRIC_KEYS, "rationale")
MAX_RATIONALE_CHARS = 600
_MAX_RESPONSE_TEXT_CHARS = 20_000
_MAX_JSON_CANDIDATES = 50
_CLAUDE_ID_RE = re.compile(r"claude-([a-z]+)-(\d+)(?:-(\d+))?")
_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {**{k: {"type": "number"} for k in METRIC_KEYS}, "rationale": {"type": "string"}},
    "required": list(JUDGE_KEYS),
}
# Catalog provider ids (app.core.models_catalog); the legacy JUDGE_PROVIDER value "gemini" maps to "vertex_gemini".
PROVIDER_ANTHROPIC_VERTEX = "anthropic_vertex"
PROVIDER_VERTEX_GEMINI = "vertex_gemini"


class JudgeResponseError(ValueError):
    """Raised when the judge returns something that is not a single valid metric object."""


class JudgeHTTPError(RawPredictHTTPError):
    """Raised when the judge endpoint answers with a non-2xx status (after bounded retries if retryable)."""


@dataclass(frozen=True)
class JudgeVerdict:
    """One judge call: strict metrics + rationale + measured usage and provenance."""

    metrics: EvalMetrics
    rationale: str
    input_tokens: int
    output_tokens: int
    response_id: str
    attempts: int


class Judge(Protocol):
    """Common interface for every judge provider."""

    provider: str
    model: str
    location: str

    def score(self, system_prompt: str, user_prompt: str) -> JudgeVerdict: ...


# ---------------------------------------------------------------- strict parsing


def _first_json_object(text: str) -> dict[str, Any]:
    """Return the first decodable JSON object in ``text`` (tolerates ```json fences and surrounding prose)."""
    decoder = json.JSONDecoder()
    start = text.find("{")
    for _ in range(_MAX_JSON_CANDIDATES):
        if start < 0:
            break
        try:
            parsed, _end = decoder.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(parsed, dict):
            return parsed
        start = text.find("{", start + 1)
    raise JudgeResponseError(f"Judge output contains no JSON object: {text[:200]!r}")


def _metric(parsed: dict[str, Any], key: str) -> float:
    value = parsed.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise JudgeResponseError(f"Judge metric '{key}' must be a number, got {value!r}")
    if not (0.0 <= float(value) <= 1.0):
        raise JudgeResponseError(f"Judge metric '{key}' must be within [0, 1], got {value!r}")
    return round(float(value), 3)


def parse_judge_json(text: str) -> tuple[EvalMetrics, str]:
    """Strictly parse ``{groundedness, qa_relevance, context_precision, rationale}``; raise ``JudgeResponseError``."""
    if not isinstance(text, str) or not text.strip():
        raise JudgeResponseError("Judge returned empty output")
    parsed = _first_json_object(text[:_MAX_RESPONSE_TEXT_CHARS])
    missing = [k for k in JUDGE_KEYS if k not in parsed]
    if missing:
        raise JudgeResponseError(f"Judge JSON is missing keys {missing}")
    rationale = parsed["rationale"]
    if not isinstance(rationale, str) or not rationale.strip():
        raise JudgeResponseError("Judge 'rationale' must be a non-empty string")
    metrics = EvalMetrics(**{k: _metric(parsed, k) for k in METRIC_KEYS})
    return metrics, rationale.strip()[:MAX_RATIONALE_CHARS]


# ---------------------------------------------------------------- labels


def judge_display_name(model: str) -> str:
    """Human label for a judge model id, e.g. ``claude-opus-5-5`` → ``Claude Opus 5.5`` (other ids unchanged)."""
    match = _CLAUDE_ID_RE.fullmatch(model.strip())
    if not match:
        return model.strip()
    family, major, minor = match.groups()
    version = f"{major}.{minor}" if minor else major
    return f"Claude {family.capitalize()} {version}"


def judge_service_label(settings: ArenaSettings) -> str:
    """Stage ``service`` label for the evaluate stage, built from settings (never a hardcoded model string)."""
    return f"Vertex AI · {judge_display_name(settings.judge_model)} (judge)"


def judge_service_label_for(label: str) -> str:
    """Per-run ``evaluate`` stage service label for a selected catalog model label (e.g. ``Claude Opus 5.5``)."""
    if not isinstance(label, str) or not label.strip():
        raise ValueError("label must be a non-empty string")
    return f"Vertex AI · {label.strip()} (judge)"


def _redact(message: str, settings: ArenaSettings) -> str:
    return redact_project(message, settings.gcp_project_id)


# ---------------------------------------------------------------- Claude on Vertex AI (rawPredict)


class AnthropicVertexJudge:
    """Claude judge over Vertex AI ``publishers/anthropic/models/<model>:rawPredict`` (google-auth + httpx).

    ``model``/``location`` select a catalog model per run; when omitted they come from ``JUDGE_MODEL`` /
    ``JUDGE_LOCATION`` (and ``JUDGE_PROVIDER`` must then be ``anthropic_vertex``).
    """

    provider = PROVIDER_ANTHROPIC_VERTEX

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
        if model is None and settings.judge_provider != self.provider:
            raise ValueError(f"AnthropicVertexJudge requires JUDGE_PROVIDER={self.provider}")
        self._settings = settings
        self.model = model or settings.judge_model
        self.location = location or settings.judge_location
        self._client = AnthropicVertexClient(
            project_id=settings.gcp_project_id,
            model=self.model,
            location=self.location,
            attempt_timeout_seconds=float(settings.judge_timeout_seconds),
            budget_seconds=float(settings.query_timeout_seconds),
            token_provider=token_provider or (lambda: get_access_token(settings)),
            sleep=sleep,
            clock=clock,
            error_cls=JudgeHTTPError,
        )

    @property
    def endpoint_url(self) -> str:
        return self._client.endpoint_url

    def _body(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        # No ``temperature``: Opus 5.5 on Vertex answers HTTP 400 "`temperature` is deprecated for this model."
        return {
            "anthropic_version": ANTHROPIC_VERTEX_VERSION,
            "max_tokens": self._settings.judge_max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }

    def score(self, system_prompt: str, user_prompt: str) -> JudgeVerdict:
        """Call Claude once (bounded retries), parse the strict JSON verdict and capture ``usage`` + ``id``."""
        if not system_prompt.strip() or not user_prompt.strip():
            raise ValueError("system_prompt and user_prompt must be non-empty")
        doc, attempts = self._client.post_messages(self._body(system_prompt, user_prompt))
        if not isinstance(doc, dict):
            raise JudgeResponseError("rawPredict response must be a JSON object")
        metrics, rationale = parse_judge_json(message_text(doc))
        input_tokens, output_tokens = message_usage(doc, required=False)
        return JudgeVerdict(
            metrics=metrics,
            rationale=rationale,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            response_id=str(doc.get("id", "")),
            attempts=attempts,
        )


# ---------------------------------------------------------------- Gemini (distinct model) judge


class GeminiJudge:
    """Structured-output Gemini judge (``temperature=0``, ``thinking_level=LOW``) behind the same interface.

    ``model``/``location`` select a catalog model per run; when omitted they come from ``JUDGE_MODEL`` /
    ``VERTEX_GENAI_LOCATION`` (and ``JUDGE_PROVIDER`` must then be ``gemini``).
    """

    provider = PROVIDER_VERTEX_GEMINI

    def __init__(self, settings: ArenaSettings, *, model: str | None = None, location: str | None = None) -> None:
        if not isinstance(settings, ArenaSettings):
            raise TypeError("settings must be an ArenaSettings instance")
        if model is None and settings.judge_provider != "gemini":
            raise ValueError("GeminiJudge requires JUDGE_PROVIDER=gemini")
        self._settings = settings
        self.model = model or settings.judge_model
        self.location = location or settings.vertex_genai_location

    def score(self, system_prompt: str, user_prompt: str) -> JudgeVerdict:
        if not system_prompt.strip() or not user_prompt.strip():
            raise ValueError("system_prompt and user_prompt must be non-empty")
        response = get_genai_client(self._settings, location=self.location).models.generate_content(
            model=self.model,
            contents=user_prompt,
            config={
                "system_instruction": system_prompt,
                "temperature": 0.0,
                "max_output_tokens": self._settings.judge_max_tokens,
                "response_mime_type": "application/json",
                "response_schema": _GEMINI_SCHEMA,
                "thinking_config": {"thinking_level": "LOW"},
            },
        )
        metrics, rationale = parse_judge_json(str(getattr(response, "text", "") or ""))
        meta = getattr(response, "usage_metadata", None)
        output = int(getattr(meta, "candidates_token_count", 0) or 0) + int(getattr(meta, "thoughts_token_count", 0) or 0)
        return JudgeVerdict(
            metrics=metrics,
            rationale=rationale,
            input_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
            output_tokens=output,
            response_id=str(getattr(response, "response_id", "") or ""),
            attempts=1,
        )


def build_judge(settings: ArenaSettings, spec: Any | None = None) -> Judge:
    """Return the judge for the selected catalog ``spec`` (a ``ModelSpec``), else the ``JUDGE_*`` settings judge.

    Provider-agnostic both ways: a Gemini judge may grade a Claude answer and vice versa. No fallback judge.
    """
    if not isinstance(settings, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance")
    if spec is not None:
        provider, model, location = getattr(spec, "provider", ""), getattr(spec, "id", ""), getattr(spec, "location", "")
        if provider == PROVIDER_ANTHROPIC_VERTEX:
            return AnthropicVertexJudge(settings, model=model, location=location)
        if provider == PROVIDER_VERTEX_GEMINI:
            return GeminiJudge(settings, model=model, location=location)
        raise ValueError(f"Unsupported judge provider {provider!r} for model {model!r}")
    if settings.judge_provider == AnthropicVertexJudge.provider:
        return AnthropicVertexJudge(settings)
    if settings.judge_provider == "gemini":
        return GeminiJudge(settings)
    raise ValueError(f"Unsupported JUDGE_PROVIDER {settings.judge_provider!r}")


__all__ = [
    "ANTHROPIC_VERTEX_VERSION",
    "AnthropicVertexJudge",
    "BACKOFF_BASE_SECONDS",
    "GeminiJudge",
    "Judge",
    "JudgeHTTPError",
    "JudgeResponseError",
    "JudgeVerdict",
    "MAX_ATTEMPTS",
    "PROVIDER_ANTHROPIC_VERTEX",
    "PROVIDER_VERTEX_GEMINI",
    "RETRYABLE_STATUS",
    "build_judge",
    "judge_display_name",
    "judge_service_label",
    "judge_service_label_for",
    "parse_judge_json",
]
