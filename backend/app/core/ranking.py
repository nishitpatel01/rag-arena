"""Discovery Engine (Vertex AI Search) Ranking API over REST (google-auth bearer + httpx; no discoveryengine SDK).

``POST https://discoveryengine.googleapis.com/v1/projects/<p>/locations/global/rankingConfigs/default_ranking_config:rank``
with ``Authorization: Bearer <ADC token>`` and ``x-goog-user-project: <p>``; body
``{"model", "query", "topN", "records": [{"id", "title", "content"}], "ignoreRecordDetailsInResponse": true}``.

Documented limits (cloud.google.com/generative-ai-app-builder/docs/ranking, read 2026-10-21): up to 1000 records
per request; models up to version 003 accept 512 tokens per record (title + content), version 004+ accept 1024 —
the API truncates silently beyond that, so records are truncated here first to a visible character bound.

Retries are bounded (≤ ``MAX_ATTEMPTS``) and happen ONLY on 429/500/503/timeouts inside the caller's budget; any
other status raises ``RankingHTTPError`` immediately. There is no fallback ranker (spec P2).
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable

import httpx

RANKING_HOST = "https://discoveryengine.googleapis.com"
RANKING_CONFIG = "default_ranking_config"
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 503})
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
MAX_RECORDS_PER_REQUEST = 1000
# ~4 chars/token: 3,600 content chars + ≤ 200 title chars stay under the 1,024-token record limit of 004 models.
MAX_RECORD_CONTENT_CHARS = 3600
MAX_RECORD_TITLE_CHARS = 200
_MIN_ATTEMPT_SECONDS = 0.5
_MAX_ERROR_BODY_CHARS = 500
_MAX_QUERY_CHARS = 4000


class RankingHTTPError(RuntimeError):
    """Non-2xx answer from the Ranking API (after bounded retries when the status is retryable)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class RankingResponseError(ValueError):
    """A 200 response that does not score exactly the records that were sent."""


@dataclass(frozen=True)
class RankRecord:
    """One record sent to ``rank`` (content already truncated to the documented per-record bound)."""

    id: str
    title: str
    content: str
    content_truncated: bool

    def to_body(self) -> dict[str, str]:
        return {"id": self.id, "title": self.title, "content": self.content}


@dataclass(frozen=True)
class RankResponse:
    """Scores keyed by record id, in the API's returned order (descending score), plus call provenance."""

    scores: tuple[tuple[str, float], ...]
    attempts: int
    duration_ms: int


def ranking_url(project_id: str) -> str:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id must be a non-empty string")
    return f"{RANKING_HOST}/v1/projects/{project_id.strip()}/locations/global/rankingConfigs/{RANKING_CONFIG}:rank"


def make_record(record_id: str, title: str, content: str) -> RankRecord:
    """Build a bounded record: title ≤ 200 chars, content ≤ 3,600 chars (truncation is flagged, never hidden)."""
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError("record id must be a non-empty string")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"record {record_id} has empty content")
    clean_title = " ".join(str(title or "").split())[:MAX_RECORD_TITLE_CHARS]
    body = content.strip()
    return RankRecord(
        id=record_id,
        title=clean_title,
        content=body[:MAX_RECORD_CONTENT_CHARS],
        content_truncated=len(body) > MAX_RECORD_CONTENT_CHARS,
    )


def redact_project(message: str, project_id: str) -> str:
    project = project_id.strip()
    return message.replace(project, "${GCP_PROJECT_ID}") if project else message


class RankingClient:
    """One Ranking API model for one project; ``rank`` performs the bounded-retry REST call."""

    def __init__(
        self,
        *,
        project_id: str,
        model: str,
        attempt_timeout_seconds: float,
        budget_seconds: float,
        token_provider: Callable[[], str],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        post: Callable[..., httpx.Response] | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if attempt_timeout_seconds <= 0 or budget_seconds <= 0:
            raise ValueError("attempt_timeout_seconds and budget_seconds must be > 0")
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        self.project_id = project_id.strip()
        self.model = model.strip()
        self.endpoint_url = ranking_url(project_id)
        self._attempt_timeout = float(attempt_timeout_seconds)
        self._budget = float(budget_seconds)
        self._token_provider = token_provider
        self._sleep = sleep
        self._clock = clock
        self._post = post

    def body(self, query: str, records: list[RankRecord], top_n: int) -> dict[str, Any]:
        """The exact JSON body sent (validated bounds)."""
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY_CHARS:
            raise ValueError(f"query must be a non-empty string of at most {_MAX_QUERY_CHARS} characters")
        if not records or len(records) > MAX_RECORDS_PER_REQUEST:
            raise ValueError(f"records must contain 1..{MAX_RECORDS_PER_REQUEST} items, got {len(records)}")
        if len({r.id for r in records}) != len(records):
            raise ValueError("record ids must be unique")
        if not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= len(records):
            raise ValueError(f"top_n must be an integer in [1, {len(records)}]")
        return {
            "model": self.model,
            "query": query.strip(),
            "topN": top_n,
            "records": [r.to_body() for r in records],
            "ignoreRecordDetailsInResponse": True,
        }

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token_provider()}",
            "x-goog-user-project": self.project_id,
            "Content-Type": "application/json",
        }

    def rank(self, query: str, records: list[RankRecord], top_n: int) -> RankResponse:
        """Call ``rank`` once (bounded retries on transient failures); validate ids/scores against the request."""
        body = self.body(query, records, top_n)
        start = self._clock()
        doc, attempts = self._post_with_retries(body)
        scores = parse_rank_response(doc, sent_ids=[r.id for r in records], top_n=top_n)
        return RankResponse(scores=scores, attempts=attempts, duration_ms=max(0, int((self._clock() - start) * 1000)))

    def _post_once(self, body: dict[str, Any], timeout: float) -> httpx.Response:
        poster = self._post or httpx.post
        return poster(self.endpoint_url, json=body, headers=self.headers(), timeout=timeout)

    def _http_error(self, response: httpx.Response) -> RankingHTTPError:
        text = redact_project(response.text[:_MAX_ERROR_BODY_CHARS], self.project_id)
        return RankingHTTPError(response.status_code, f"Ranking API HTTP {response.status_code}: {text}")

    def _post_with_retries(self, body: dict[str, Any]) -> tuple[Any, int]:
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
            raise TimeoutError(f"Ranking API budget ({self._budget:.0f} s) exhausted before the first attempt")
        raise last_error


def parse_rank_response(doc: Any, *, sent_ids: list[str], top_n: int) -> tuple[tuple[str, float], ...]:
    """``((id, score), ...)`` in returned order; exactly ``top_n`` distinct sent ids, each with a numeric score."""
    if not isinstance(doc, dict) or not isinstance(doc.get("records"), list):
        raise RankingResponseError("Ranking API response has no 'records' list")
    allowed = set(sent_ids)
    out: list[tuple[str, float]] = []
    for rec in doc["records"][: len(sent_ids) + 1]:
        rid = rec.get("id") if isinstance(rec, dict) else None
        score = rec.get("score") if isinstance(rec, dict) else None
        if rid not in allowed:
            raise RankingResponseError(f"Ranking API returned an unknown record id {rid!r}")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise RankingResponseError(f"Ranking API returned no numeric score for record {rid!r}")
        out.append((str(rid), float(score)))
    if len(out) != top_n or len({rid for rid, _ in out}) != len(out):
        raise RankingResponseError(f"Ranking API scored {len(out)} records (distinct ids required) for topN={top_n}")
    return tuple(out)


__all__ = [
    "MAX_ATTEMPTS",
    "MAX_RECORDS_PER_REQUEST",
    "MAX_RECORD_CONTENT_CHARS",
    "MAX_RECORD_TITLE_CHARS",
    "RETRYABLE_STATUS",
    "RankRecord",
    "RankResponse",
    "RankingClient",
    "RankingHTTPError",
    "RankingResponseError",
    "make_record",
    "parse_rank_response",
    "ranking_url",
]
