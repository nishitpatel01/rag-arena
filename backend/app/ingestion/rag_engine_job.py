"""Level 6 (``rag_engine``) ingestion: benchmark corpus + isolated per-upload-workspace ``RagCorpus``.

Stages (emitted in order):
* Benchmark corpus (``job.scope is None``):
  ``discover_docs`` (shared) → ``upload_gcs`` (md5 diff sync to ``GCS_CORPUS_BUCKET``) → ``import_documents``
  (sequential batches of ≤ 25 explicit ``gs://`` URIs via ``ragFiles:import``, bounded LRO polling, fail loud on
  any ``failedRagFilesCount``) → ``parse``/``chunk``/``embed``/``load_bigquery``/``build_indexes`` emitted as
  ``stage_skipped`` ("managed by Vertex AI RAG Engine") → ``verify_counts`` (``ragFiles.list``, checking every URI
  exists and is ``ACTIVE``).
* Upload workspace (``job.scope = upl_<12hex>``):
  0 bytes downloaded locally; creates/reuses an isolated ``RagCorpus`` named ``rag-arena-upl_<12hex>`` (never touching
  ``ragEngineConfig`` or neighbor corpora), stamps ``job.extra_manifest["rag_corpus_id"]`` so ``DELETE /uploads/{ws}``
  always cleans up the corpus, imports ``gs://<UPLOAD_BUCKET>/uploads/<ws>/<file_id>-<safe_name>``, and verifies via
  ``ragFiles.list``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Any, Callable, Sequence

from app.core.config import ArenaSettings
from app.core.rag_engine_client import (
    OperationFailedError,
    OperationTimeoutError,
    RagEngineResources,
    VertexRagClient,
    classify_engine_config,
    corpus_body,
    extract_short_corpus_id,
    redact_resource_text,
)
from app.core.telemetry import utc_now_iso
from app.ingestion.gcs_sync import GCS_PREFIX, GcsClient, local_objects, sync_corpus
from app.ingestion.jobs import IngestJob, IngestJobRunner, UploadSource, _DiscoveredDoc
from app.ingestion.uploads import UploadStore, check_workspace_id, parse_upload_object
from app.recording.recorder import read_git_sha

TIER = "rag_engine"
SKIP_REASON = "managed by Vertex AI RAG Engine"
SKIPPED_STAGES: tuple[str, ...] = ("parse", "chunk", "embed", "load_bigquery", "build_indexes")
IMPORT_POLL_SECONDS = 5.0
MAX_IMPORT_POLLS = 360
MAX_URIS_PER_IMPORT = 25
MAX_UPLOAD_IMPORT_DOCS = 10_000
CORPUS_DISPLAY_NAME = "rag-arena-procurement"
UPLOAD_CORPUS_PREFIX = "rag-arena-"
_CORPUS_CREATE_DEADLINE_S = 300.0


class RagEngineImportError(RuntimeError):
    """``ragFiles:import`` reported failed files."""


class RagEngineVerifyError(RuntimeError):
    """The target ``RagCorpus`` is missing expected files or holds ``ERROR``-state files after import."""


@dataclass(frozen=True)
class RagEngineBackends:
    gcs: GcsClient
    rag: VertexRagClient
    resources: RagEngineResources
    bucket: str
    write_ingest_run: Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class RagEngineUploadBackends:
    store: Any
    rag: VertexRagClient
    resources: RagEngineResources
    write_ingest_run: Callable[[dict[str, Any]], None]


def rag_engine_unavailable(settings: ArenaSettings) -> RagEngineBackends:
    from app.ingestion.jobs import IngestBackendUnavailable

    raise IngestBackendUnavailable(
        f"DEMO_MODE={settings.demo_mode}: Vertex AI RAG Engine ingestion/status needs live|record mode (no GCP calls otherwise)"
    )


def rag_engine_upload_unavailable(settings: ArenaSettings) -> RagEngineUploadBackends:
    from app.ingestion.jobs import IngestBackendUnavailable

    raise IngestBackendUnavailable(
        f"DEMO_MODE={settings.demo_mode}: Vertex AI RAG Engine upload ingestion needs live|record mode (no GCP calls otherwise)"
    )


def upload_corpus_display_name(workspace_id: str) -> str:
    return f"{UPLOAD_CORPUS_PREFIX}{check_workspace_id(workspace_id)}"


def _redact(text: str, project_id: str, *, corpus_bucket: str = "", upload_bucket: str = "", corpus_id: str = "") -> str:
    pairs = [
        (upload_bucket.strip(), "${UPLOAD_BUCKET}"),
        (corpus_bucket.strip(), "${GCS_CORPUS_BUCKET}"),
        (corpus_id.strip(), "${RAG_ENGINE_CORPUS_ID}"),
        (project_id.strip(), "${GCP_PROJECT_ID}"),
    ]
    out = text
    for raw, placeholder in sorted((p for p in pairs if p[0]), key=lambda item: -len(item[0])):
        out = out.replace(raw, placeholder)
    return redact_resource_text(out, project_id)


def import_rag_files_body(
    uris: Sequence[str],
    *,
    chunk_size: int,
    chunk_overlap: int,
    max_embedding_requests_per_min: int = 1000,
) -> dict[str, Any]:
    """REST v1 ``ImportRagFilesRequest`` JSON body for 1..25 explicit ``gs://`` URIs."""
    if not uris or len(uris) > MAX_URIS_PER_IMPORT:
        raise ValueError(f"ragFiles:import batch needs 1..{MAX_URIS_PER_IMPORT} URIs, got {len(uris)}")
    if any(not isinstance(u, str) or not u.startswith("gs://") for u in uris):
        raise ValueError("every ragFiles:import URI must start with 'gs://'")
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or not 64 <= chunk_size <= 2048:
        raise ValueError(f"chunk_size must be in [64, 2048], got {chunk_size!r}")
    if not isinstance(chunk_overlap, int) or isinstance(chunk_overlap, bool) or not 0 <= chunk_overlap < chunk_size:
        raise ValueError(f"chunk_overlap must be in [0, chunk_size), got {chunk_overlap!r}")
    return {
        "importRagFilesConfig": {
            "gcsSource": {"uris": list(uris)},
            "ragFileTransformationConfig": {
                "ragFileChunkingConfig": {
                    "fixedLengthChunking": {
                        "chunkSize": chunk_size,
                        "chunkOverlap": chunk_overlap,
                    }
                }
            },
            "maxEmbeddingRequestsPerMin": int(max_embedding_requests_per_min),
        }
    }


def _import_counts(op: dict[str, Any]) -> dict[str, int]:
    resp = op.get("response") if isinstance(op.get("response"), dict) else {}
    return {
        "imported": int(resp.get("importedRagFilesCount", 0) or 0),
        "skipped": int(resp.get("skippedRagFilesCount", 0) or 0),
        "failed": int(resp.get("failedRagFilesCount", 0) or 0),
    }


async def _poll_import_op(
    job: IngestJob,
    runner: IngestJobRunner,
    rag: VertexRagClient,
    op: dict[str, Any],
    deadline: float,
    redact_fn: Callable[[str], str],
    batch_index: int,
) -> tuple[dict[str, Any], int]:
    polls = 0
    while not op.get("done"):
        if polls >= MAX_IMPORT_POLLS or time.monotonic() + IMPORT_POLL_SECONDS > deadline:
            raise OperationTimeoutError(f"ragFiles:import batch {batch_index} not done after {polls} polls (job budget)")
        await asyncio.sleep(IMPORT_POLL_SECONDS)
        op, _ = await asyncio.to_thread(rag.request, "GET", op["name"])
        polls += 1
        await runner._put(job, {"event": "import_progress", "stage": "import_documents", "batch": batch_index, "poll": polls})
    if isinstance(op.get("error"), dict) and op["error"]:
        raise OperationFailedError(redact_fn(f"ragFiles:import failed: {op['error']}"))
    return op, polls


async def _run_batched_imports(
    runner: IngestJobRunner,
    job: IngestJob,
    emitter: Any,
    tracker: Any,
    *,
    rag: VertexRagClient,
    corpus_path: str,
    endpoint_label: str,
    uris: list[str],
    redact_fn: Callable[[str], str],
    started: float,
) -> dict[str, int]:
    chunk_size = runner.settings.rag_engine_chunk_size
    chunk_overlap = runner.settings.rag_engine_chunk_overlap
    batches = [uris[i : i + MAX_URIS_PER_IMPORT] for i in range(0, len(uris), MAX_URIS_PER_IMPORT)]
    shown_uris = [redact_fn(u) for u in uris[:MAX_URIS_PER_IMPORT]]
    request = {
        "method": "POST",
        "endpoint": endpoint_label,
        "total_uris": len(uris),
        "batches": len(batches),
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "sample_body": import_rag_files_body(shown_uris, chunk_size=chunk_size, chunk_overlap=chunk_overlap),
    }
    totals = {"imported": 0, "skipped": 0, "failed": 0, "polls": 0}
    op_ids: list[str] = []
    async with tracker.scope(
        emitter, "import_documents", "Import into Vertex AI RAG Engine", "Vertex AI RAG Engine · ragFiles:import", request
    ) as sc:
        deadline = started + float(runner.settings.ingest_job_timeout_seconds) - 30.0
        for idx, batch_uris in enumerate(batches, start=1):
            body = import_rag_files_body(batch_uris, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
            op, _ = await asyncio.to_thread(rag.request, "POST", f"{corpus_path}/ragFiles:import", body=body)
            done_op, polls = await _poll_import_op(job, runner, rag, op, deadline, redact_fn, idx)
            counts = _import_counts(done_op)
            for key in ("imported", "skipped", "failed"):
                totals[key] += counts[key]
            totals["polls"] += polls
            op_ids.append(str(done_op.get("name", "")).rsplit("/", 1)[-1])
        sc.payload = {
            "operation_ids": op_ids,
            "batches": len(batches),
            "polls": totals["polls"],
            "poll_interval_s": IMPORT_POLL_SECONDS,
            "imported_count": totals["imported"],
            "skipped_count": totals["skipped"],
            "failed_count": totals["failed"],
            "uris": len(uris),
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
        }
        if totals["failed"] > 0:
            raise RagEngineImportError(
                f"ragFiles:import reported {totals['failed']} failed file(s) out of {len(uris)} (operations={op_ids})"
            )
    return totals


def _verify_rag_files(
    rag: VertexRagClient,
    corpus_path: str,
    expected_uris: set[str],
    redact_fn: Callable[[str], str],
    *,
    allowed_prefix: str | None = None,
) -> dict[str, Any]:
    files = rag.list_files(corpus_path)
    present_uris: set[str] = set()
    errors: list[str] = []
    active = 0
    for item in files:
        uris = [str(u) for u in ((item.get("gcsSource") or {}).get("uris") or []) if u]
        present_uris.update(uris)
        status = item.get("fileStatus") if isinstance(item.get("fileStatus"), dict) else {}
        state = str(status.get("state") or "")
        if state == "ACTIVE":
            active += 1
        elif state == "ERROR":
            errors.append(redact_fn(f"{item.get('displayName')}: {status.get('errorStatus') or 'ERROR'}"))
    missing = sorted(redact_fn(u) for u in expected_uris - present_uris)
    outside = [redact_fn(u) for u in present_uris if allowed_prefix and not u.startswith(allowed_prefix)]
    if errors:
        raise RagEngineVerifyError(f"RagCorpus contains {len(errors)} ERROR file(s): {errors[:5]}")
    if missing:
        raise RagEngineVerifyError(f"{len(missing)} expected file(s) missing from RagCorpus after import: {missing[:10]}")
    if outside:
        raise RagEngineVerifyError(f"RagCorpus holds {len(outside)} file(s) outside workspace prefix: {outside[:5]}")
    return {
        "file_count": len(files),
        "active_files": active,
        "expected": len(expected_uris),
        "missing": missing,
        "outside_corpus": len(present_uris - expected_uris),
        "counts_by_tier": {TIER: len(files)},
    }


def ensure_upload_corpus(
    client: VertexRagClient,
    res: RagEngineResources,
    workspace_id: str,
    *,
    embedding_model: str,
    deadline_seconds: float = _CORPUS_CREATE_DEADLINE_S,
) -> tuple[str, str]:
    """Ensure ``rag-arena-upl_<12hex>`` exists without touching ``ragEngineConfig`` or neighbor corpora."""
    display_name = upload_corpus_display_name(workspace_id)
    for corpus in client.list_corpora(res.corpora_path):
        if corpus.get("displayName") != display_name:
            continue
        short_id = extract_short_corpus_id(str(corpus.get("name") or ""))
        state = str((corpus.get("corpusStatus") or {}).get("state") or "UNKNOWN")
        if state == "ERROR":
            msg = redact_resource_text(str(corpus.get("corpusStatus")), res.project_id)
            raise RuntimeError(f"upload corpus '{display_name}' is in ERROR state: {msg}")
        return "exists", short_id
    body = corpus_body(
        res,
        display_name=display_name,
        embedding_model=embedding_model,
        description=f"The RAG Arena private upload workspace {workspace_id}",
    )
    op, _ = client.request("POST", res.corpora_path, body=body)
    done = client.poll_operation(op, deadline_seconds=deadline_seconds)
    created = done.get("response") if isinstance(done.get("response"), dict) else {}
    return "created", extract_short_corpus_id(str(created.get("name") or ""))


async def _upload_corpus_gcs(
    runner: IngestJobRunner, job: IngestJob, emitter: Any, tracker: Any, backends: RagEngineBackends, docs: list[_DiscoveredDoc]
) -> list[str]:
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


async def run_rag_engine_job(
    runner: IngestJobRunner, job: IngestJob, emitter: Any, tracker: Any, docs: list[_DiscoveredDoc]
) -> None:
    """Benchmark corpus ingestion into ``RAG_ENGINE_CORPUS_ID``."""
    started = time.monotonic()
    backends = await asyncio.to_thread(runner.rag_engine_factory, runner.settings)
    names = await _upload_corpus_gcs(runner, job, emitter, tracker, backends, docs)
    uris = [f"gs://{backends.bucket}/{n}" for n in names]
    redact_fn = lambda text: _redact(  # noqa: E731
        text, backends.resources.project_id, corpus_bucket=backends.bucket, corpus_id=backends.resources.corpus_id
    )
    counts = await _run_batched_imports(
        runner, job, emitter, tracker,
        rag=backends.rag,
        corpus_path=backends.resources.corpus_path,
        endpoint_label="ragCorpora/${RAG_ENGINE_CORPUS_ID}/ragFiles:import",
        uris=uris,
        redact_fn=redact_fn,
        started=started,
    )
    for stage in SKIPPED_STAGES:
        await emitter.skipped(stage, reason=SKIP_REASON)
    verify_req = {"method": "GET", "endpoint": "ragCorpora/${RAG_ENGINE_CORPUS_ID}/ragFiles"}
    async with tracker.scope(emitter, "verify_counts", "Verify RagCorpus files", "Vertex AI RAG Engine · ragFiles.list", verify_req) as sc:
        verified = await asyncio.to_thread(_verify_rag_files, backends.rag, backends.resources.corpus_path, set(uris), redact_fn)
        sc.payload = verified
    total = int(verified["file_count"])
    job.counts_by_tier = {TIER: total}
    s = runner.settings
    await asyncio.to_thread(backends.write_ingest_run, {
        "run_id": job.job_id, "level": job.level, "tier_profile": TIER, "started_at": job.created_at,
        "finished_at": utc_now_iso(), "status": "completed", "docs": len(docs), "pages": None, "chunks": None,
        "embedded": None, "loaded": counts["imported"] + counts["skipped"],
        "embedding_model": f"managed ({s.rag_engine_embedding_model})",
        "layout_source": f"vertex_ai_rag_engine (chunk_size={s.rag_engine_chunk_size}, overlap={s.rag_engine_chunk_overlap})",
        "git_sha": read_git_sha(), "stage_log": [r.to_dict() for r in emitter.records],
    })
    job.status = "completed"
    job.finished_at = utc_now_iso()
    await runner._put(job, {"event": "job_complete", "counts_by_tier": {TIER: total}, "chunks": 0})


def _prepare_upload_uris(workspace_id: str, bucket: str, files: Sequence[tuple[str, str]]) -> tuple[list[str], list[str]]:
    check_workspace_id(workspace_id)
    if not isinstance(bucket, str) or not bucket.strip() or "/" in bucket:
        raise ValueError("upload store must expose a bare bucket name")
    if not files or len(files) > MAX_UPLOAD_IMPORT_DOCS:
        raise ValueError(f"upload import needs 1..{MAX_UPLOAD_IMPORT_DOCS} files, got {len(files)}")
    bkt = bucket.strip()
    uris: list[str] = []
    doc_names: list[str] = []
    for obj, safe_name in files:
        _, parsed_name = parse_upload_object(workspace_id, obj)
        if parsed_name != safe_name:
            raise ValueError(f"object {obj!r} filename {parsed_name!r} does not match {safe_name!r}")
        uris.append(f"gs://{bkt}/{obj}")
        doc_names.append(safe_name)
    return uris, doc_names


async def run_rag_engine_upload_job(
    runner: IngestJobRunner, job: IngestJob, emitter: Any, tracker: Any, source: UploadSource
) -> None:
    """Zero-download per-workspace ``RagCorpus`` creation + GCS URI import + file verification."""
    started = time.monotonic()
    disc_req = {"workspace": source.workspace_id, "files": len(source.files), "download_bytes": 0}
    async with tracker.scope(emitter, "discover_docs", "Prepare upload URIs", "Cloud Storage · workspace manifest", disc_req) as sc:
        backends = await asyncio.to_thread(runner.upload_rag_engine_factory, runner.settings)
        bucket = str(getattr(backends.store, "bucket", "") or "").strip()
        uris, doc_names = _prepare_upload_uris(source.workspace_id, bucket, source.files)
        sc.payload = {"docs": len(uris), "doc_names": doc_names[:50], "bytes": 0, "source": "upload workspace", "workspace": source.workspace_id}

    display_name = upload_corpus_display_name(source.workspace_id)
    prov_req = {"display_name": display_name, "location": backends.resources.location, "embedding_model": runner.settings.rag_engine_embedding_model}
    async with tracker.scope(emitter, "upload_gcs", "Ensure isolated workspace RagCorpus", "Vertex AI RAG Engine · ragCorpora", prov_req) as sc:
        corpus_state, short_id = await asyncio.to_thread(
            lambda: ensure_upload_corpus(
                backends.rag, backends.resources, source.workspace_id,
                embedding_model=runner.settings.rag_engine_embedding_model,
            )
        )
        job.extra_manifest["rag_corpus_id"] = short_id
        job.extra_manifest["rag_corpus_display_name"] = display_name
        sc.payload = {"corpus_display_name": display_name, "corpus_state": corpus_state, "workspace": source.workspace_id, "uploaded": 0, "skipped": len(uris)}

    ws_res = RagEngineResources(backends.resources.project_id, backends.resources.location, short_id)
    redact_fn = lambda text: _redact(  # noqa: E731
        text, ws_res.project_id, upload_bucket=bucket, corpus_id=short_id
    )
    counts = await _run_batched_imports(
        runner, job, emitter, tracker,
        rag=backends.rag,
        corpus_path=ws_res.corpus_path,
        endpoint_label=f"ragCorpora/{display_name}/ragFiles:import",
        uris=uris,
        redact_fn=redact_fn,
        started=started,
    )
    for stage in SKIPPED_STAGES:
        await emitter.skipped(stage, reason=SKIP_REASON)
    verify_req = {"method": "GET", "endpoint": f"ragCorpora/{display_name}/ragFiles", "expected_docs": len(uris), "workspace": source.workspace_id}
    prefix = f"gs://{bucket}/uploads/{source.workspace_id}/"
    async with tracker.scope(emitter, "verify_counts", "Verify workspace RagCorpus files", "Vertex AI RAG Engine · ragFiles.list", verify_req) as sc:
        verified = await asyncio.to_thread(
            lambda: _verify_rag_files(backends.rag, ws_res.corpus_path, set(uris), redact_fn, allowed_prefix=prefix)
        )
        sc.payload = {**verified, "workspace": source.workspace_id}
    total = int(verified["file_count"])
    job.chunks = total
    job.counts_by_tier = {TIER: total}
    s = runner.settings
    await asyncio.to_thread(backends.write_ingest_run, {
        "run_id": job.job_id, "level": job.level, "tier_profile": TIER, "started_at": job.created_at,
        "finished_at": utc_now_iso(), "status": "completed", "docs": len(uris), "pages": None, "chunks": None,
        "embedded": None, "loaded": counts["imported"] + counts["skipped"],
        "embedding_model": f"managed ({s.rag_engine_embedding_model})",
        "layout_source": f"vertex_ai_rag_engine (chunk_size={s.rag_engine_chunk_size}, overlap={s.rag_engine_chunk_overlap})",
        "git_sha": read_git_sha(), "stage_log": [r.to_dict() for r in emitter.records],
    })
    job.status = "completed"
    job.finished_at = utc_now_iso()
    await runner._put(job, {"event": "job_complete", "counts_by_tier": {TIER: total}, "chunks": total})


async def rag_engine_status(runner: IngestJobRunner) -> dict[str, Any]:
    """``/api/v1/ingest/status`` entry for Level 6 (never exposes raw numeric ``RAG_ENGINE_CORPUS_ID``)."""
    backends = await asyncio.to_thread(runner.rag_engine_factory, runner.settings)
    cfg, corpus = await asyncio.gather(
        asyncio.to_thread(backends.rag.get_or_none, backends.resources.engine_config_path),
        asyncio.to_thread(backends.rag.get_or_none, backends.resources.corpus_path),
    )
    db_mode = classify_engine_config(cfg)
    s = runner.settings
    base = {
        "available": True,
        "db_mode": db_mode,
        "location": backends.resources.location,
        "corpus_display_name": CORPUS_DISPLAY_NAME,
        "embedding_model": s.rag_engine_embedding_model,
        "chunk_size": s.rag_engine_chunk_size,
        "chunk_overlap": s.rag_engine_chunk_overlap,
    }
    if corpus is None:
        return {**base, "corpus_state": "NOT_FOUND", "file_count": 0, "active_files": 0}
    state = str((corpus.get("corpusStatus") or {}).get("state") or "ACTIVE")
    files = await asyncio.to_thread(backends.rag.list_files, backends.resources.corpus_path)
    active = sum(1 for f in files if (f.get("fileStatus") or {}).get("state") == "ACTIVE")
    return {
        **base,
        "corpus_state": state,
        "corpus_display_name": str(corpus.get("displayName") or CORPUS_DISPLAY_NAME),
        "file_count": len(files),
        "active_files": active,
    }


def live_rag_engine_backends(settings: ArenaSettings) -> RagEngineBackends:
    """Factory for live/record corpus ingestion against ``RAG_ENGINE_CORPUS_ID`` + ``GCS_CORPUS_BUCKET``."""
    from app.core.gcp_clients import _ensure_live_settings, get_access_token
    from app.ingestion.live_backends import BigQueryLiveLoader

    _ensure_live_settings(settings)
    if not settings.rag_engine_corpus_id or not settings.gcs_corpus_bucket:
        raise RuntimeError(
            "rag_engine ingestion needs GCS_CORPUS_BUCKET and RAG_ENGINE_CORPUS_ID in .env (run scripts/provision_rag_engine.py)"
        )
    token = lambda: get_access_token(settings)  # noqa: E731
    resources = RagEngineResources(settings.gcp_project_id, settings.rag_engine_location, settings.rag_engine_corpus_id)
    rag = VertexRagClient(
        project_id=settings.gcp_project_id,
        location=settings.rag_engine_location,
        token_provider=token,
        attempt_timeout_seconds=float(settings.rag_engine_timeout_seconds),
        budget_seconds=float(settings.rag_engine_timeout_seconds) * 3,
    )
    gcs = GcsClient(bucket=settings.gcs_corpus_bucket, token_provider=token, timeout_seconds=60.0)
    return RagEngineBackends(
        gcs=gcs, rag=rag, resources=resources, bucket=settings.gcs_corpus_bucket,
        write_ingest_run=BigQueryLiveLoader(settings).write_ingest_run,
    )


def live_rag_engine_upload_backends(settings: ArenaSettings) -> RagEngineUploadBackends:
    """Factory for live/record upload workspace ingestion against per-workspace ``RagCorpus`` + ``UPLOAD_BUCKET``."""
    from app.core.gcp_clients import _ensure_live_settings, get_access_token
    from app.ingestion.live_backends import BigQueryLiveLoader

    _ensure_live_settings(settings)
    if not settings.upload_bucket:
        raise RuntimeError("rag_engine upload ingestion needs UPLOAD_BUCKET (or GCP_PROJECT_ID) in .env")
    token = lambda: get_access_token(settings)  # noqa: E731
    resources = RagEngineResources(settings.gcp_project_id, settings.rag_engine_location)
    rag = VertexRagClient(
        project_id=settings.gcp_project_id,
        location=settings.rag_engine_location,
        token_provider=token,
        attempt_timeout_seconds=float(settings.rag_engine_timeout_seconds),
        budget_seconds=float(settings.rag_engine_timeout_seconds) * 3,
    )
    store = UploadStore(bucket=settings.upload_bucket, token_provider=token, timeout_seconds=60.0)
    return RagEngineUploadBackends(
        store=store, rag=rag, resources=resources,
        write_ingest_run=BigQueryLiveLoader(settings).write_ingest_run,
    )


__all__ = [
    "CORPUS_DISPLAY_NAME",
    "MAX_UPLOAD_IMPORT_DOCS",
    "MAX_URIS_PER_IMPORT",
    "RagEngineBackends",
    "RagEngineImportError",
    "RagEngineUploadBackends",
    "RagEngineVerifyError",
    "SKIPPED_STAGES",
    "SKIP_REASON",
    "TIER",
    "UPLOAD_CORPUS_PREFIX",
    "ensure_upload_corpus",
    "import_rag_files_body",
    "live_rag_engine_backends",
    "live_rag_engine_upload_backends",
    "rag_engine_status",
    "rag_engine_unavailable",
    "rag_engine_upload_unavailable",
    "run_rag_engine_job",
    "run_rag_engine_upload_job",
    "upload_corpus_display_name",
]
