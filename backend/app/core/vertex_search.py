"""Discovery Engine (Vertex AI Search) REST v1 client: google-auth bearer + httpx, no discoveryengine SDK.

Used by Level 5 (``managed_vertex``) for provisioning (``scripts/provision_vertex_search.py``), ingestion
(``documents:import`` LRO, ``documents.list``) and querying (``servingConfigs/default_search:search``).

* Host: ``https://discoveryengine.googleapis.com`` for ``global``, ``https://<loc>-discoveryengine.googleapis.com``
  otherwise; header ``x-goog-user-project: <project>`` (required with user ADC credentials).
* Retries are bounded (≤ ``MAX_ATTEMPTS``) and happen ONLY on 429/500/503/timeouts inside the caller's budget; any
  other status raises ``DiscoveryEngineHTTPError`` immediately with the (redacted) response body. No fallback.
* Long-running operations are polled with a bounded number of polls and a wall-clock deadline (``poll_operation``).
* Every error message and every payload path built here is redacted: project id → ``${GCP_PROJECT_ID}``,
  ``projects/<digits>`` → ``projects/${GCP_PROJECT_NUMBER}``.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any, Callable

import httpx

COLLECTION = "default_collection"
DEFAULT_BRANCH = "default_branch"
SERVING_CONFIG = "default_search"
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 503})
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
MAX_OPERATION_POLLS = 400
_MIN_ATTEMPT_SECONDS = 0.5
_MAX_ERROR_BODY_CHARS = 1500
_PROJECT_NUMBER_RE = re.compile(r"projects/\d+")
_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


class DiscoveryEngineHTTPError(RuntimeError):
    """Non-2xx answer from Discovery Engine (after bounded retries when the status is retryable)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class OperationFailedError(RuntimeError):
    """A long-running operation finished with ``error`` (message verbatim, redacted)."""


class OperationTimeoutError(TimeoutError):
    """A long-running operation did not finish within the poll bound / deadline."""


def redact_resource_text(text: str, project_id: str) -> str:
    """Replace the project id and any ``projects/<number>`` with placeholders (resource names echo both)."""
    out = str(text)
    project = (project_id or "").strip()
    if project:
        out = out.replace(project, "${GCP_PROJECT_ID}")
    return _PROJECT_NUMBER_RE.sub("projects/${GCP_PROJECT_NUMBER}", out)


def discovery_host(location: str) -> str:
    if not isinstance(location, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", location):
        raise ValueError(f"invalid Discovery Engine location: {location!r}")
    return "https://discoveryengine.googleapis.com" if location == "global" else f"https://{location}-discoveryengine.googleapis.com"


def _short_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value.strip()):
        raise ValueError(f"{name} must be a short Discovery Engine id, got {value!r}")
    return value.strip()


@dataclass(frozen=True)
class VaisResources:
    """Our Discovery Engine resources (short ids only; full names are built, never stored or printed)."""

    project_id: str
    location: str
    data_store_id: str
    engine_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        discovery_host(self.location)
        if self.data_store_id:
            _short_id(self.data_store_id, "data_store_id")
        if self.engine_id:
            _short_id(self.engine_id, "engine_id")

    @property
    def collection_path(self) -> str:
        return f"projects/{self.project_id}/locations/{self.location}/collections/{COLLECTION}"

    @property
    def data_store_path(self) -> str:
        return f"{self.collection_path}/dataStores/{_short_id(self.data_store_id, 'data_store_id')}"

    @property
    def engine_path(self) -> str:
        return f"{self.collection_path}/engines/{_short_id(self.engine_id, 'engine_id')}"

    @property
    def branch_path(self) -> str:
        return f"{self.data_store_path}/branches/{DEFAULT_BRANCH}"

    @property
    def serving_config_path(self) -> str:
        return f"{self.engine_path}/servingConfigs/{SERVING_CONFIG}"

    @property
    def serving_config_label(self) -> str:
        """Identity-free label for payloads: ``engines/<id>/servingConfigs/default_search``."""
        return f"engines/{self.engine_id}/servingConfigs/{SERVING_CONFIG}"


class DiscoveryEngineClient:
    """Bounded-retry REST calls for one project/location. ``request`` returns the decoded JSON body."""

    def __init__(
        self,
        *,
        project_id: str,
        location: str,
        token_provider: Callable[[], str],
        attempt_timeout_seconds: float,
        budget_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        send: Callable[..., httpx.Response] | None = None,
    ) -> None:
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        if attempt_timeout_seconds <= 0 or budget_seconds <= 0:
            raise ValueError("attempt_timeout_seconds and budget_seconds must be > 0")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        self.project_id = project_id.strip()
        self.host = discovery_host(location)
        self._token_provider = token_provider
        self._attempt_timeout = float(attempt_timeout_seconds)
        self._budget = float(budget_seconds)
        self._sleep = sleep
        self._clock = clock
        self._send = send or httpx.request

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token_provider()}",
            "x-goog-user-project": self.project_id,
            "Content-Type": "application/json; charset=utf-8",
        }

    def _error(self, response: httpx.Response, method: str, path: str) -> DiscoveryEngineHTTPError:
        body = redact_resource_text(response.text[:_MAX_ERROR_BODY_CHARS], self.project_id)
        where = redact_resource_text(path, self.project_id)
        return DiscoveryEngineHTTPError(response.status_code, f"Discovery Engine {method} {where} HTTP {response.status_code}: {body}")

    def request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None, params: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], int]:
        """``(json, attempts)``; ``path`` is relative to ``/v1/`` (e.g. ``projects/.../dataStores``)."""
        if method not in ("GET", "POST", "PATCH", "DELETE"):
            raise ValueError(f"unsupported method {method!r}")
        url = f"{self.host}/v1/{path.lstrip('/')}"
        deadline = self._clock() + self._budget
        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = deadline - self._clock()
            if remaining < _MIN_ATTEMPT_SECONDS:
                break
            try:
                response = self._send(method, url, headers=self.headers(), json=body, params=params,
                                      timeout=httpx.Timeout(min(self._attempt_timeout, remaining)))
            except httpx.TimeoutException as exc:
                last = exc
            else:
                if response.status_code in (200, 204):
                    return (response.json() if response.text.strip() else {}), attempt
                last = self._error(response, method, path)
                if response.status_code not in RETRYABLE_STATUS:
                    raise last
            if attempt < MAX_ATTEMPTS:
                self._sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        if last is None:
            raise TimeoutError(f"Discovery Engine budget ({self._budget:.0f} s) exhausted before the first attempt")
        raise last

    def get_or_none(self, path: str) -> dict[str, Any] | None:
        """GET a resource; ``None`` only for HTTP 404 (every other error raises)."""
        try:
            doc, _ = self.request("GET", path)
        except DiscoveryEngineHTTPError as exc:
            if exc.status_code == 404:
                return None
            raise
        return doc

    def delete_if_exists(self, path: str) -> bool:
        """DELETE a resource; ``True`` when deleted, ``False`` only on HTTP 404 (every other error raises)."""
        try:
            self.request("DELETE", path)
        except DiscoveryEngineHTTPError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    def poll_operation(
        self, operation: dict[str, Any], *, deadline_seconds: float, interval_seconds: float = 5.0,
        on_poll: Callable[[dict[str, Any], int], None] | None = None,
    ) -> dict[str, Any]:
        """Poll ``GET /v1/<operation.name>`` until ``done`` (≤ ``MAX_OPERATION_POLLS`` polls and the deadline)."""
        if not isinstance(operation, dict) or not isinstance(operation.get("name"), str):
            raise ValueError("operation must be a JSON object with a 'name'")
        deadline = self._clock() + float(deadline_seconds)
        current = operation
        for poll in range(MAX_OPERATION_POLLS + 1):
            if current.get("done"):
                return _operation_result(current, self.project_id)
            if on_poll is not None:
                on_poll(current, poll)
            if self._clock() + interval_seconds > deadline:
                break
            self._sleep(interval_seconds)
            current, _ = self.request("GET", current["name"])
        name = redact_resource_text(str(operation.get("name")), self.project_id)
        raise OperationTimeoutError(f"operation {name} not done after {poll} polls / {deadline_seconds:.0f} s")


def _operation_result(operation: dict[str, Any], project_id: str) -> dict[str, Any]:
    error = operation.get("error")
    if isinstance(error, dict) and error:
        message = redact_resource_text(str(error.get("message", error))[:_MAX_ERROR_BODY_CHARS], project_id)
        raise OperationFailedError(f"operation failed (code {error.get('code')}): {message}")
    return operation


__all__ = [
    "BACKOFF_BASE_SECONDS",
    "COLLECTION",
    "DEFAULT_BRANCH",
    "DiscoveryEngineClient",
    "DiscoveryEngineHTTPError",
    "MAX_ATTEMPTS",
    "MAX_OPERATION_POLLS",
    "OperationFailedError",
    "OperationTimeoutError",
    "RETRYABLE_STATUS",
    "SERVING_CONFIG",
    "VaisResources",
    "discovery_host",
    "redact_resource_text",
]
