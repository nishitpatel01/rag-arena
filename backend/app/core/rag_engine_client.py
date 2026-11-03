"""Vertex AI RAG Engine REST v1 client: google-auth bearer + httpx, no heavy google-cloud-aiplatform dependency.

Used by Level 6 (``rag_engine``) for provisioning (``scripts/provision_rag_engine.py``), ingestion
(``ragFiles:import`` LRO, ``ragFiles`` list, per-upload ``RagCorpus`` creation/deletion) and querying
(``:retrieveContexts``).

* Host: ``https://<loc>-aiplatform.googleapis.com`` (regional only; ``global`` is rejected at the boundary).
* Regional singleton safety invariant: ``ragEngineConfig`` mutations carrying ``unprovisioned`` (or ``DELETE``) are
  rejected before any HTTP call is sent, protecting existing non-Arena corpora in shared GCP projects.
* Retries are bounded (≤ ``MAX_ATTEMPTS``) and happen ONLY on 429/500/503/timeouts inside the caller's budget; any
  other status raises ``RagEngineHTTPError`` immediately with the (redacted) response body. No fallback.
* Every error message and operation trace redacts ``${GCP_PROJECT_ID}`` and ``projects/${GCP_PROJECT_NUMBER}``.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any, Callable

import httpx

from app.core.vertex_search import (
    BACKOFF_BASE_SECONDS,
    MAX_ATTEMPTS,
    MAX_OPERATION_POLLS,
    OperationFailedError,
    OperationTimeoutError,
    RETRYABLE_STATUS,
    redact_resource_text,
)

_MIN_ATTEMPT_SECONDS = 0.5
_MAX_ERROR_BODY_CHARS = 1500
_MAX_GUARD_DEPTH = 16
_REGION_RE = re.compile(r"[a-z]+-[a-z]+[0-9]+")
_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_MODEL_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,110}(?:@[a-z0-9._-]{1,16})?")


class RagEngineHTTPError(RuntimeError):
    """Non-2xx answer from Vertex AI RAG Engine (after bounded retries when the status is retryable)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def aiplatform_host(location: str) -> str:
    """Regional Vertex AI endpoint host (``RagCorpus`` is regional; ``global`` is rejected)."""
    if not isinstance(location, str) or not _REGION_RE.fullmatch(location.strip()):
        raise ValueError(f"invalid regional Vertex AI RAG Engine location (expected e.g. 'us-central1'): {location!r}")
    return f"https://{location.strip()}-aiplatform.googleapis.com"


def _short_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value.strip()):
        raise ValueError(f"{name} must be a short RAG Engine id (not a full resource path), got {value!r}")
    return value.strip()


def _contains_unprovisioned_key(value: Any, _depth: int = 0) -> bool:
    """True when any nested dict in ``value`` carries the destructive ``unprovisioned`` tier key."""
    if _depth > _MAX_GUARD_DEPTH:
        raise ValueError("request body nesting exceeds guard bound")
    if isinstance(value, dict):
        if "unprovisioned" in value:
            return True
        return any(_contains_unprovisioned_key(v, _depth + 1) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_unprovisioned_key(item, _depth + 1) for item in value)
    return False


def classify_engine_config(doc: dict[str, Any] | None) -> str:
    """Classify a ``GoogleCloudAiplatformV1RagEngineConfig`` JSON payload into its active DB mode."""
    if not isinstance(doc, dict):
        return "unprovisioned"
    cfg = doc.get("ragManagedDbConfig")
    if not isinstance(cfg, dict) or not cfg or "unprovisioned" in cfg:
        return "unprovisioned"
    if "serverless" in cfg:
        return "serverless"
    if "enterprise" in cfg:
        return "enterprise"
    spanner = cfg.get("spanner")
    if isinstance(spanner, dict):
        if "unprovisioned" in spanner:
            return "unprovisioned"
        if "scaled" in spanner:
            return "spanner_scaled"
        if "basic" in spanner:
            return "spanner_basic"
        return "spanner"
    if "scaled" in cfg:
        return "scaled"
    if "basic" in cfg:
        return "basic"
    return sorted(str(k) for k in cfg.keys())[0]


def extract_short_corpus_id(resource_name: str) -> str:
    """Extract ``<short_id>`` from ``projects/<p>/locations/<loc>/ragCorpora/<short_id>``."""
    if not isinstance(resource_name, str) or "/ragCorpora/" not in resource_name:
        raise ValueError(f"expected a RagCorpus resource name (.../ragCorpora/<id>), got {resource_name!r}")
    short_id = resource_name.rsplit("/ragCorpora/", 1)[-1].strip().strip("/")
    if not short_id or "/" in short_id:
        raise ValueError(f"invalid RagCorpus resource name: {resource_name!r}")
    return _short_id(short_id, "corpus_id")


def corpus_body(
    res: "RagEngineResources",
    *,
    display_name: str,
    embedding_model: str = "text-embedding-005",
    description: str = "",
) -> dict[str, Any]:
    """REST v1 ``RagCorpus`` payload using managed KNN vector search and publisher embedding model."""
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError("display_name must be non-empty")
    return {
        "displayName": display_name.strip(),
        "description": description,
        "vectorDbConfig": {
            "ragManagedDb": {"knn": {}},
            "ragEmbeddingModelConfig": {
                "vertexPredictionEndpoint": {
                    "endpoint": res.publisher_model_endpoint(embedding_model),
                }
            },
        },
    }


@dataclass(frozen=True)
class RagEngineResources:
    """Vertex AI RAG Engine paths for one project + region + optional short corpus id."""

    project_id: str
    location: str
    corpus_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        aiplatform_host(self.location)
        if self.corpus_id:
            _short_id(self.corpus_id, "corpus_id")

    @property
    def parent_path(self) -> str:
        return f"projects/{self.project_id.strip()}/locations/{self.location.strip()}"

    @property
    def engine_config_path(self) -> str:
        return f"{self.parent_path}/ragEngineConfig"

    @property
    def corpora_path(self) -> str:
        return f"{self.parent_path}/ragCorpora"

    @property
    def corpus_path(self) -> str:
        return f"{self.corpora_path}/{_short_id(self.corpus_id, 'corpus_id')}"

    @property
    def corpus_label(self) -> str:
        """Identity-free label for payloads: ``ragCorpora/<short_id>``."""
        return f"ragCorpora/{_short_id(self.corpus_id, 'corpus_id')}"

    def publisher_model_endpoint(self, model: str) -> str:
        if not isinstance(model, str) or not _MODEL_ID_RE.fullmatch(model.strip()):
            raise ValueError(f"invalid publisher model id: {model!r}")
        return f"{self.parent_path}/publishers/google/models/{model.strip()}"


class VertexRagClient:
    """Bounded-retry REST v1 client for Vertex AI RAG Engine in one project/region."""

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
        self.location = location.strip()
        self.host = aiplatform_host(self.location)
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

    def _error(self, response: httpx.Response, method: str, path: str) -> RagEngineHTTPError:
        body = redact_resource_text(response.text[:_MAX_ERROR_BODY_CHARS], self.project_id)
        where = redact_resource_text(path, self.project_id)
        return RagEngineHTTPError(
            response.status_code, f"Vertex AI RAG Engine {method} {where} HTTP {response.status_code}: {body}"
        )

    def _enforce_safety_guards(self, method: str, path: str, body: dict[str, Any] | None) -> None:
        if method in ("POST", "PATCH", "DELETE") and "ragEngineConfig" in path:
            if method == "DELETE" or _contains_unprovisioned_key(body):
                raise ValueError(
                    "Refusing to unprovision or delete regional ragEngineConfig: doing so destroys all RAG corpora in the region"
                )

    def request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None, params: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], int]:
        """``(json, attempts)``; ``path`` is relative to ``/v1/`` (e.g. ``projects/.../ragCorpora``)."""
        if method not in ("GET", "POST", "PATCH", "DELETE"):
            raise ValueError(f"unsupported method {method!r}")
        self._enforce_safety_guards(method, path, body)
        url = f"{self.host}/v1/{path.lstrip('/')}"
        deadline = self._clock() + self._budget
        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = deadline - self._clock()
            if remaining < _MIN_ATTEMPT_SECONDS:
                break
            try:
                response = self._send(
                    method,
                    url,
                    headers=self.headers(),
                    json=body,
                    params=params,
                    timeout=httpx.Timeout(min(self._attempt_timeout, remaining)),
                )
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
            raise TimeoutError(f"Vertex AI RAG Engine budget ({self._budget:.0f} s) exhausted before the first attempt")
        raise last

    def get_or_none(self, path: str) -> dict[str, Any] | None:
        """GET a resource; ``None`` only for HTTP 404 (every other error raises)."""
        try:
            doc, _ = self.request("GET", path)
        except RagEngineHTTPError as exc:
            if exc.status_code == 404:
                return None
            raise
        return doc

    def delete_corpus_if_exists(
        self, corpus_path: str, *, deadline_seconds: float = 180.0, interval_seconds: float = 2.0
    ) -> bool:
        """DELETE a RagCorpus (polling its LRO to completion); ``False`` only on HTTP 404."""
        if "ragEngineConfig" in corpus_path or "/ragCorpora/" not in corpus_path:
            raise ValueError(f"delete_corpus_if_exists requires a .../ragCorpora/<id> path, got {corpus_path!r}")
        try:
            resp, _ = self.request("DELETE", corpus_path)
        except RagEngineHTTPError as exc:
            if exc.status_code == 404:
                return False
            raise
        if isinstance(resp, dict) and isinstance(resp.get("name"), str) and (
            "done" in resp or "/operations/" in resp["name"]
        ):
            self.poll_operation(resp, deadline_seconds=deadline_seconds, interval_seconds=interval_seconds)
        return True

    def list_corpora(self, corpora_path: str, *, max_pages: int = 20) -> list[dict[str, Any]]:
        """List all ``RagCorpus`` items under ``corpora_path`` with bounded pagination."""
        if max_pages < 1 or max_pages > 100:
            raise ValueError("max_pages must be between 1 and 100")
        items: list[dict[str, Any]] = []
        token = ""
        for _ in range(max_pages):
            params: dict[str, Any] = {"pageSize": 100}
            if token:
                params["pageToken"] = token
            body, _ = self.request("GET", corpora_path, params=params)
            batch = body.get("ragCorpora")
            if isinstance(batch, list):
                items.extend(c for c in batch if isinstance(c, dict))
            token = str(body.get("nextPageToken") or "").strip()
            if not token:
                return items
        raise RuntimeError(f"ragCorpora pagination exceeded {max_pages} pages")

    def list_files(self, corpus_path: str, *, max_pages: int = 50) -> list[dict[str, Any]]:
        """List all ``RagFile`` items in ``corpus_path`` with bounded pagination."""
        if max_pages < 1 or max_pages > 200:
            raise ValueError("max_pages must be between 1 and 200")
        items: list[dict[str, Any]] = []
        token = ""
        files_path = f"{corpus_path.rstrip('/')}/ragFiles"
        for _ in range(max_pages):
            params: dict[str, Any] = {"pageSize": 100}
            if token:
                params["pageToken"] = token
            body, _ = self.request("GET", files_path, params=params)
            batch = body.get("ragFiles")
            if isinstance(batch, list):
                items.extend(f for f in batch if isinstance(f, dict))
            token = str(body.get("nextPageToken") or "").strip()
            if not token:
                return items
        raise RuntimeError(f"ragFiles pagination exceeded {max_pages} pages")

    def poll_operation(
        self,
        operation: dict[str, Any],
        *,
        deadline_seconds: float,
        interval_seconds: float = 5.0,
        on_poll: Callable[[dict[str, Any], int], None] | None = None,
    ) -> dict[str, Any]:
        """Poll ``GET /v1/<operation.name>`` until ``done`` (≤ ``MAX_OPERATION_POLLS`` polls and the deadline)."""
        if not isinstance(operation, dict) or not isinstance(operation.get("name"), str):
            raise ValueError("operation must be a JSON object with a 'name'")
        deadline = self._clock() + float(deadline_seconds)
        current = operation
        for poll in range(MAX_OPERATION_POLLS + 1):
            if current.get("done"):
                error = current.get("error")
                if isinstance(error, dict) and error:
                    msg = redact_resource_text(str(error.get("message", error))[:_MAX_ERROR_BODY_CHARS], self.project_id)
                    raise OperationFailedError(f"operation failed (code {error.get('code')}): {msg}")
                return current
            if on_poll is not None:
                on_poll(current, poll)
            if self._clock() + interval_seconds > deadline:
                break
            self._sleep(interval_seconds)
            current, _ = self.request("GET", current["name"])
        name = redact_resource_text(str(operation.get("name")), self.project_id)
        raise OperationTimeoutError(f"operation {name} not done after {poll} polls / {deadline_seconds:.0f} s")


__all__ = [
    "OperationFailedError",
    "OperationTimeoutError",
    "RagEngineHTTPError",
    "RagEngineResources",
    "VertexRagClient",
    "aiplatform_host",
    "classify_engine_config",
    "corpus_body",
    "extract_short_corpus_id",
    "redact_resource_text",
]
