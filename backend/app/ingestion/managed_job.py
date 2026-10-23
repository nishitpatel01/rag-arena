"""Level 5 (``managed_vertex``) ingestion: sync the local corpus to GCS, import into the Vertex AI Search data store,
verify the document count. Stages (emitted in this order):

``discover_docs`` (shared) → ``upload_gcs`` (only missing/changed PDFs) → ``import_documents`` (``documents:import``
INCREMENTAL from explicit ``gs://`` URIs, bounded LRO poll ≤ the job budget, success/failure counts + error samples
verbatim) → ``parse``/``chunk``/``embed``/``load_bigquery``/``build_indexes`` emitted as ``stage_skipped``
("managed by Vertex AI Search") → ``verify_counts`` (``documents.list``, paged, bounded).

Fail loud (spec P2): an import with any failed document, a missing expected document, or an HTTP error fails the stage.
Nothing is deleted (GCS objects or data store documents).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Any, Callable

from app.core.config import ArenaSettings
from app.core.telemetry import utc_now_iso
from app.core.vertex_search import (
    DiscoveryEngineClient,
    OperationFailedError,
    OperationTimeoutError,
    VaisResources,
    redact_resource_text,
)
from app.ingestion.gcs_sync import GCS_PREFIX, GcsClient, local_objects, sync_corpus
from app.ingestion.jobs import IngestJob, IngestJobRunner, _DiscoveredDoc
from app.recording.recorder import read_git_sha

TIER = "vertex_ai_search"
SKIP_REASON = "managed by Vertex AI Search"
SKIPPED_STAGES: tuple[str, ...] = ("parse", "chunk", "embed", "load_bigquery", "build_indexes")
IMPORT_POLL_SECONDS = 10.0
MAX_IMPORT_POLLS = 360
MAX_LIST_PAGES = 20
LIST_PAGE_SIZE = 1000
_MAX_ERROR_SAMPLES = 10


class ManagedImportError(RuntimeError):
    """documents:import reported failed documents (samples verbatim)."""


class ManagedVerifyError(RuntimeError):
    """The data store does not hold every expected corpus document after the import."""


@dataclass(frozen=True)
class ManagedBackends:
    gcs: GcsClient
    vais: DiscoveryEngineClient
    resources: VaisResources
    bucket: str
    write_ingest_run: Callable[[dict[str, Any]], None]


def managed_unavailable(settings: ArenaSettings) -> ManagedBackends:
    from app.ingestion.jobs import IngestBackendUnavailable

    raise IngestBackendUnavailable(
        f"DEMO_MODE={settings.demo_mode}: Vertex AI Search ingestion/status needs live|record mode (no GCP calls otherwise)"
    )


def _redact(text: str, backends: ManagedBackends) -> str:
    return redact_resource_text(text, backends.resources.project_id).replace(backends.bucket, "${GCS_CORPUS_BUCKET}")


def import_body(bucket: str, object_names: list[str]) -> dict[str, Any]:
    """Explicit URIs (≤ 100 per request for dataSchema=content) keep the import to exactly the discovered corpus."""
    if not object_names or len(object_names) > 100:
        raise ValueError(f"import needs 1..100 object names, got {len(object_names)}")
    if any(not n.startswith(GCS_PREFIX) for n in object_names):
        raise ValueError(f"every imported object must live under {GCS_PREFIX}")
    return {
        "gcsSource": {"inputUris": [f"gs://{bucket}/{n}" for n in object_names], "dataSchema": "content"},
        "reconciliationMode": "INCREMENTAL",
    }


def _counts(op: dict[str, Any]) -> dict[str, int]:
    meta = op.get("metadata") if isinstance(op.get("metadata"), dict) else {}
    return {k: int(meta.get(k, 0) or 0) for k in ("successCount", "failureCount", "totalCount")}


async def _upload_gcs(runner: IngestJobRunner, job: IngestJob, emitter: Any, tracker: Any, backends: ManagedBackends, docs: list[_DiscoveredDoc]) -> list[str]:
    request = {"bucket": "${GCS_CORPUS_BUCKET}", "prefix": GCS_PREFIX, "local_docs": len(docs), "compare": "md5Hash", "deletes": "never"}
    async with tracker.scope(emitter, "upload_gcs", "Sync corpus to Cloud Storage", "Cloud Storage JSON API", request) as sc:
        objs = await asyncio.to_thread(local_objects, runner.corpus_dir, [d.path for d in docs])
        result = await asyncio.to_thread(sync_corpus, backends.gcs, objs)
        sc.payload = {
            "uploaded": len(result.uploaded), "skipped": len(result.skipped), "bytes": result.bytes_uploaded,
            "uploaded_objects": list(result.uploaded), "reasons": [{"object": n, "reason": r} for n, r in result.reasons],
            "remote_objects_listed": result.remote_objects_listed, "list_pages": result.list_pages,
        }
    return [o.name for o in objs]


async def _poll_import(job: IngestJob, runner: IngestJobRunner, backends: ManagedBackends, op: dict[str, Any], deadline: float) -> tuple[dict[str, Any], int]:
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


async def _import_documents(runner: IngestJobRunner, job: IngestJob, emitter: Any, tracker: Any, backends: ManagedBackends, names: list[str], started: float) -> dict[str, int]:
    body = import_body(backends.bucket, names)
    shown = {**body, "gcsSource": {**body["gcsSource"], "inputUris": [u.replace(backends.bucket, "${GCS_CORPUS_BUCKET}") for u in body["gcsSource"]["inputUris"]]}}
    request = {"method": "POST", "endpoint": f"dataStores/{backends.resources.data_store_id}/branches/default_branch/documents:import", "body": shown}
    async with tracker.scope(emitter, "import_documents", "Import into Vertex AI Search", "Vertex AI Search · documents:import", request) as sc:
        op, _ = await asyncio.to_thread(backends.vais.request, "POST", f"{backends.resources.branch_path}/documents:import", body=body)
        deadline = started + float(runner.settings.ingest_job_timeout_seconds) - 30.0
        op, polls = await _poll_import(job, runner, backends, op, deadline)
        counts = _counts(op)
        response = op.get("response") if isinstance(op.get("response"), dict) else {}
        samples = [_redact(str(s), backends) for s in (response.get("errorSamples") or [])[:_MAX_ERROR_SAMPLES]]
        sc.payload = {
            "operation_id": str(op.get("name", "")).rsplit("/", 1)[-1], "polls": polls, "poll_interval_s": IMPORT_POLL_SECONDS,
            "success_count": counts["successCount"], "failure_count": counts["failureCount"], "total_count": counts["totalCount"],
            "error_samples": samples, "reconciliation_mode": "INCREMENTAL", "uris": len(names),
        }
        if counts["failureCount"] or samples:
            raise ManagedImportError(f"documents:import failed for {counts['failureCount']} document(s); error_samples={samples}")
    return counts


def list_documents(backends: ManagedBackends) -> tuple[list[dict[str, Any]], int]:
    """Every document of the default branch (≤ MAX_LIST_PAGES pages; more fails loud)."""
    docs: list[dict[str, Any]] = []
    token: str | None = None
    for page in range(1, MAX_LIST_PAGES + 1):
        params: dict[str, Any] = {"pageSize": LIST_PAGE_SIZE}
        if token:
            params["pageToken"] = token
        doc, _ = backends.vais.request("GET", f"{backends.resources.branch_path}/documents", params=params)
        docs.extend(doc.get("documents", []) or [])
        token = doc.get("nextPageToken")
        if not token:
            return docs, page
    raise ManagedVerifyError(f"documents.list exceeded {MAX_LIST_PAGES} pages")


async def _verify_counts(tracker: Any, emitter: Any, backends: ManagedBackends, names: list[str]) -> int:
    request = {"method": "GET", "endpoint": f"dataStores/{backends.resources.data_store_id}/branches/default_branch/documents", "pageSize": LIST_PAGE_SIZE}
    async with tracker.scope(emitter, "verify_counts", "Verify data store documents", "Vertex AI Search · documents.list", request) as sc:
        docs, pages = await asyncio.to_thread(list_documents, backends)
        uris = {str((d.get("content") or {}).get("uri", "")) for d in docs}
        expected = {f"gs://{backends.bucket}/{n}" for n in names}
        missing = sorted(u.replace(backends.bucket, "${GCS_CORPUS_BUCKET}") for u in expected - uris)
        sc.payload = {
            "document_count": len(docs), "expected": len(expected), "missing": missing, "pages": pages,
            "outside_corpus": len(uris - expected), "indexed": sum(1 for d in docs if d.get("indexTime")),
            "counts_by_tier": {TIER: len(docs)},
        }
        if missing:
            raise ManagedVerifyError(f"{len(missing)} corpus document(s) missing from the data store after import: {missing[:10]}")
    return len(docs)


async def run_managed_job(runner: IngestJobRunner, job: IngestJob, emitter: Any, tracker: Any, docs: list[_DiscoveredDoc]) -> None:
    """upload_gcs → import_documents → skipped BigQuery stages → verify_counts → ingest_runs row → job_complete."""
    started = time.monotonic()
    backends = runner.managed_factory(runner.settings)
    names = await _upload_gcs(runner, job, emitter, tracker, backends, docs)
    counts = await _import_documents(runner, job, emitter, tracker, backends, names, started)
    for stage in SKIPPED_STAGES:
        await emitter.skipped(stage, reason=SKIP_REASON)
    total = await _verify_counts(tracker, emitter, backends, names)
    job.counts_by_tier = {TIER: total}
    await asyncio.to_thread(backends.write_ingest_run, {
        "run_id": job.job_id, "level": job.level, "tier_profile": TIER, "started_at": job.created_at,
        "finished_at": utc_now_iso(), "status": "completed", "docs": len(docs), "pages": None, "chunks": None,
        "embedded": None, "loaded": counts["successCount"], "embedding_model": "managed (Vertex AI Search)",
        "layout_source": "vertex_ai_search (digital parser)", "git_sha": read_git_sha(),
        "stage_log": [r.to_dict() for r in emitter.records],
    })
    job.status = "completed"
    job.finished_at = utc_now_iso()
    await runner._put(job, {"event": "job_complete", "counts_by_tier": {TIER: total}, "chunks": 0})


async def managed_status(runner: IngestJobRunner) -> dict[str, Any]:
    """``/api/v1/ingest/status`` entry: data store existence + document count (gates the Level 5 Run button)."""
    backends = runner.managed_factory(runner.settings)
    store = await asyncio.to_thread(backends.vais.get_or_none, backends.resources.data_store_path)
    engine = await asyncio.to_thread(backends.vais.get_or_none, backends.resources.engine_path)
    if store is None:
        return {"available": True, "data_store_state": "NOT_FOUND", "engine_state": "NOT_FOUND" if engine is None else "ACTIVE",
                "document_count": 0, "data_store_id": backends.resources.data_store_id, "engine_id": backends.resources.engine_id}
    docs, _ = await asyncio.to_thread(list_documents, backends)
    return {
        "available": True, "data_store_state": "ACTIVE", "engine_state": "NOT_FOUND" if engine is None else "ACTIVE",
        "document_count": len(docs), "indexed": sum(1 for d in docs if d.get("indexTime")),
        "data_store_id": backends.resources.data_store_id, "engine_id": backends.resources.engine_id,
        "search_tier": ((engine or {}).get("searchEngineConfig") or {}).get("searchTier"),
        "search_add_ons": ((engine or {}).get("searchEngineConfig") or {}).get("searchAddOns", []),
    }


def live_managed_backends(settings: ArenaSettings) -> ManagedBackends:
    """Factory for live/record: REST clients for GCS + Discovery Engine, ingest_runs writer via the BigQuery loader."""
    from app.core.gcp_clients import _ensure_live_settings, get_access_token
    from app.ingestion.live_backends import BigQueryLiveLoader

    _ensure_live_settings(settings)
    if not settings.vais_data_store_id or not settings.vais_engine_id or not settings.gcs_corpus_bucket:
        raise RuntimeError("managed_vertex ingestion needs GCS_CORPUS_BUCKET, VAIS_DATA_STORE_ID and VAIS_ENGINE_ID in .env")
    token = lambda: get_access_token(settings)  # noqa: E731 - small closure
    resources = VaisResources(settings.gcp_project_id, settings.vais_location, settings.vais_data_store_id, settings.vais_engine_id)
    vais = DiscoveryEngineClient(project_id=settings.gcp_project_id, location=settings.vais_location, token_provider=token,
                                 attempt_timeout_seconds=float(settings.vais_timeout_seconds), budget_seconds=float(settings.vais_timeout_seconds) * 3)
    gcs = GcsClient(bucket=settings.gcs_corpus_bucket, token_provider=token, timeout_seconds=60.0)
    return ManagedBackends(gcs=gcs, vais=vais, resources=resources, bucket=settings.gcs_corpus_bucket,
                           write_ingest_run=BigQueryLiveLoader(settings).write_ingest_run)


__all__ = [
    "ManagedBackends",
    "ManagedImportError",
    "ManagedVerifyError",
    "SKIPPED_STAGES",
    "SKIP_REASON",
    "TIER",
    "import_body",
    "list_documents",
    "live_managed_backends",
    "managed_status",
    "managed_unavailable",
    "run_managed_job",
]
