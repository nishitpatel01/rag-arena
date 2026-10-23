"""Sync the local procurement corpus to the GCS mirror (Cloud Storage JSON API over REST; google-auth + httpx).

Level 5 imports documents from GCS, so the mirror must equal the local corpus. Only missing or changed objects are
uploaded (compared by base64 MD5, the ``md5Hash`` GCS stores for non-composite objects). Nothing is ever deleted and
nothing outside ``GCS_PREFIX`` is listed or written.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from pathlib import Path
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

GCS_PREFIX = "sample_docs/procurement_corpus/"
STORAGE_HOST = "https://storage.googleapis.com"
MAX_LIST_PAGES = 20
LIST_PAGE_SIZE = 1000
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_ATTEMPTS = 3
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_MAX_ERROR_CHARS = 600


class GcsHTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class LocalObject:
    name: str  # object name under GCS_PREFIX
    path: Path
    md5_b64: str
    size: int


@dataclass(frozen=True)
class SyncPlan:
    upload: tuple[LocalObject, ...]
    skip: tuple[LocalObject, ...]
    reasons: tuple[tuple[str, str], ...]  # (name, "missing" | "md5 differs")


@dataclass(frozen=True)
class SyncResult:
    uploaded: tuple[str, ...]
    skipped: tuple[str, ...]
    reasons: tuple[tuple[str, str], ...]
    bytes_uploaded: int
    remote_objects_listed: int
    list_pages: int


def md5_b64(path: Path) -> str:
    digest = hashlib.md5(path.read_bytes(), usedforsecurity=False).digest()
    return base64.b64encode(digest).decode("ascii")


def local_objects(corpus_dir: Path, paths: list[Path]) -> list[LocalObject]:
    """Object names ``GCS_PREFIX + <path relative to corpus_dir>`` for the discovered PDFs (bounded size)."""
    out: list[LocalObject] = []
    root = corpus_dir.resolve()
    for path in paths:
        resolved = path.resolve()
        rel = resolved.relative_to(root).as_posix()
        if rel.startswith("..") or not rel.lower().endswith(".pdf"):
            raise ValueError(f"corpus path outside corpus dir or not a PDF: {rel}")
        size = resolved.stat().st_size
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            raise ValueError(f"{rel} has size {size} bytes (allowed 1..{MAX_UPLOAD_BYTES})")
        out.append(LocalObject(name=GCS_PREFIX + rel, path=resolved, md5_b64=md5_b64(resolved), size=size))
    return out


def plan_sync(local: list[LocalObject], remote_md5: dict[str, str]) -> SyncPlan:
    """Upload what is missing remotely or whose MD5 differs; skip identical objects (pure)."""
    upload: list[LocalObject] = []
    skip: list[LocalObject] = []
    reasons: list[tuple[str, str]] = []
    for obj in local:
        remote = remote_md5.get(obj.name)
        if remote == obj.md5_b64:
            skip.append(obj)
            continue
        upload.append(obj)
        reasons.append((obj.name, "missing" if remote is None else "md5 differs"))
    return SyncPlan(upload=tuple(upload), skip=tuple(skip), reasons=tuple(reasons))


class GcsClient:
    """List + upload for ONE bucket, restricted to ``GCS_PREFIX`` (bounded retries, bounded pagination)."""

    def __init__(
        self, *, bucket: str, token_provider: Callable[[], str], timeout_seconds: float,
        send: Callable[..., httpx.Response] | None = None, sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(bucket, str) or not bucket.strip() or "/" in bucket:
            raise ValueError("bucket must be a bare bucket name")
        if not callable(token_provider) or timeout_seconds <= 0:
            raise ValueError("token_provider must be callable and timeout_seconds > 0")
        self.bucket = bucket.strip()
        self._token = token_provider
        self._timeout = float(timeout_seconds)
        self._send = send or httpx.request
        self._sleep = sleep

    def _redact(self, text: str) -> str:
        return text.replace(self.bucket, "${GCS_CORPUS_BUCKET}")

    def _call(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token()}", **kwargs.pop("headers", {})}
        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self._send(method, url, headers=headers, timeout=httpx.Timeout(self._timeout), **kwargs)
            except httpx.TimeoutException as exc:
                last = exc
            else:
                if resp.status_code == 200:
                    return resp
                last = GcsHTTPError(resp.status_code, self._redact(
                    f"Cloud Storage {method} HTTP {resp.status_code}: {resp.text[:_MAX_ERROR_CHARS]}"))
                if resp.status_code not in RETRYABLE_STATUS:
                    raise last
            if attempt < MAX_ATTEMPTS:
                self._sleep(1.0 * (2 ** (attempt - 1)))
        raise last if last else RuntimeError("Cloud Storage call made no attempt")

    def list_md5(self, prefix: str = GCS_PREFIX) -> tuple[dict[str, str], int]:
        """``({object name: md5Hash}, pages)`` for objects under ``prefix`` (≤ MAX_LIST_PAGES pages; more fails loud)."""
        if not prefix.startswith(GCS_PREFIX):
            raise ValueError(f"listing is restricted to {GCS_PREFIX}")
        url = f"{STORAGE_HOST}/storage/v1/b/{quote(self.bucket, safe='')}/o"
        out: dict[str, str] = {}
        token: str | None = None
        for page in range(1, MAX_LIST_PAGES + 1):
            params = {"prefix": prefix, "maxResults": LIST_PAGE_SIZE, "fields": "items(name,md5Hash,size),nextPageToken"}
            if token:
                params["pageToken"] = token
            doc = self._call("GET", url, params=params).json()
            for item in doc.get("items", []) or []:
                out[str(item.get("name"))] = str(item.get("md5Hash") or "")
            token = doc.get("nextPageToken")
            if not token:
                return out, page
        raise RuntimeError(f"GCS listing under {prefix} exceeded {MAX_LIST_PAGES} pages")

    def upload(self, obj: LocalObject) -> int:
        """Simple media upload of one PDF; verifies the stored md5Hash equals the local MD5."""
        if not obj.name.startswith(GCS_PREFIX):
            raise ValueError(f"uploads are restricted to {GCS_PREFIX}")
        url = f"{STORAGE_HOST}/upload/storage/v1/b/{quote(self.bucket, safe='')}/o"
        data = obj.path.read_bytes()
        resp = self._call("POST", url, params={"uploadType": "media", "name": obj.name},
                          headers={"Content-Type": "application/pdf"}, content=data)
        stored = str(resp.json().get("md5Hash") or "")
        if stored != obj.md5_b64:
            raise RuntimeError(f"uploaded {obj.name} but GCS md5Hash {stored!r} != local {obj.md5_b64!r}")
        return len(data)


def sync_corpus(client: GcsClient, local: list[LocalObject]) -> SyncResult:
    """List the managed prefix, upload missing/changed objects, never delete."""
    remote, pages = client.list_md5(GCS_PREFIX)
    plan = plan_sync(local, remote)
    sent = 0
    for obj in plan.upload:
        sent += client.upload(obj)
    return SyncResult(
        uploaded=tuple(o.name for o in plan.upload), skipped=tuple(o.name for o in plan.skip), reasons=plan.reasons,
        bytes_uploaded=sent, remote_objects_listed=len(remote), list_pages=pages,
    )


__all__ = [
    "GCS_PREFIX",
    "GcsClient",
    "GcsHTTPError",
    "LocalObject",
    "SyncPlan",
    "SyncResult",
    "local_objects",
    "md5_b64",
    "plan_sync",
    "sync_corpus",
]
