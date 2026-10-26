"""Upload API (v1.1): private bucket status/create, per-user workspaces, resumable sessions, finalize, delete.

Live service only: in replay mode (public replay copy) every route answers 404. The signed-in identity comes from the
IAP header ``X-Goog-Authenticated-User-Email`` (Cloud Run with IAP only receives requests through IAP); without IAP
(local dev, ``ACCESS_MODE=public``) everyone shares the ``anonymous`` owner. Workspaces store only a SHA-256 of the
identity, and another owner's workspace answers 404.

The bucket is created on demand by default (``UPLOAD_BUCKET_MODE=auto``): ``POST /api/v1/uploads/bucket`` and the
first ``POST /api/v1/uploads`` call ``ensure()``. A denied create returns the typed state + remedy; nothing falls back.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
from typing import Any, Protocol
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.config import ArenaSettings
from app.core.telemetry import utc_now_iso
from app.ingestion.upload_bucket import BucketState, BucketStatus, UploadBucketManager
from app.ingestion.uploads import (
    PdfRejected,
    UploadStore,
    UploadStoreError,
    check_file_id,
    check_workspace_id,
    new_file_id,
    new_workspace_id,
    object_name,
    owner_key,
    sanitize_filename,
    validate_pdf_file,
)

IAP_EMAIL_HEADER = "x-goog-authenticated-user-email"
ANONYMOUS = "anonymous"
MANIFEST_VERSION = 1
BUCKET_TIMEOUT_SECONDS = 30.0
STORE_TIMEOUT_SECONDS = 60.0
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
_STATE_HTTP = {
    BucketState.OK: 200, BucketState.CREATED: 200, BucketState.PERMISSION_DENIED: 403,
    BucketState.ORG_POLICY: 409, BucketState.NAME_CONFLICT: 409, BucketState.NOT_FOUND: 409,
    BucketState.UNSAFE_BUCKET: 409, BucketState.UNREACHABLE: 503, BucketState.ERROR: 502,
}


class BucketManagerLike(Protocol):
    def check(self) -> BucketStatus: ...
    def ensure(self) -> BucketStatus: ...


class UploadServices:
    """Real GCS-backed services built from settings (ADC identity: you locally, the service account on Cloud Run)."""

    def __init__(self, settings: ArenaSettings) -> None:
        if not settings.upload_bucket:
            raise ValueError("UPLOAD_BUCKET could not be derived (GCP_PROJECT_ID is empty)")
        self._settings = settings

    def _token(self) -> str:
        from app.core.gcp_clients import get_access_token

        return get_access_token(self._settings)

    def bucket_manager(self) -> UploadBucketManager:
        s = self._settings
        return UploadBucketManager(
            project_id=s.gcp_project_id, bucket=s.upload_bucket, location=s.gcp_location, mode=s.upload_bucket_mode,
            kms_key=s.upload_bucket_kms_key, token_provider=self._token, timeout_seconds=BUCKET_TIMEOUT_SECONDS)

    def store(self) -> UploadStore:
        return UploadStore(bucket=self._settings.upload_bucket, token_provider=self._token,
                           timeout_seconds=STORE_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------------------------------- helpers

def _error(message: str, status: int, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


def _services(request: Request) -> Any | None:
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, ArenaSettings) or not settings.is_live:
        return None
    return getattr(request.app.state, "upload_services", None)


def _unavailable() -> JSONResponse:
    return _error("uploads are available only on the live service (this copy is replay-only)", 404)


def _identity(request: Request) -> str:
    return request.headers.get(IAP_EMAIL_HEADER, "").strip() or ANONYMOUS


def _origin_for_session(request: Request) -> str | None:
    """The browser Origin bound into the resumable session; must be this app (or localhost dev off Cloud Run)."""
    origin = request.headers.get("origin", "").strip()
    if not origin:
        return None
    if urlsplit(origin).netloc == request.headers.get("host", ""):
        return origin
    if not os.environ.get("K_SERVICE") and _LOCAL_ORIGIN_RE.match(origin):
        return origin
    raise ValueError("request origin does not match this app")


def _bucket_response(status: BucketStatus) -> JSONResponse:
    return JSONResponse(status.to_dict(safe=False), status_code=_STATE_HTTP.get(status.state, 500))


def _public_file(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if k != "object"}


def _public_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in manifest.items() if k != "owner"}
    out["files"] = [_public_file(f) for f in manifest.get("files", [])]
    return out


async def _owned_manifest(request: Request, store: Any, workspace_id: str) -> tuple[dict[str, Any], int] | JSONResponse:
    try:
        check_workspace_id(workspace_id)
    except ValueError:
        return _error("workspace not found", 404)
    found = await run_in_threadpool(store.read_manifest, workspace_id)
    if found is None or found[0].get("owner") != owner_key(_identity(request)):
        return _error("workspace not found", 404)
    return found


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise ValueError("request body must be JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    return body


# ---------------------------------------------------------------------------------------------------- bucket

async def bucket_status_endpoint(request: Request) -> JSONResponse:
    services = _services(request)
    if services is None:
        return _unavailable()
    status = await run_in_threadpool(services.bucket_manager().check)
    return JSONResponse(status.to_dict(safe=False))  # a status report: always 200, the state field is the outcome


async def bucket_create_endpoint(request: Request) -> JSONResponse:
    services = _services(request)
    if services is None:
        return _unavailable()
    return _bucket_response(await run_in_threadpool(services.bucket_manager().ensure))


# ---------------------------------------------------------------------------------------------------- workspaces

async def workspaces_list_endpoint(request: Request) -> JSONResponse:
    services = _services(request)
    if services is None:
        return _unavailable()
    store, me = services.store(), owner_key(_identity(request))
    ids = await run_in_threadpool(store.list_workspace_ids)
    mine = []
    for ws in ids:
        found = await run_in_threadpool(store.read_manifest, ws)
        if found is not None and found[0].get("owner") == me:
            mine.append(_public_manifest(found[0]))
    return JSONResponse({"workspaces": mine})


async def workspace_create_endpoint(request: Request) -> JSONResponse:
    services = _services(request)
    if services is None:
        return _unavailable()
    bucket = await run_in_threadpool(services.bucket_manager().ensure)
    if not bucket.usable:
        return _error(f"upload bucket is not usable: {bucket.message}", 409, bucket=bucket.to_dict(safe=False))
    ws = new_workspace_id()
    manifest = {"version": MANIFEST_VERSION, "id": ws, "owner": owner_key(_identity(request)),
                "created_at": utc_now_iso(), "files": [], "levels": {}}
    await run_in_threadpool(lambda: services.store().write_manifest(ws, manifest, if_generation=0))
    return JSONResponse({"workspace": _public_manifest(manifest), "bucket": bucket.to_dict(safe=False)},
                        status_code=201)


async def workspace_get_endpoint(request: Request) -> JSONResponse:
    services = _services(request)
    if services is None:
        return _unavailable()
    found = await _owned_manifest(request, services.store(), request.path_params["workspace_id"])
    if isinstance(found, JSONResponse):
        return found
    return JSONResponse({"workspace": _public_manifest(found[0])})


async def workspace_delete_endpoint(request: Request) -> JSONResponse:
    services = _services(request)
    if services is None:
        return _unavailable()
    store, ws = services.store(), request.path_params["workspace_id"]
    found = await _owned_manifest(request, store, ws)
    if isinstance(found, JSONResponse):
        return found
    deleted = await run_in_threadpool(store.delete_workspace, ws)
    return JSONResponse({"deleted": ws, "deleted_objects": deleted})


# ---------------------------------------------------------------------------------------------------- files

async def file_session_endpoint(request: Request) -> JSONResponse:
    services = _services(request)
    if services is None:
        return _unavailable()
    store, ws = services.store(), request.path_params["workspace_id"]
    found = await _owned_manifest(request, store, ws)
    if isinstance(found, JSONResponse):
        return found
    manifest, generation = found
    try:
        body = await _json_body(request)
        safe_name = sanitize_filename(str(body.get("filename", "")))
        declared = int(body.get("size_bytes") or 0)
        if declared < 0:
            raise ValueError("size_bytes must be >= 0")
        origin = _origin_for_session(request)
    except ValueError as exc:
        return _error(str(exc), 400)
    file_id = new_file_id()
    session_uri = await run_in_threadpool(lambda: store.start_resumable(ws, file_id, safe_name, origin=origin))
    entry = {"id": file_id, "name": safe_name, "object": object_name(ws, file_id, safe_name), "status": "uploading",
             "declared_size_bytes": declared, "created_at": utc_now_iso()}
    manifest = {**manifest, "files": [*manifest.get("files", []), entry]}
    try:
        await run_in_threadpool(lambda: store.write_manifest(ws, manifest, if_generation=generation))
    except RuntimeError as exc:
        return _error(str(exc), 409)
    return JSONResponse({"file": _public_file(entry), "object": entry["object"], "session_uri": session_uri},
                        status_code=201)


def _validate_object(store: Any, name: str) -> tuple[dict[str, Any] | None, str]:
    """(PDF facts, '') when safe; (None, reason) when rejected. Raises UploadStoreError(404) if not uploaded."""
    with tempfile.TemporaryDirectory(prefix="rag_arena_upload_") as tmp:
        local = Path(tmp) / "upload.pdf"
        store.download_to(name, local)
        try:
            info = validate_pdf_file(local)
        except PdfRejected as exc:
            return None, str(exc)
    return {"status": "ready", "pages": info.pages, "size_bytes": info.size_bytes, "sha256": info.sha256}, ""


async def file_finalize_endpoint(request: Request) -> JSONResponse:
    services = _services(request)
    if services is None:
        return _unavailable()
    store, ws = services.store(), request.path_params["workspace_id"]
    found = await _owned_manifest(request, store, ws)
    if isinstance(found, JSONResponse):
        return found
    manifest, generation = found
    try:
        file_id = check_file_id(request.path_params["file_id"])
    except ValueError:
        return _error("file not found", 404)
    entry = next((f for f in manifest.get("files", []) if f.get("id") == file_id), None)
    if entry is None:
        return _error("file not found", 404)
    if await run_in_threadpool(store.object_meta, entry["object"]) is None:
        return _error("uploaded object not found: finish the upload to the session URI first", 409)
    try:
        facts, reason = await run_in_threadpool(_validate_object, store, entry["object"])
    except UploadStoreError as exc:
        return _error(f"could not read the uploaded file: {exc}", 502)
    if facts is None:
        await run_in_threadpool(store.delete_object, entry["object"])
        facts = {"status": "rejected", "error": reason}
    updated = {**entry, **facts, "finalized_at": utc_now_iso()}
    manifest = {**manifest, "files": [updated if f.get("id") == file_id else f for f in manifest["files"]]}
    try:
        await run_in_threadpool(lambda: store.write_manifest(ws, manifest, if_generation=generation))
    except RuntimeError as exc:
        return _error(str(exc), 409)
    if updated["status"] == "rejected":
        return _error(f"file rejected: {reason}", 422, file=_public_file(updated))
    return JSONResponse({"file": _public_file(updated)})


def _store_errors_as_json(endpoint: Any) -> Any:
    """Cloud Storage failures (incl. missing credentials) become a 502 JSON error naming the cause, never a bare 500."""
    async def wrapped(request: Request) -> JSONResponse:
        try:
            return await endpoint(request)
        except UploadStoreError as exc:
            return _error(str(exc), 502)
    wrapped.__name__ = endpoint.__name__
    return wrapped


def get_upload_routes() -> list[Route]:
    return [Route(r.path, endpoint=_store_errors_as_json(r.endpoint), methods=sorted(r.methods or []))
            for r in _raw_routes()]


def _raw_routes() -> list[Route]:
    return [
        Route("/api/v1/uploads/bucket", endpoint=bucket_status_endpoint, methods=["GET"]),
        Route("/api/v1/uploads/bucket", endpoint=bucket_create_endpoint, methods=["POST"]),
        Route("/api/v1/uploads", endpoint=workspaces_list_endpoint, methods=["GET"]),
        Route("/api/v1/uploads", endpoint=workspace_create_endpoint, methods=["POST"]),
        Route("/api/v1/uploads/{workspace_id}", endpoint=workspace_get_endpoint, methods=["GET"]),
        Route("/api/v1/uploads/{workspace_id}", endpoint=workspace_delete_endpoint, methods=["DELETE"]),
        Route("/api/v1/uploads/{workspace_id}/files", endpoint=file_session_endpoint, methods=["POST"]),
        Route("/api/v1/uploads/{workspace_id}/files/{file_id}/finalize", endpoint=file_finalize_endpoint,
              methods=["POST"]),
    ]


__all__ = ["ANONYMOUS", "IAP_EMAIL_HEADER", "UploadServices", "get_upload_routes"]
