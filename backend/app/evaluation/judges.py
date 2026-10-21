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

import httpx

from app.core.config import ArenaSettings
from app.core.gcp_clients import get_access_token, get_genai_client
from app.core.models import EvalMetrics

ANTHROPIC_VERTEX_VERSION = "vertex-2023-10-16"
METRIC_KEYS: tuple[str, ...] = ("groundedness", "qa_relevance", "context_precision")
JUDGE_KEYS: tuple[str, ...] = (*METRIC_KEYS, "rationale")
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 503})
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
MAX_RATIONALE_CHARS = 600
_MAX_RESPONSE_TEXT_CHARS = 20_000
_MAX_JSON_CANDIDATES = 50
_MAX_ERROR_BODY_CHARS = 500
_MIN_ATTEMPT_SECONDS = 1.0
_CLAUDE_ID_RE = re.compile(r"claude-([a-z]+)-(\d+)(?:-(\d+))?")
_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {**{k: {"type": "number"} for k in METRIC_KEYS}, "rationale": {"type": "string"}},
    "required": list(JUDGE_KEYS),
}


class JudgeResponseError(ValueError):
    """Raised when the judge returns something that is not a single valid metric object."""


class JudgeHTTPError(RuntimeError):
    """Raised when the judge endpoint answers with a non-2xx status (after bounded retries if retryable)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


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


def _redact(message: str, settings: ArenaSettings) -> str:
    project = settings.gcp_project_id.strip()
    return message.replace(project, "${GCP_PROJECT_ID}") if project else message


# ---------------------------------------------------------------- Claude on Vertex AI (rawPredict)


class AnthropicVertexJudge:
    """Claude judge over Vertex AI ``publishers/anthropic/models/<model>:rawPredict`` (google-auth + httpx)."""

    provider = "anthropic_vertex"

    def __init__(
        self,
        settings: ArenaSettings,
        *,
        token_provider: Callable[[], str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(settings, ArenaSettings):
            raise TypeError("settings must be an ArenaSettings instance")
        if settings.judge_provider != self.provider:
            raise ValueError(f"AnthropicVertexJudge requires JUDGE_PROVIDER={self.provider}")
        self._settings = settings
        self.model = settings.judge_model
        self.location = settings.judge_location
        self._token_provider = token_provider or (lambda: get_access_token(settings))
        self._sleep = sleep
        self._clock = clock

    @property
    def endpoint_url(self) -> str:
        host = "aiplatform.googleapis.com" if self.location == "global" else f"{self.location}-aiplatform.googleapis.com"
        return (
            f"https://{host}/v1/projects/{self._settings.gcp_project_id}/locations/{self.location}"
            f"/publishers/anthropic/models/{self.model}:rawPredict"
        )

    def _body(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        # No ``temperature``: Opus 5.5 on Vertex answers HTTP 400 "`temperature` is deprecated for this model."
        return {
            "anthropic_version": ANTHROPIC_VERTEX_VERSION,
            "max_tokens": self._settings.judge_max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }

    def _post_once(self, body: dict[str, Any], timeout: float) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token_provider()}", "Content-Type": "application/json; charset=utf-8"}
        return httpx.post(self.endpoint_url, headers=headers, json=body, timeout=httpx.Timeout(timeout))

    def _http_error(self, response: httpx.Response) -> JudgeHTTPError:
        body = _redact(response.text[:_MAX_ERROR_BODY_CHARS], self._settings)
        return JudgeHTTPError(response.status_code, f"Vertex AI rawPredict ({self.model}) HTTP {response.status_code}: {body}")

    def _post_with_retry(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """POST with ≤ MAX_ATTEMPTS tries; backoff only on 429/500/503/timeouts and only within the run budget."""
        deadline = self._clock() + float(self._settings.query_timeout_seconds)
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = deadline - self._clock()
            if remaining < _MIN_ATTEMPT_SECONDS:
                break
            try:
                response = self._post_once(body, min(float(self._settings.judge_timeout_seconds), remaining))
            except httpx.TimeoutException as exc:
                last_error = exc
            else:
                if response.status_code == 200:
                    return response.json(), attempt
                last_error = self._http_error(response)
                if response.status_code not in RETRYABLE_STATUS:
                    raise last_error
            if attempt < MAX_ATTEMPTS:
                self._sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        if last_error is None:
            raise TimeoutError(f"Judge budget (QUERY_TIMEOUT_SECONDS={self._settings.query_timeout_seconds}) exhausted")
        raise last_error

    def score(self, system_prompt: str, user_prompt: str) -> JudgeVerdict:
        """Call Claude once (bounded retries), parse the strict JSON verdict and capture ``usage`` + ``id``."""
        if not system_prompt.strip() or not user_prompt.strip():
            raise ValueError("system_prompt and user_prompt must be non-empty")
        doc, attempts = self._post_with_retry(self._body(system_prompt, user_prompt))
        if not isinstance(doc, dict):
            raise JudgeResponseError("rawPredict response must be a JSON object")
        blocks = doc.get("content") if isinstance(doc.get("content"), list) else []
        text = "".join(str(b.get("text", "")) for b in blocks[:16] if isinstance(b, dict) and b.get("type") == "text")
        metrics, rationale = parse_judge_json(text)
        usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
        return JudgeVerdict(
            metrics=metrics,
            rationale=rationale,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            response_id=str(doc.get("id", "")),
            attempts=attempts,
        )


# ---------------------------------------------------------------- Gemini (distinct model) judge


class GeminiJudge:
    """Structured-output Gemini judge (``temperature=0``, ``thinking_level=LOW``) behind the same interface."""

    provider = "gemini"

    def __init__(self, settings: ArenaSettings) -> None:
        if not isinstance(settings, ArenaSettings):
            raise TypeError("settings must be an ArenaSettings instance")
        if settings.judge_provider != self.provider:
            raise ValueError(f"GeminiJudge requires JUDGE_PROVIDER={self.provider}")
        self._settings = settings
        self.model = settings.judge_model
        self.location = settings.vertex_genai_location

    def score(self, system_prompt: str, user_prompt: str) -> JudgeVerdict:
        if not system_prompt.strip() or not user_prompt.strip():
            raise ValueError("system_prompt and user_prompt must be non-empty")
        response = get_genai_client(self._settings).models.generate_content(
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


def build_judge(settings: ArenaSettings) -> Judge:
    """Return the configured judge (``JUDGE_PROVIDER``); unknown providers are rejected by ``ArenaSettings``."""
    if not isinstance(settings, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance")
    if settings.judge_provider == AnthropicVertexJudge.provider:
        return AnthropicVertexJudge(settings)
    if settings.judge_provider == GeminiJudge.provider:
        return GeminiJudge(settings)
    raise ValueError(f"Unsupported JUDGE_PROVIDER {settings.judge_provider!r}")


__all__ = [
    "ANTHROPIC_VERTEX_VERSION",
    "AnthropicVertexJudge",
    "GeminiJudge",
    "Judge",
    "JudgeHTTPError",
    "JudgeResponseError",
    "JudgeVerdict",
    "MAX_ATTEMPTS",
    "RETRYABLE_STATUS",
    "build_judge",
    "judge_display_name",
    "judge_service_label",
    "parse_judge_json",
]
