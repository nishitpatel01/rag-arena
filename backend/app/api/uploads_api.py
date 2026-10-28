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
    upload_doc_id,
    validate_pdf_file,
)

IAP_EMAIL_HEADER = "x-goog-authenticated-user-email"
ANONYMOUS = "anonymous"
MANIFEST_VERSION = 1
BUCKET_TIMEOUT_SECONDS = 30.0
STORE_TIMEOUT_SECONDS = 60.0
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
MANIFEST_WRITE_ATTEMPTS = 3
# Level 3 cost estimate. Document AI Layout Parser is billed per page (exact list price in multimodal_job). Gemini
# vision runs only on diagram pages, which are unknown before parsing, so the confirmed amount is the UPPER bound
# "every page is a diagram", priced with these per-page token assumptions (Phase 3 fakes/measurements: ≈1.7k image+
# prompt tokens in, ≈0.8k output+thinking tokens out per page).
VISION_TOKENS_IN_PER_PAGE = 1_800
VISION_TOKENS_OUT_PER_PAGE = 900
CONFIRM_LEVELS: frozenset[str] = frozenset({"level3_multimodal"})
# Level 4's agent searches the Level 3 rows, so a workspace is ready for L4 once L3 ingestion completed.
DATA_LEVEL_OF: dict[str, str] = {"level4_agentic": "level3_multimodal"}
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

    def delete_vais_docs(self, doc_ids: list[str]) -> int:
        """Delete deterministic upload documents from the Vertex AI Search uploads store (404 = already absent)."""
        from app.core.vertex_search import DiscoveryEngineClient, VaisResources

        if not doc_ids:
            return 0
        s = self._settings
        if not s.vais_uploads_data_store_id:
            raise RuntimeError("VAIS_UPLOADS_DATA_STORE_ID is not configured; cannot delete Vertex AI Search documents")
        resources = VaisResources(s.gcp_project_id, s.vais_location, s.vais_uploads_data_store_id, s.vais_uploads_engine_id)
        client = DiscoveryEngineClient(
            project_id=s.gcp_project_id, location=s.vais_location, token_provider=self._token,
            attempt_timeout_seconds=float(s.vais_timeout_seconds), budget_seconds=float(s.vais_timeout_seconds) * 3,
        )
        return sum(1 for doc_id in doc_ids if client.delete_if_exists(f"{resources.branch_path}/documents/{doc_id}"))


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


def _vais_doc_ids_for_delete(ws: str, manifest: dict[str, Any]) -> list[str]:
    levels = manifest.get("levels") or {}
    if "managed_vertex" not in levels:
        return []
    seen: list[str] = []
    for fid in [*(f.get("id") for f in manifest.get("files", [])), *((levels.get("managed_vertex") or {}).get("file_ids") or [])]:
        if isinstance(fid, str) and fid and fid not in seen:
            seen.append(fid)
    return [upload_doc_id(ws, fid) for fid in seen]


async def workspace_delete_endpoint(request: Request) -> JSONResponse:
    """BigQuery rows first, then Vertex AI Search upload docs, then the bucket prefix: failures leave state to retry."""
    services = _services(request)
    if services is None:
        return _unavailable()
    store, ws = services.store(), request.path_params["workspace_id"]
    found = await _owned_manifest(request, store, ws)
    if isinstance(found, JSONResponse):
        return found
    manifest, _ = found
    runner = request.app.state.ingest_runner
    try:
        loader = runner.backend_factory(runner.settings).loader
        await run_in_threadpool(loader.delete_scope, ws)
    except Exception as exc:  # boundary: reported, objects are NOT deleted so the user can retry
        return _error(f"could not delete the workspace's BigQuery rows ({type(exc).__name__}: {exc}); nothing was deleted", 502)
    doc_ids = _vais_doc_ids_for_delete(ws, manifest)
    deleted_vais = 0
    if doc_ids:
        try:
            deleted_vais = await run_in_threadpool(services.delete_vais_docs, doc_ids)
        except Exception as exc:  # boundary: reported, GCS manifest + objects remain so the user can retry
            return _error(
                f"could not delete the workspace's Vertex AI Search documents ({type(exc).__name__}: {exc}); "
                "workspace files were kept so you can retry",
                502,
            )
    deleted = await run_in_threadpool(store.delete_workspace, ws)
    return JSONResponse({
        "deleted": ws,
        "deleted_objects": deleted,
        "bigquery": {"deleted_scope": ws},
        "vertex_ai_search": {"deleted_docs": deleted_vais},
    })


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
    if any(f.get("name") == safe_name and f.get("status") != "rejected" for f in manifest.get("files", [])):
        return _error(f"a file named {safe_name} already exists in this workspace", 409)
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


# ---------------------------------------------------------------------------------------------------- ingestion

def missing_upload_levels(manifest: dict[str, Any], engines: list[str]) -> list[str]:
    """Engines whose data level has not completed ingestion for this workspace (L4 reads L3 data)."""
    levels = manifest.get("levels", {}) or {}
    return [e for e in engines if (levels.get(DATA_LEVEL_OF.get(e, e)) or {}).get("status") != "completed"]


async def upload_scope_error(request: Request, scope: str, engines: list[str]) -> JSONResponse | None:
    """None when ``scope`` is the caller's workspace and every engine's level is ingested; else the error response."""
    services = _services(request)
    if services is None:
        return _unavailable()
    found = await _owned_manifest(request, services.store(), scope)
    if isinstance(found, JSONResponse):
        return found
    missing = missing_upload_levels(found[0], engines)
    if missing:
        return _error(f"workspace {scope} is not ingested for: {', '.join(missing)} (run its ingestion first)", 400)
    return None


def _level_recorder(store: Any, ws: str, level: str, file_ids: list[str]) -> Any:
    """on_finish callback: write the job outcome into the manifest (bounded retries on concurrent changes)."""
    def record(job: Any) -> None:
        for attempt in range(1, MANIFEST_WRITE_ATTEMPTS + 1):
            found = store.read_manifest(ws)
            if found is None:
                raise RuntimeError(f"workspace {ws} disappeared during ingestion")
            manifest, generation = found
            entry = {"status": job.status, "job_id": job.job_id, "chunks": job.chunks, "tier_profile": job.tier_profile,
                     "file_ids": file_ids, "error": job.error, "finished_at": job.finished_at or utc_now_iso()}
            try:
                store.write_manifest(ws, {**manifest, "levels": {**manifest.get("levels", {}), level: entry}},
                                     if_generation=generation)
                return
            except RuntimeError:
                if attempt == MANIFEST_WRITE_ATTEMPTS:
                    raise
    return record


def estimate_cost(level: str, pages: int, vision_model: str) -> dict[str, Any]:
    """Pre-ingest cost estimate for an upload workspace; Level 3 must be confirmed against ``max_usd``."""
    from app.core.pricing import PRICE_TABLE
    from app.ingestion.multimodal_job import DOCAI_USD_PER_PAGE

    if level == "managed_vertex":
        return {"level": level, "pages": pages, "requires_confirmation": False, "max_usd": 0.0,
                "basis": "Vertex AI Search digital parser + indexing: free under 10 GiB data store tier"}
    if level not in CONFIRM_LEVELS:
        return {"level": level, "pages": pages, "requires_confirmation": False, "max_usd": 0.0,
                "basis": "embeddings only (gemini-embedding-2): fractions of a cent for typical documents"}
    price = PRICE_TABLE.get(vision_model)
    if price is None:
        raise ValueError(f"no list price for vision model {vision_model}; cannot estimate Level 3 cost")
    per_page = (VISION_TOKENS_IN_PER_PAGE * price.input_usd_per_1m + VISION_TOKENS_OUT_PER_PAGE * price.output_usd_per_1m) / 1e6
    docai = round(pages * DOCAI_USD_PER_PAGE, 4)
    vision_max = round(pages * per_page, 4)
    return {
        "level": level, "pages": pages, "requires_confirmation": True, "vision_model": vision_model,
        "documentai_usd": docai, "vision_max_usd": vision_max, "max_usd": round(docai + vision_max, 4),
        "basis": (f"Document AI Layout Parser ${DOCAI_USD_PER_PAGE * 1000:.0f}/1,000 pages (exact) + Gemini vision on "
                  "diagram pages only; the maximum assumes every page is a diagram"),
    }


def _ready_pages(manifest: dict[str, Any]) -> int:
    return sum(int(f.get("pages") or 0) for f in manifest.get("files", []) if f.get("status") == "ready")


async def workspace_estimate_endpoint(request: Request) -> JSONResponse:
    services = _services(request)
    if services is None:
        return _unavailable()
    found = await _owned_manifest(request, services.store(), request.path_params["workspace_id"])
    if isinstance(found, JSONResponse):
        return found
    level = request.query_params.get("level", "").strip()
    try:
        estimate = estimate_cost(level, _ready_pages(found[0]), request.app.state.settings.vision_model)
    except ValueError as exc:
        return _error(str(exc), 400)
    return JSONResponse({"estimate": estimate})


async def workspace_ingest_endpoint(request: Request) -> JSONResponse:
    from app.ingestion.jobs import UPLOAD_LEVELS, UploadSource

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
        level = str(body.get("level", "")).strip()
        confirmed = float(body.get("confirm_cost_usd") or 0)
    except (ValueError, TypeError) as exc:
        return _error(f"invalid request: {exc}", 400)
    if level not in UPLOAD_LEVELS:
        return _error(f"{level or 'level'} ingestion is not available for uploads yet (available: {', '.join(UPLOAD_LEVELS)})", 400)
    ready = [f for f in manifest.get("files", []) if f.get("status") == "ready"]
    if not ready:
        return _error("this workspace has no ready files (upload and finalize at least one PDF)", 409)
    if (manifest.get("levels", {}).get(level) or {}).get("status") in ("starting", "running"):
        return _error(f"{level} is already being ingested for this workspace", 409)
    if level in CONFIRM_LEVELS:
        try:
            estimate = estimate_cost(level, _ready_pages(manifest), request.app.state.settings.vision_model)
        except ValueError as exc:
            return _error(str(exc), 400)
        if confirmed + 1e-9 < estimate["max_usd"]:
            return _error(f"{level} costs up to ${estimate['max_usd']:.4f} for this workspace; confirm it to start",
                          409, needs_confirmation=True, estimate=estimate)
    file_ids = [f["id"] for f in ready]
    starting = {**manifest, "levels": {**manifest.get("levels", {}), level: {"status": "starting", "file_ids": file_ids,
                                                                             "started_at": utc_now_iso()}}}
    try:
        await run_in_threadpool(lambda: store.write_manifest(ws, starting, if_generation=generation))
    except RuntimeError as exc:
        return _error(str(exc), 409)
    source = UploadSource(workspace_id=ws, files=tuple((f["object"], f["name"]) for f in ready),
                          fetch=store.download_to, on_finish=_level_recorder(store, ws, level, file_ids))
    job = request.app.state.ingest_runner.start(level, upload=source)
    return JSONResponse({"job": job.to_dict(), "level": level, "files": len(ready)}, status_code=202)


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
        Route("/api/v1/uploads/{workspace_id}/ingest", endpoint=workspace_ingest_endpoint, methods=["POST"]),
        Route("/api/v1/uploads/{workspace_id}/estimate", endpoint=workspace_estimate_endpoint, methods=["GET"]),
        Route("/api/v1/uploads/{workspace_id}/files/{file_id}/finalize", endpoint=file_finalize_endpoint,
              methods=["POST"]),
    ]


__all__ = ["ANONYMOUS", "IAP_EMAIL_HEADER", "UploadServices", "get_upload_routes", "missing_upload_levels",
           "upload_scope_error"]
