"""User document uploads (v1.1): workspace ids, filename + PDF safety checks, and the private-bucket object store.

Flow: the browser asks the API for a workspace and a **resumable upload session** per file; the session URI is created
here with the backend's own credentials (``ifGenerationMatch=0`` so nothing is ever overwritten; ``Origin`` bound to the
app) and the browser sends the bytes straight to Cloud Storage. That avoids Cloud Run's 32 MiB request-body cap, so the
app imposes no size limit (owner decision 2026-10-26). On finalize the object is copied to local disk in bounded ranged
reads and validated for SAFETY only (real PDF, not encrypted, no JavaScript / launch actions / embedded files) — never
for size or page count.

Each workspace is a prefix ``uploads/<upl_id>/`` holding the files and ``workspace.json`` (the manifest, updated with
generation preconditions so concurrent writers fail loudly instead of overwriting each other). Deleting a workspace
deletes exactly that prefix. Nothing expires automatically (owner decision: keep until the user deletes it).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

STORAGE_HOST = "https://storage.googleapis.com"
UPLOAD_PREFIX = "uploads/"
MANIFEST_NAME = "workspace.json"
MAX_ATTEMPTS = 3
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
MAX_LIST_PAGES = 20
LIST_PAGE_SIZE = 1000
DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
MAX_FILENAME_CHARS = 120
_WS_RE = re.compile(r"^upl_[0-9a-f]{12}$")
_FILE_RE = re.compile(r"^f_[0-9a-f]{12}$")
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_JS_RE = re.compile(r"/(JavaScript|JS)\b")
_LAUNCH_RE = re.compile(r"/Launch\b")
_MAX_ERROR_CHARS = 400


class PdfRejected(ValueError):
    """The uploaded file is not a safe, readable PDF (message names the reason)."""


class UploadStoreError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PdfInfo:
    pages: int
    size_bytes: int
    sha256: str


# ---------------------------------------------------------------------------------------------------- ids and names

def new_workspace_id() -> str:
    return f"upl_{secrets.token_hex(6)}"


def new_file_id() -> str:
    return f"f_{secrets.token_hex(6)}"


def check_workspace_id(workspace_id: str) -> str:
    if not isinstance(workspace_id, str) or not _WS_RE.match(workspace_id):
        raise ValueError("workspace id must look like upl_<12 hex>")
    return workspace_id


def check_file_id(file_id: str) -> str:
    if not isinstance(file_id, str) or not _FILE_RE.match(file_id):
        raise ValueError("file id must look like f_<12 hex>")
    return file_id


def sanitize_filename(name: str) -> str:
    """Basename only, safe characters, must end in ``.pdf``; raises ``ValueError`` for anything else."""
    if not isinstance(name, str) or _CONTROL_CHARS.search(name):
        raise ValueError("filename must be text without control characters")
    base = re.split(r"[\\/]", name.strip())[-1].strip()
    if not base.lower().endswith(".pdf"):
        raise ValueError("only .pdf files can be uploaded")
    stem = _UNSAFE_CHARS.sub("_", base[:-4]).strip(".")
    if not stem:
        raise ValueError("filename needs a name before .pdf")
    return f"{stem[: MAX_FILENAME_CHARS - 4]}.pdf"


def owner_key(identity: str) -> str:
    """SHA-256 of the signed-in identity (IAP prefix stripped, lower-cased); manifests never store the email."""
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("identity is required")
    value = identity.strip()
    if value.startswith("accounts.google.com:"):
        value = value[len("accounts.google.com:"):]
    return hashlib.sha256(value.lower().encode("utf-8")).hexdigest()


def object_name(workspace_id: str, file_id: str, safe_name: str) -> str:
    check_workspace_id(workspace_id)
    check_file_id(file_id)
    if sanitize_filename(safe_name) != safe_name:
        raise ValueError("object names are built from sanitized filenames only")
    return f"{UPLOAD_PREFIX}{workspace_id}/{file_id}-{safe_name}"


def manifest_object(workspace_id: str) -> str:
    return f"{UPLOAD_PREFIX}{check_workspace_id(workspace_id)}/{MANIFEST_NAME}"


# ---------------------------------------------------------------------------------------------------- PDF safety

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(HASH_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _active_content(doc: Any) -> str:
    """'' when clean, else the reason (JavaScript / launch actions / embedded files)."""
    if doc.embfile_count() > 0:
        return "PDF contains embedded files"
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref, compressed=True)
        except Exception:  # noqa: BLE001 - a broken object is skipped; parsing problems surface via page_count
            continue
        if _JS_RE.search(obj):
            return "PDF contains JavaScript"
        if _LAUNCH_RE.search(obj):
            return "PDF contains launch actions"
    return ""


def validate_pdf_file(path: Path) -> PdfInfo:
    """Safety checks only (owner decision: no size or page limits). Raises ``PdfRejected`` naming the reason."""
    import pymupdf  # type: ignore[import-not-found]

    path = Path(path)
    size = path.stat().st_size
    if size == 0:
        raise PdfRejected("file is empty")
    with path.open("rb") as fh:
        if b"%PDF-" not in fh.read(1024):
            raise PdfRejected("not a PDF (missing %PDF- header)")
    try:
        doc = pymupdf.open(str(path), filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - re-raised as a typed rejection
        raise PdfRejected(f"unreadable PDF ({type(exc).__name__})") from exc
    with doc:
        if doc.needs_pass or doc.is_encrypted:
            raise PdfRejected("PDF is encrypted or password-protected")
        pages = int(doc.page_count)
        if pages < 1:
            raise PdfRejected("unreadable PDF (no pages)")
        reason = _active_content(doc)
        if reason:
            raise PdfRejected(reason)
    return PdfInfo(pages=pages, size_bytes=size, sha256=_sha256_file(path))


# ---------------------------------------------------------------------------------------------------- object store

class UploadStore:
    """Cloud Storage JSON API for ONE bucket, restricted to ``uploads/<upl_id>/`` (bounded retries + pagination)."""

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
        return text.replace(self.bucket, "${UPLOAD_BUCKET}")[:_MAX_ERROR_CHARS]

    def _call(self, method: str, url: str, *, ok: tuple[int, ...] = (200,), **kwargs: Any) -> httpx.Response:
        try:
            token = self._token()
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed store error
            raise UploadStoreError(0, self._redact(f"Cloud Storage credentials unavailable: {type(exc).__name__}: {exc}")) from exc
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        last: UploadStoreError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self._send(method, url, headers=headers, timeout=httpx.Timeout(self._timeout), **kwargs)
            except httpx.TimeoutException:
                last = UploadStoreError(0, f"Cloud Storage {method} timed out")
            else:
                if resp.status_code in ok:
                    return resp
                last = UploadStoreError(resp.status_code, self._redact(
                    f"Cloud Storage {method} HTTP {resp.status_code}: {resp.text}"))
                if resp.status_code not in RETRYABLE_STATUS:
                    raise last
            if attempt < MAX_ATTEMPTS:
                self._sleep(1.0 * (2 ** (attempt - 1)))
        raise last if last else UploadStoreError(0, "Cloud Storage call made no attempt")

    def _o(self, name: str = "") -> str:
        base = f"{STORAGE_HOST}/storage/v1/b/{quote(self.bucket, safe='')}/o"
        return f"{base}/{quote(name, safe='')}" if name else base

    def _upload_url(self) -> str:
        return f"{STORAGE_HOST}/upload/storage/v1/b/{quote(self.bucket, safe='')}/o"

    @staticmethod
    def _check_scoped(name: str) -> str:
        parts = name.split("/")
        if len(parts) != 3 or parts[0] + "/" != UPLOAD_PREFIX or ".." in name:
            raise ValueError("object must be uploads/<upl_id>/<name>")
        check_workspace_id(parts[1])
        return name

    # ----- sessions -----
    def start_resumable(self, workspace_id: str, file_id: str, safe_name: str, *, origin: str | None) -> str:
        """Create a resumable upload session (create-only) and return its URI for the browser."""
        name = object_name(workspace_id, file_id, safe_name)
        headers = {"X-Upload-Content-Type": "application/pdf", "Content-Type": "application/json; charset=UTF-8"}
        if origin:
            headers["Origin"] = origin
        resp = self._call("POST", self._upload_url(), headers=headers, json={"contentType": "application/pdf"},
                          params={"uploadType": "resumable", "name": name, "ifGenerationMatch": "0"})
        location = resp.headers.get("Location", "")
        if not location.startswith("https://"):
            raise UploadStoreError(resp.status_code, "Cloud Storage did not return a resumable session URI")
        return location

    # ----- objects -----
    def object_meta(self, name: str) -> dict[str, Any] | None:
        try:
            return self._call("GET", self._o(self._check_scoped(name))).json()
        except UploadStoreError as exc:
            if exc.status_code == 404:
                return None
            raise

    def download_to(self, name: str, dest: Path) -> int:
        """Copy an object to local disk in bounded ranged reads (memory stays at one chunk)."""
        meta = self.object_meta(name)
        if meta is None:
            raise UploadStoreError(404, "uploaded object not found (upload incomplete?)")
        size = int(meta.get("size") or 0)
        written = 0
        with Path(dest).open("wb") as fh:
            for index in range(max(1, math.ceil(size / DOWNLOAD_CHUNK_BYTES))):
                start = index * DOWNLOAD_CHUNK_BYTES
                end = min(size, start + DOWNLOAD_CHUNK_BYTES) - 1
                if end < start:
                    break
                resp = self._call("GET", self._o(name), ok=(200, 206), params={"alt": "media"},
                                  headers={"Range": f"bytes={start}-{end}"})
                fh.write(resp.content)
                written += len(resp.content)
        if written != size:
            raise UploadStoreError(0, f"downloaded {written} bytes but object has {size}")
        return written

    def delete_object(self, name: str) -> bool:
        try:
            self._call("DELETE", self._o(self._check_scoped(name)), ok=(200, 204))
        except UploadStoreError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    def _list(self, prefix: str) -> list[str]:
        names: list[str] = []
        token: str | None = None
        for _page in range(MAX_LIST_PAGES):
            params: dict[str, Any] = {"prefix": prefix, "maxResults": LIST_PAGE_SIZE, "fields": "items(name),nextPageToken"}
            if token:
                params["pageToken"] = token
            doc = self._call("GET", self._o(), params=params).json()
            names.extend(str(item.get("name")) for item in doc.get("items", []) or [])
            token = doc.get("nextPageToken")
            if not token:
                return names
        raise UploadStoreError(0, f"listing {prefix} exceeded {MAX_LIST_PAGES} pages")

    def delete_workspace(self, workspace_id: str) -> int:
        prefix = f"{UPLOAD_PREFIX}{check_workspace_id(workspace_id)}/"
        return sum(1 for name in self._list(prefix) if self.delete_object(name))

    def list_workspace_ids(self) -> list[str]:
        ids = []
        for name in self._list(UPLOAD_PREFIX):
            parts = name.split("/")
            if len(parts) == 3 and parts[2] == MANIFEST_NAME and _WS_RE.match(parts[1]):
                ids.append(parts[1])
        return sorted(ids)

    # ----- manifest -----
    def read_manifest(self, workspace_id: str) -> tuple[dict[str, Any], int] | None:
        name = manifest_object(workspace_id)
        meta = self.object_meta(name)
        if meta is None:
            return None
        generation = int(meta.get("generation") or 0)
        resp = self._call("GET", self._o(name), params={"alt": "media", "generation": str(generation)})
        return json.loads(resp.content.decode("utf-8")), generation

    def write_manifest(self, workspace_id: str, manifest: dict[str, Any], *, if_generation: int) -> int:
        """Write with ``ifGenerationMatch`` (0 = must not exist); a concurrent change fails loudly."""
        name = manifest_object(workspace_id)
        body = json.dumps(manifest, sort_keys=True).encode("utf-8")
        try:
            resp = self._call("POST", self._upload_url(), headers={"Content-Type": "application/json"}, content=body,
                              params={"uploadType": "media", "name": name, "ifGenerationMatch": str(int(if_generation))})
        except UploadStoreError as exc:
            if exc.status_code == 412:
                raise RuntimeError("workspace manifest changed concurrently; reload and retry") from exc
            raise
        return int(resp.json().get("generation") or 0)


__all__ = [
    "MANIFEST_NAME",
    "PdfInfo",
    "PdfRejected",
    "UPLOAD_PREFIX",
    "UploadStore",
    "UploadStoreError",
    "check_file_id",
    "check_workspace_id",
    "manifest_object",
    "new_file_id",
    "new_workspace_id",
    "object_name",
    "owner_key",
    "sanitize_filename",
    "validate_pdf_file",
]
