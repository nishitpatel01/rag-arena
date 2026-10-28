"""Level 5 (``managed_vertex``) upload ingestion (v1.1 step 4): zero-download JSONL metadata import into the dedicated
``rag-arena-uploads`` Vertex AI Search data store, followed by point ``documents.get`` verification.

Unlike L1–L3, uploaded PDFs already live in ``gs://<UPLOAD_BUCKET>/uploads/<ws>/<file_id>-<safe_name>`` and the
Discovery Engine service agent has ``roles/storage.objectViewer`` on ``<UPLOAD_BUCKET>``. We therefore never download
PDF bytes to local disk:

1. ``discover_docs``: build ``dataSchema=document`` records carrying ``structData.{workspace_id, doc_name}`` and
   deterministic id ``upl_<12hex>_f_<12hex>`` (0 bytes downloaded).
2. ``upload_gcs``: write ``uploads/<ws>/_vais_import.jsonl`` to ``<UPLOAD_BUCKET>``.
3. ``import_documents``: ``POST .../documents:import`` (``INCREMENTAL``, ``dataSchema="document"``) with bounded LRO
   polling and bucket/project redaction; fail loud on any ``failureCount`` or ``errorSamples``.
4. ``parse`` / ``chunk`` / ``embed`` / ``load_bigquery`` / ``build_indexes``: emitted as ``stage_skipped``.
5. ``verify_counts``: point ``GET .../documents/{id}`` per expected document (never ``documents.list``, which would
   enumerate other workspaces' documents) and assert ``structData.workspace_id == source.workspace_id``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Any, Callable, Sequence

from app.core.config import ArenaSettings
from app.core.telemetry import utc_now_iso
from app.core.vertex_search import (
    DiscoveryEngineClient,
    OperationFailedError,
    OperationTimeoutError,
    VaisResources,
    redact_resource_text,
)
from app.ingestion.jobs import IngestJob, IngestJobRunner, UploadSource
from app.ingestion.managed_job import (
    IMPORT_POLL_SECONDS,
    MAX_IMPORT_POLLS,
    ManagedImportError,
    ManagedVerifyError,
    SKIP_REASON,
    SKIPPED_STAGES,
    TIER,
)
from app.ingestion.uploads import (
    UploadStore,
    check_workspace_id,
    parse_upload_object,
    upload_doc_id,
    vais_manifest_object,
)
from app.recording.recorder import read_git_sha

MAX_UPLOAD_IMPORT_DOCS = 10_000
_MAX_ERROR_SAMPLES = 10


@dataclass(frozen=True)
class ManagedUploadBackends:
    store: Any
    vais: DiscoveryEngineClient
    resources: VaisResources
    write_ingest_run: Callable[[dict[str, Any]], None]


def managed_upload_unavailable(settings: ArenaSettings) -> ManagedUploadBackends:
    from app.ingestion.jobs import IngestBackendUnavailable

    raise IngestBackendUnavailable(
        f"DEMO_MODE={settings.demo_mode}: Vertex AI Search upload ingestion needs live|record mode (no GCP calls otherwise)"
    )


def _bucket_of(backends: ManagedUploadBackends) -> str:
    bucket = str(getattr(backends.store, "bucket", "") or "").strip()
    if not bucket or "/" in bucket:
        raise ValueError("upload store must expose a bare bucket name")
    return bucket


def _redact(text: str, backends: ManagedUploadBackends) -> str:
    bucket = str(getattr(backends.store, "bucket", "") or "").strip()
    out = redact_resource_text(text, backends.resources.project_id)
    return out.replace(bucket, "${UPLOAD_BUCKET}") if bucket else out


def build_upload_import_records(
    workspace_id: str, bucket: str, files: Sequence[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Build ``dataSchema=document`` JSONL records pointing at the uploaded GCS objects with filterable ``structData``."""
    check_workspace_id(workspace_id)
    if not isinstance(bucket, str) or not bucket.strip() or "/" in bucket:
        raise ValueError("bucket must be a bare bucket name")
    if not files or len(files) > MAX_UPLOAD_IMPORT_DOCS:
        raise ValueError(f"upload import needs 1..{MAX_UPLOAD_IMPORT_DOCS} files, got {len(files)}")
    bkt = bucket.strip()
    records: list[dict[str, Any]] = []
    for obj, safe_name in files:
        file_id, parsed_name = parse_upload_object(workspace_id, obj)
        if parsed_name != safe_name:
            raise ValueError(f"object {obj!r} filename {parsed_name!r} does not match {safe_name!r}")
        records.append({
            "id": upload_doc_id(workspace_id, file_id),
            "structData": {"workspace_id": workspace_id, "doc_name": safe_name},
            "content": {"mimeType": "application/pdf", "uri": f"gs://{bkt}/{obj}"},
        })
    return records


def upload_import_body(bucket: str, manifest_obj: str) -> dict[str, Any]:
    """``documents:import`` request body reading the workspace's ``_vais_import.jsonl`` manifest."""
    if not isinstance(bucket, str) or not bucket.strip() or "/" in bucket:
        raise ValueError("bucket must be a bare bucket name")
    parts = manifest_obj.split("/") if isinstance(manifest_obj, str) else []
    if len(parts) != 3 or vais_manifest_object(parts[1]) != manifest_obj:
        raise ValueError(f"manifest_obj must look like uploads/<upl_id>/_vais_import.jsonl, got {manifest_obj!r}")
    return {
        "gcsSource": {"inputUris": [f"gs://{bucket.strip()}/{manifest_obj}"], "dataSchema": "document"},
        "reconciliationMode": "INCREMENTAL",
    }


def _counts(op: dict[str, Any]) -> dict[str, int]:
    meta = op.get("metadata") if isinstance(op.get("metadata"), dict) else {}
    return {k: int(meta.get(k, 0) or 0) for k in ("successCount", "failureCount", "totalCount")}


async def _poll_import(
    job: IngestJob, runner: IngestJobRunner, backends: ManagedUploadBackends, op: dict[str, Any], deadline: float
) -> tuple[dict[str, Any], int]:
    polls = 0
    while not op.get("done"):
        if polls >= MAX_IMPORT_POLLS or time.monotonic() + IMPORT_POLL_SECONDS > deadline:
            raise OperationTimeoutError(f"documents:import not done after {polls} polls (job budget)")
        await asyncio.sleep(IMPORT_POLL_SECONDS)
        op, _ = await asyncio.to_thread(backends.vais.request, "GET", op["name"])
        polls += 1
        await runner._put(job, {"event": "import_progress", "stage": "import_documents", "poll": polls, **_counts(op)})
    if isinstance(op.get("error"), dict) and op["error"]:
        raise OperationFailedError(_redact(f"documents:import failed: {op['error']}", backends))
    return op, polls


async def _discover_upload_records(
    emitter: Any, tracker: Any, runner: IngestJobRunner, source: UploadSource
) -> tuple[ManagedUploadBackends, list[dict[str, Any]]]:
    request = {"workspace": source.workspace_id, "files": len(source.files), "download_bytes": 0}
    async with tracker.scope(emitter, "discover_docs", "Prepare upload metadata", "Cloud Storage · workspace manifest", request) as sc:
        backends = runner.upload_managed_factory(runner.settings)
        records = build_upload_import_records(source.workspace_id, _bucket_of(backends), source.files)
        sc.payload = {
            "docs": len(records), "doc_names": [r["structData"]["doc_name"] for r in records[:50]],
            "bytes": 0, "source": "upload workspace", "workspace": source.workspace_id,
        }
    return backends, records


async def _write_manifest_gcs(
    emitter: Any, tracker: Any, backends: ManagedUploadBackends, workspace_id: str, records: list[dict[str, Any]]
) -> str:
    target = vais_manifest_object(workspace_id)
    request = {"bucket": "${UPLOAD_BUCKET}", "object": target, "records": len(records)}
    async with tracker.scope(emitter, "upload_gcs", "Write JSONL import manifest", "Cloud Storage JSON API", request) as sc:
        written = await asyncio.to_thread(backends.store.write_jsonl_manifest, workspace_id, records)
        sc.payload = {"manifest_object": written, "records": len(records), "uploaded": 1, "skipped": 0}
    return written


async def _import_upload_docs(
    runner: IngestJobRunner, job: IngestJob, emitter: Any, tracker: Any,
    backends: ManagedUploadBackends, manifest_obj: str, expected_docs: int, started: float,
) -> dict[str, int]:
    bucket = _bucket_of(backends)
    body = upload_import_body(bucket, manifest_obj)
    shown = {
        **body,
        "gcsSource": {
            **body["gcsSource"],
            "inputUris": [u.replace(bucket, "${UPLOAD_BUCKET}") for u in body["gcsSource"]["inputUris"]],
        },
    }
    endpoint = f"dataStores/{backends.resources.data_store_id}/branches/default_branch/documents:import"
    request = {"method": "POST", "endpoint": endpoint, "body": shown}
    async with tracker.scope(emitter, "import_documents", "Import into Vertex AI Search", "Vertex AI Search · documents:import", request) as sc:
        op, _ = await asyncio.to_thread(backends.vais.request, "POST", f"{backends.resources.branch_path}/documents:import", body=body)
        deadline = started + float(runner.settings.ingest_job_timeout_seconds) - 30.0
        op, polls = await _poll_import(job, runner, backends, op, deadline)
        counts = _counts(op)
        response = op.get("response") if isinstance(op.get("response"), dict) else {}
        samples = [_redact(str(s), backends) for s in (response.get("errorSamples") or [])[:_MAX_ERROR_SAMPLES]]
        sc.payload = {
            "operation_id": str(op.get("name", "")).rsplit("/", 1)[-1], "polls": polls,
            "poll_interval_s": IMPORT_POLL_SECONDS, "success_count": counts["successCount"],
            "failure_count": counts["failureCount"], "total_count": counts["totalCount"],
            "error_samples": samples, "reconciliation_mode": "INCREMENTAL", "uris": expected_docs,
        }
        if counts["failureCount"] or samples:
            raise ManagedImportError(f"documents:import failed for {counts['failureCount']} document(s); error_samples={samples}")
    return counts


def _fetch_workspace_docs(
    backends: ManagedUploadBackends, workspace_id: str, records: list[dict[str, Any]]
) -> tuple[int, int]:
    indexed = 0
    for rec in records[:MAX_UPLOAD_IMPORT_DOCS]:
        doc_id = str(rec["id"])
        doc = backends.vais.get_or_none(f"{backends.resources.branch_path}/documents/{doc_id}")
        if doc is None:
            raise ManagedVerifyError(f"uploaded document {doc_id} missing from {backends.resources.data_store_id} after import")
        actual_ws = (doc.get("structData") or {}).get("workspace_id")
        if actual_ws != workspace_id:
            raise ManagedVerifyError(
                f"uploaded document {doc_id} has structData.workspace_id={actual_ws!r}, expected {workspace_id!r}"
            )
        if doc.get("indexTime"):
            indexed += 1
    return len(records), indexed


async def _verify_upload_docs(
    emitter: Any, tracker: Any, backends: ManagedUploadBackends, workspace_id: str, records: list[dict[str, Any]]
) -> int:
    endpoint = f"dataStores/{backends.resources.data_store_id}/branches/default_branch/documents/{{id}}"
    request = {"method": "GET", "endpoint": endpoint, "expected_docs": len(records), "workspace": workspace_id}
    async with tracker.scope(emitter, "verify_counts", "Verify uploaded documents", "Vertex AI Search · documents.get", request) as sc:
        total, indexed = await asyncio.to_thread(_fetch_workspace_docs, backends, workspace_id, records)
        sc.payload = {
            "document_count": total, "expected": len(records), "missing": [], "indexed": indexed,
            "workspace": workspace_id, "counts_by_tier": {TIER: total},
        }
    return total


async def run_managed_upload_job(
    runner: IngestJobRunner, job: IngestJob, emitter: Any, tracker: Any, source: UploadSource
) -> None:
    """discover_docs → upload_gcs → import_documents → skipped BQ stages → verify_counts → ingest_runs → job_complete."""
    started = time.monotonic()
    backends, records = await _discover_upload_records(emitter, tracker, runner, source)
    manifest_obj = await _write_manifest_gcs(emitter, tracker, backends, source.workspace_id, records)
    counts = await _import_upload_docs(runner, job, emitter, tracker, backends, manifest_obj, len(records), started)
    for stage in SKIPPED_STAGES:
        await emitter.skipped(stage, reason=SKIP_REASON)
    total = await _verify_upload_docs(emitter, tracker, backends, source.workspace_id, records)
    job.counts_by_tier = {TIER: total}
    await asyncio.to_thread(backends.write_ingest_run, {
        "run_id": job.job_id, "level": job.level, "tier_profile": TIER, "started_at": job.created_at,
        "finished_at": utc_now_iso(), "status": "completed", "docs": len(records), "pages": None, "chunks": None,
        "embedded": None, "loaded": counts["successCount"], "embedding_model": "managed (Vertex AI Search)",
        "layout_source": "vertex_ai_search (digital parser)", "git_sha": read_git_sha(),
        "stage_log": [r.to_dict() for r in emitter.records],
    })
    job.status = "completed"
    job.finished_at = utc_now_iso()
    await runner._put(job, {"event": "job_complete", "counts_by_tier": {TIER: total}, "chunks": 0})


def live_managed_upload_backends(settings: ArenaSettings) -> ManagedUploadBackends:
    """Factory for live/record upload ingestion against ``VAIS_UPLOADS_DATA_STORE_ID`` + ``UPLOAD_BUCKET``."""
    from app.core.gcp_clients import _ensure_live_settings, get_access_token
    from app.ingestion.live_backends import BigQueryLiveLoader

    _ensure_live_settings(settings)
    if not settings.upload_bucket or not settings.vais_uploads_data_store_id or not settings.vais_uploads_engine_id:
        raise RuntimeError(
            "managed_vertex upload ingestion needs UPLOAD_BUCKET (or GCP_PROJECT_ID), "
            "VAIS_UPLOADS_DATA_STORE_ID and VAIS_UPLOADS_ENGINE_ID in .env"
        )
    token = lambda: get_access_token(settings)  # noqa: E731 - small closure
    resources = VaisResources(
        settings.gcp_project_id, settings.vais_location,
        settings.vais_uploads_data_store_id, settings.vais_uploads_engine_id,
    )
    vais = DiscoveryEngineClient(
        project_id=settings.gcp_project_id, location=settings.vais_location, token_provider=token,
        attempt_timeout_seconds=float(settings.vais_timeout_seconds),
        budget_seconds=float(settings.vais_timeout_seconds) * 3,
    )
    store = UploadStore(bucket=settings.upload_bucket, token_provider=token, timeout_seconds=60.0)
    return ManagedUploadBackends(
        store=store, vais=vais, resources=resources,
        write_ingest_run=BigQueryLiveLoader(settings).write_ingest_run,
    )


__all__ = [
    "MAX_UPLOAD_IMPORT_DOCS",
    "ManagedUploadBackends",
    "build_upload_import_records",
    "live_managed_upload_backends",
    "managed_upload_unavailable",
    "run_managed_upload_job",
    "upload_import_body",
]
