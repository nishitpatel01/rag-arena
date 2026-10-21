"""Claude on Vertex AI over the partner-model ``:rawPredict`` REST endpoint (google-auth bearer + httpx).

Shared by the Claude judge (``app.evaluation.judges``) and the Claude synthesizer (``app.engines.synthesis``).
Body = Anthropic Messages with ``anthropic_version=vertex-2023-10-16``; the model lives in the URL. Never send
``temperature``: Opus 5.5 on Vertex answers HTTP 400 "`temperature` is deprecated for this model."

Retries are bounded (≤ ``MAX_ATTEMPTS``, exponential backoff) and happen ONLY on 429/500/503/timeouts, inside the
caller's wall-clock budget. Any other status raises immediately; there is no fallback model.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

ANTHROPIC_VERTEX_VERSION = "vertex-2023-10-16"
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 503})
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
_MIN_ATTEMPT_SECONDS = 1.0
_MAX_ERROR_BODY_CHARS = 500
_MAX_CONTENT_BLOCKS = 16


class RawPredictHTTPError(RuntimeError):
    """Non-2xx answer from Vertex ``:rawPredict`` (after bounded retries when the status is retryable)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def raw_predict_url(project_id: str, location: str, model: str) -> str:
    """Partner-model endpoint; the ``global`` location uses the location-less host."""
    for name, value in (("project_id", project_id), ("location", location), ("model", model)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    return f"https://{host}/v1/projects/{project_id}/locations/{location}/publishers/anthropic/models/{model}:rawPredict"


def redact_project(message: str, project_id: str) -> str:
    """Replace the project id with ``${GCP_PROJECT_ID}`` (error bodies echo resource names)."""
    project = project_id.strip()
    return message.replace(project, "${GCP_PROJECT_ID}") if project else message


def message_text(doc: Any) -> str:
    """Concatenate the ``text`` content blocks of an Anthropic Messages response (bounded)."""
    if not isinstance(doc, dict):
        raise ValueError("rawPredict response must be a JSON object")
    blocks = doc.get("content") if isinstance(doc.get("content"), list) else []
    return "".join(
        str(b.get("text", "")) for b in blocks[:_MAX_CONTENT_BLOCKS] if isinstance(b, dict) and b.get("type") == "text"
    )


def message_usage(doc: Any, *, required: bool) -> tuple[int, int]:
    """``(input_tokens, output_tokens)`` from ``usage``; raise when ``required`` and usage is absent."""
    usage = doc.get("usage") if isinstance(doc, dict) else None
    if not isinstance(usage, dict):
        if required:
            raise ValueError("rawPredict response carries no usage; cannot compute measured cost")
        return 0, 0
    return int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)


class AnthropicVertexClient:
    """One Claude model at one location; ``post_messages`` performs the bounded-retry rawPredict call."""

    def __init__(
        self,
        *,
        project_id: str,
        model: str,
        location: str,
        attempt_timeout_seconds: float,
        budget_seconds: float,
        token_provider: Callable[[], str],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        error_cls: type[RawPredictHTTPError] = RawPredictHTTPError,
    ) -> None:
        if attempt_timeout_seconds <= 0 or budget_seconds <= 0:
            raise ValueError("attempt_timeout_seconds and budget_seconds must be > 0")
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        self.project_id = project_id
        self.model = model
        self.location = location
        self.endpoint_url = raw_predict_url(project_id, location, model)
        self._attempt_timeout = float(attempt_timeout_seconds)
        self._budget = float(budget_seconds)
        self._token_provider = token_provider
        self._sleep = sleep
        self._clock = clock
        self._error_cls = error_cls

    def _post_once(self, body: dict[str, Any], timeout: float) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token_provider()}", "Content-Type": "application/json; charset=utf-8"}
        return httpx.post(self.endpoint_url, headers=headers, json=body, timeout=httpx.Timeout(timeout))

    def _http_error(self, response: httpx.Response) -> RawPredictHTTPError:
        text = redact_project(response.text[:_MAX_ERROR_BODY_CHARS], self.project_id)
        return self._error_cls(response.status_code, f"Vertex AI rawPredict ({self.model}) HTTP {response.status_code}: {text}")

    def post_messages(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """POST with ≤ MAX_ATTEMPTS tries; backoff only on 429/500/503/timeouts and only within the budget."""
        if "temperature" in body:
            raise ValueError("temperature must not be sent to Claude on Vertex (deprecated for Opus 5.5)")
        deadline = self._clock() + self._budget
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
                    return response.json(), attempt
                last_error = self._http_error(response)
                if response.status_code not in RETRYABLE_STATUS:
                    raise last_error
            if attempt < MAX_ATTEMPTS:
                self._sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        if last_error is None:
            raise TimeoutError(f"rawPredict budget ({self._budget:.0f} s) exhausted before the first attempt")
        raise last_error


__all__ = [
    "ANTHROPIC_VERTEX_VERSION",
    "AnthropicVertexClient",
    "BACKOFF_BASE_SECONDS",
    "MAX_ATTEMPTS",
    "RETRYABLE_STATUS",
    "RawPredictHTTPError",
    "message_text",
    "message_usage",
    "raw_predict_url",
    "redact_project",
]
