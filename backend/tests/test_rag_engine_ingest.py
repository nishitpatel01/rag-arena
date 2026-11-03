"""Level 6 (``rag_engine``) ingestion + upload workspace corpus lifecycle tests (offline; GCS & Vertex RAG REST mocked)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any

import httpx
import pytest

from app.api.uploads_api import UploadServices, _level_recorder, _public_manifest, estimate_cost
from app.core.rag_engine_client import (
    OperationTimeoutError,
    RagEngineResources,
    VertexRagClient,
    corpus_body,
    extract_short_corpus_id,
)
from app.ingestion.gcs_sync import GCS_PREFIX, GcsClient
from app.ingestion.jobs import IngestJob, IngestJobRunner, UploadSource, _JobEmitter, _StageTracker, discover_corpus_docs
from app.ingestion.rag_engine_job import (
    MAX_URIS_PER_IMPORT,
    SKIP_REASON,
    TIER,
    RagEngineBackends,
    RagEngineUploadBackends,
    ensure_upload_corpus,
    import_rag_files_body,
    rag_engine_status,
    run_rag_engine_job,
    run_rag_engine_upload_job,
)
from fakes import live_settings

_CORPUS_BUCKET = "acme-proj-rag-arena-corpus"
_UPLOAD_BUCKET = "acme-proj-rag-arena-uploads"
_CORPUS_ID = "3379951520341557248"
_UPL_CORPUS_ID = "7788990011223344556"
_WS = "upl_0123456789ab"
_FID = "f_abcdef012345"


def _md5(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode()


def _write_corpus(root: Path, files: dict[str, bytes]) -> Path:
    for rel, raw in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return root


class _Router:
    """Deterministic httpx mock matching (method, substring) rules."""

    def __init__(self, rules: list[tuple[str, str, Any]]) -> None:
        self.rules = rules
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kw})
        for expected_method, needle, answer in self.rules:
            if method == expected_method and needle in url:
                status, body = answer(kw) if callable(answer) else answer
                text = "" if body is None else json.dumps(body)
                return httpx.Response(status, text=text, request=httpx.Request(method, url))
        raise AssertionError(f"unexpected HTTP call: {method} {url}")


def _rag_client(router: _Router) -> VertexRagClient:
    return VertexRagClient(
        project_id="acme-proj",
        location="us-central1",
        token_provider=lambda: "tok",
        attempt_timeout_seconds=5.0,
        budget_seconds=15.0,
        sleep=lambda s: None,
        send=router,
    )


def test_import_rag_files_body_validates_bounds_and_chunking() -> None:
    body = import_rag_files_body(
        [f"gs://{_CORPUS_BUCKET}/{GCS_PREFIX}solicitation_01/a.pdf"],
        chunk_size=512,
        chunk_overlap=100,
    )
    assert body == {
        "importRagFilesConfig": {
            "gcsSource": {"uris": [f"gs://{_CORPUS_BUCKET}/{GCS_PREFIX}solicitation_01/a.pdf"]},
            "ragFileTransformationConfig": {
                "ragFileChunkingConfig": {
                    "fixedLengthChunking": {"chunkSize": 512, "chunkOverlap": 100}
                }
            },
            "maxEmbeddingRequestsPerMin": 1000,
        }
    }
    assert extract_short_corpus_id("projects/76543/locations/us-central1/ragCorpora/12345") == "12345"
    res = RagEngineResources("acme-proj", "us-central1")
    cbody = corpus_body(res, display_name="rag-arena-procurement")
    assert cbody["vectorDbConfig"]["ragManagedDb"] == {"knn": {}}

    for bad_uris in ([], ["https://example.com/a.pdf"], [f"gs://b/{i}.pdf" for i in range(MAX_URIS_PER_IMPORT + 1)]):
        with pytest.raises(ValueError):
            import_rag_files_body(bad_uris, chunk_size=512, chunk_overlap=100)
    with pytest.raises(ValueError):
        import_rag_files_body(["gs://b/a.pdf"], chunk_size=256, chunk_overlap=256)


def test_corpus_rag_engine_job_batches_uris_skips_bq_verifies_and_redacts() -> None:
    # Create 27 PDFs across two solicitations so MAX_URIS_PER_IMPORT (25) exercises multi-batch import (25 + 2).
    files = {
        f"solicitation_01_cloud/doc_{i:02d}.pdf": f"%PDF-{i}".encode()
        for i in range(25)
    }
    files["solicitation_02_cyber/doc_25.pdf"] = b"%PDF-25"
    files["solicitation_02_cyber/doc_26.pdf"] = b"%PDF-26"
    corpus_dir = _write_corpus(Path(tempfile.mkdtemp()) / "procurement_corpus", files)
    docs = discover_corpus_docs(corpus_dir)
    assert len(docs) == 27

    gcs_router = _Router([
        ("GET", "/storage/v1/b/", (200, {"items": []})),
        ("POST", "/upload/storage/v1/b/", lambda kw: (200, {"md5Hash": _md5(kw["content"])})),
    ])
    expected_uris = [
        f"gs://{_CORPUS_BUCKET}/{GCS_PREFIX}{rel}" for rel in sorted(files)
    ]

    batch_calls = {"n": 0}

    def import_handler(kw: dict[str, Any]) -> tuple[int, Any]:
        batch_calls["n"] += 1
        uris = kw["json"]["importRagFilesConfig"]["gcsSource"]["uris"]
        assert 1 <= len(uris) <= MAX_URIS_PER_IMPORT
        return 200, {
            "name": f"projects/76543/locations/us-central1/operations/imp-{batch_calls['n']}",
            "done": True,
            "response": {
                "importedRagFilesCount": str(len(uris) - 1),
                "skippedRagFilesCount": "1",
                "failedRagFilesCount": "0",
            },
        }

    rag_files_body = {
        "ragFiles": [
            {
                "name": f"projects/76543/locations/us-central1/ragCorpora/{_CORPUS_ID}/ragFiles/rf_{i}",
                "displayName": u.rsplit("/", 1)[-1],
                "gcsSource": {"uris": [u]},
                "fileStatus": {"state": "ACTIVE"},
            }
            for i, u in enumerate(expected_uris)
        ]
    }
    rag_router = _Router([
        ("POST", f"/ragCorpora/{_CORPUS_ID}/ragFiles:import", import_handler),
        ("GET", f"/ragCorpora/{_CORPUS_ID}/ragFiles", (200, rag_files_body)),
    ])
    writes: list[dict[str, Any]] = []
    settings = live_settings(
        gcp_project_id="acme-proj",
        gcs_corpus_bucket=_CORPUS_BUCKET,
        rag_engine_location="us-central1",
        rag_engine_corpus_id=_CORPUS_ID,
    )
    backends = RagEngineBackends(
        gcs=GcsClient(bucket=_CORPUS_BUCKET, token_provider=lambda: "tok", timeout_seconds=5, send=gcs_router, sleep=lambda s: None),
        rag=_rag_client(rag_router),
        resources=RagEngineResources("acme-proj", "us-central1", _CORPUS_ID),
        bucket=_CORPUS_BUCKET,
        write_ingest_run=writes.append,
    )
    runner = IngestJobRunner(settings, corpus_dir=corpus_dir, rag_engine_factory=lambda s: backends)
    job = IngestJob(job_id="ing_test_rag01", level="rag_engine", tier_profile=TIER, status="running")
    emitter = _JobEmitter(runner, job)
    tracker = _StageTracker(job)

    asyncio.run(run_rag_engine_job(runner, job, emitter, tracker, docs))

    assert job.status == "completed", job.error
    assert batch_calls["n"] == 2  # 25 + 2 sequential batches
    seq = [(e["event"], e.get("stage")) for e in job.events if e["event"].startswith("stage_")]
    assert seq == [
        ("stage_started", "upload_gcs"), ("stage_completed", "upload_gcs"),
        ("stage_started", "import_documents"), ("stage_completed", "import_documents"),
        *[("stage_skipped", s) for s in ("parse", "chunk", "embed", "load_bigquery", "build_indexes")],
        ("stage_started", "verify_counts"), ("stage_completed", "verify_counts"),
    ]
    skipped = [e for e in job.events if e["event"] == "stage_skipped"]
    assert all(e["reason"] == SKIP_REASON for e in skipped)
    payload = {e["stage"]: e.get("payload") for e in job.events if e["event"] == "stage_completed"}
    assert payload["import_documents"]["imported_count"] == 25
    assert payload["import_documents"]["skipped_count"] == 2
    assert payload["import_documents"]["failed_count"] == 0
    assert payload["import_documents"]["batches"] == 2
    assert payload["verify_counts"]["file_count"] == 27 and payload["verify_counts"]["missing"] == []
    assert job.counts_by_tier == {TIER: 27}
    assert writes[0]["tier_profile"] == TIER and writes[0]["loaded"] == 27

    serialized = json.dumps(job.events)
    assert _CORPUS_BUCKET not in serialized
    assert "acme-proj" not in serialized
    assert "76543" not in serialized
    assert _CORPUS_ID not in serialized


def test_corpus_rag_engine_job_fails_loud_on_failed_files_missing_files_or_error_state() -> None:
    corpus_dir = _write_corpus(
        Path(tempfile.mkdtemp()) / "procurement_corpus",
        {"solicitation_01_cloud/a.pdf": b"%PDF-a"},
    )
    docs = discover_corpus_docs(corpus_dir)
    settings = live_settings(
        gcp_project_id="acme-proj",
        gcs_corpus_bucket=_CORPUS_BUCKET,
        rag_engine_location="us-central1",
        rag_engine_corpus_id=_CORPUS_ID,
    )

    def make_backends(rag_rules: list[tuple[str, str, Any]]) -> tuple[IngestJobRunner, IngestJob, _JobEmitter, _StageTracker]:
        gcs_router = _Router([
            ("GET", "/storage/v1/b/", (200, {"items": []})),
            ("POST", "/upload/storage/v1/b/", lambda kw: (200, {"md5Hash": _md5(kw["content"])})),
        ])
        backends = RagEngineBackends(
            gcs=GcsClient(bucket=_CORPUS_BUCKET, token_provider=lambda: "tok", timeout_seconds=5, send=gcs_router, sleep=lambda s: None),
            rag=_rag_client(_Router(rag_rules)),
            resources=RagEngineResources("acme-proj", "us-central1", _CORPUS_ID),
            bucket=_CORPUS_BUCKET,
            write_ingest_run=lambda r: None,
        )
        runner = IngestJobRunner(settings, corpus_dir=corpus_dir, rag_engine_factory=lambda s: backends)
        job = IngestJob(job_id="ing_fail", level="rag_engine", tier_profile=TIER, status="running")
        return runner, job, _JobEmitter(runner, job), _StageTracker(job)

    # Case 1: failedRagFilesCount > 0 fails import_documents
    runner, job, emitter, tracker = make_backends([
        ("POST", ":import", (200, {
            "name": "projects/76543/locations/us-central1/operations/imp-bad",
            "done": True,
            "response": {"importedRagFilesCount": "0", "failedRagFilesCount": "1"},
        })),
    ])
    with pytest.raises(RuntimeError):
        asyncio.run(run_rag_engine_job(runner, job, emitter, tracker, docs))
    assert job.failed_stage == "import_documents"

    # Case 2: RagFile missing or in ERROR state fails verify_counts
    runner2, job2, emitter2, tracker2 = make_backends([
        ("POST", ":import", (200, {
            "name": "projects/76543/locations/us-central1/operations/imp-ok",
            "done": True,
            "response": {"importedRagFilesCount": "1", "failedRagFilesCount": "0"},
        })),
        ("GET", "/ragFiles", (200, {"ragFiles": [{
            "name": f"projects/76543/locations/us-central1/ragCorpora/{_CORPUS_ID}/ragFiles/rf_0",
            "displayName": "a.pdf",
            "gcsSource": {"uris": [f"gs://{_CORPUS_BUCKET}/{GCS_PREFIX}solicitation_01_cloud/a.pdf"]},
            "fileStatus": {"state": "ERROR", "errorStatus": f"corrupt pdf in {_CORPUS_BUCKET}"},
        }]})),
    ])
    try:
        asyncio.run(run_rag_engine_job(runner2, job2, emitter2, tracker2, docs))
    except RuntimeError as exc:
        assert job2.failed_stage == "verify_counts"
        assert _CORPUS_BUCKET not in str(exc) and "${GCS_CORPUS_BUCKET}" in str(exc)
    else:
        raise AssertionError("ERROR RagFile must fail verify_counts")


def test_upload_rag_engine_job_creates_isolated_workspace_corpus_without_downloading_pdfs() -> None:
    class _FakeUploadStore:
        bucket = _UPLOAD_BUCKET

    neighbor = {
        "name": "projects/76543/locations/us-central1/ragCorpora/999111222",
        "displayName": "travel_planner_corpus",
        "corpusStatus": {"state": "ACTIVE"},
    }
    created_corpus = {
        "name": f"projects/76543/locations/us-central1/ragCorpora/{_UPL_CORPUS_ID}",
        "displayName": f"rag-arena-{_WS}",
        "corpusStatus": {"state": "ACTIVE"},
    }
    expected_uri = f"gs://{_UPLOAD_BUCKET}/uploads/{_WS}/{_FID}-Pricing.pdf"
    rag_router = _Router([
        ("GET", "/ragCorpora?", (200, {"ragCorpora": [neighbor]})),
        ("GET", "/ragCorpora", (200, {"ragCorpora": [neighbor]})),
        ("POST", "/ragCorpora/", lambda kw: (200, {
            "name": "projects/76543/locations/us-central1/operations/imp-upl",
            "done": True,
            "response": {"importedRagFilesCount": "1", "failedRagFilesCount": "0"},
        })),
        ("POST", "/ragCorpora", (200, {
            "name": "projects/76543/locations/us-central1/operations/create-upl",
            "done": True,
            "response": created_corpus,
        })),
    ])
    # Put more specific routes BEFORE broader "/ragCorpora" route!
    rag_router.rules = [
        ("GET", f"/ragCorpora/{_UPL_CORPUS_ID}/ragFiles", (200, {
            "ragFiles": [{
                "name": f"projects/76543/locations/us-central1/ragCorpora/{_UPL_CORPUS_ID}/ragFiles/rf_1",
                "displayName": f"{_FID}-Pricing.pdf",
                "gcsSource": {"uris": [expected_uri]},
                "fileStatus": {"state": "ACTIVE"},
            }]
        })),
        ("POST", f"/ragCorpora/{_UPL_CORPUS_ID}/ragFiles:import", (200, {
            "name": "projects/76543/locations/us-central1/operations/imp-upl",
            "done": True,
            "response": {"importedRagFilesCount": "1", "failedRagFilesCount": "0"},
        })),
        ("GET", "/ragCorpora", (200, {"ragCorpora": [neighbor]})),
        ("POST", "/ragCorpora", (200, {
            "name": "projects/76543/locations/us-central1/operations/create-upl",
            "done": True,
            "response": created_corpus,
        })),
    ]
    writes: list[dict[str, Any]] = []
    settings = live_settings(
        gcp_project_id="acme-proj",
        upload_bucket=_UPLOAD_BUCKET,
        rag_engine_location="us-central1",
        rag_engine_corpus_id=_CORPUS_ID,
    )
    upload_backends = RagEngineUploadBackends(
        store=_FakeUploadStore(),
        rag=_rag_client(rag_router),
        resources=RagEngineResources("acme-proj", "us-central1"),
        write_ingest_run=writes.append,
    )
    runner = IngestJobRunner(settings, upload_rag_engine_factory=lambda s: upload_backends)
    job = IngestJob(job_id="ing_upl_rag01", level="rag_engine", tier_profile=TIER, scope=_WS, status="running")
    emitter = _JobEmitter(runner, job)
    tracker = _StageTracker(job)

    def fail_if_downloaded(obj: str, dest: Path) -> int:
        raise AssertionError(f"rag_engine upload job must never download PDFs locally ({obj})")

    source = UploadSource(
        workspace_id=_WS,
        files=((f"uploads/{_WS}/{_FID}-Pricing.pdf", "Pricing.pdf"),),
        fetch=fail_if_downloaded,
    )
    asyncio.run(run_rag_engine_upload_job(runner, job, emitter, tracker, source))

    assert job.status == "completed", job.error
    assert job.extra_manifest == {
        "rag_corpus_id": _UPL_CORPUS_ID,
        "rag_corpus_display_name": f"rag-arena-{_WS}",
    }
    # Never touched ragEngineConfig or neighbor corpora
    assert all("ragEngineConfig" not in c["url"] for c in rag_router.calls)
    assert all("999111222" not in c["url"] for c in rag_router.calls)
    serialized = json.dumps(job.events)
    assert _UPLOAD_BUCKET not in serialized and "acme-proj" not in serialized and "76543" not in serialized


def test_level_recorder_persists_rag_corpus_id_and_delete_rag_corpus_cleans_up() -> None:
    # 1. Verify _level_recorder merges job.extra_manifest into manifest["levels"]["rag_engine"]
    manifests: dict[str, tuple[dict[str, Any], int]] = {
        _WS: ({"version": 1, "id": _WS, "files": [], "levels": {}}, 1)
    }

    class _MemStore:
        def read_manifest(self, ws: str) -> tuple[dict[str, Any], int] | None:
            return manifests.get(ws)

        def write_manifest(self, ws: str, data: dict[str, Any], *, if_generation: int) -> int:
            cur = manifests[ws][1]
            assert if_generation == cur
            manifests[ws] = (data, cur + 1)
            return cur + 1

    store = _MemStore()
    job = IngestJob(job_id="ing_1", level="rag_engine", tier_profile=TIER, status="completed", finished_at="2026-11-03T00:00:00Z")
    job.extra_manifest["rag_corpus_id"] = _UPL_CORPUS_ID
    job.extra_manifest["rag_corpus_display_name"] = f"rag-arena-{_WS}"
    _level_recorder(store, _WS, "rag_engine", [_FID])(job)
    recorded = manifests[_WS][0]["levels"]["rag_engine"]
    assert recorded["status"] == "completed" and recorded["rag_corpus_id"] == _UPL_CORPUS_ID
    pub = _public_manifest(manifests[_WS][0])
    assert _UPL_CORPUS_ID not in json.dumps(pub)
    assert pub["levels"]["rag_engine"]["rag_corpus_display_name"] == f"rag-arena-{_WS}"

    # 2. Verify estimate_cost("rag_engine", ...) requires no confirmation
    est = estimate_cost("rag_engine", 12, "gemini-4-argon")
    assert est["requires_confirmation"] is False and est["max_usd"] == 0.0 and "RAG Engine" in est["basis"]

    # 3. Verify UploadServices.delete_rag_corpus deletes recorded corpus + displayName fallback, never neighbor corpora
    delete_router = _Router([
        ("GET", "/ragCorpora", (200, {"ragCorpora": [
            {"name": "projects/76543/locations/us-central1/ragCorpora/999111222", "displayName": "travel_planner"},
            {"name": f"projects/76543/locations/us-central1/ragCorpora/{_UPL_CORPUS_ID}", "displayName": f"rag-arena-{_WS}"},
        ]})),
        ("DELETE", f"/ragCorpora/{_UPL_CORPUS_ID}", (200, {
            "name": "projects/76543/locations/us-central1/operations/del-upl",
            "done": True,
        })),
    ])
    settings = live_settings(gcp_project_id="acme-proj", upload_bucket=_UPLOAD_BUCKET, rag_engine_location="us-central1")
    services = UploadServices(settings)
    deleted_count = services.delete_rag_corpus(
        _WS, manifests[_WS][0], client=_rag_client(delete_router)
    )
    assert deleted_count == 1
    assert [c["url"] for c in delete_router.calls if c["method"] == "DELETE"] == [
        f"https://us-central1-aiplatform.googleapis.com/v1/projects/acme-proj/locations/us-central1/ragCorpora/{_UPL_CORPUS_ID}"
    ]


def test_rag_engine_status_reports_corpus_state_without_leaking_corpus_id() -> None:
    router = _Router([
        ("GET", "/ragEngineConfig", (200, {"ragManagedDbConfig": {"spanner": {"basic": {}}}})),
        ("GET", f"/ragCorpora/{_CORPUS_ID}/ragFiles", (200, {
            "ragFiles": [
                {"name": "f1", "fileStatus": {"state": "ACTIVE"}},
                {"name": "f2", "fileStatus": {"state": "ACTIVE"}},
            ]
        })),
        ("GET", f"/ragCorpora/{_CORPUS_ID}", (200, {
            "name": f"projects/76543/locations/us-central1/ragCorpora/{_CORPUS_ID}",
            "displayName": "rag-arena-procurement",
            "corpusStatus": {"state": "ACTIVE"},
        })),
    ])
    settings = live_settings(
        gcp_project_id="acme-proj",
        gcs_corpus_bucket=_CORPUS_BUCKET,
        rag_engine_location="us-central1",
        rag_engine_corpus_id=_CORPUS_ID,
    )
    backends = RagEngineBackends(
        gcs=GcsClient(bucket=_CORPUS_BUCKET, token_provider=lambda: "tok", timeout_seconds=5, send=_Router([])),
        rag=_rag_client(router),
        resources=RagEngineResources("acme-proj", "us-central1", _CORPUS_ID),
        bucket=_CORPUS_BUCKET,
        write_ingest_run=lambda r: None,
    )
    runner = IngestJobRunner(settings, rag_engine_factory=lambda s: backends)
    status = asyncio.run(rag_engine_status(runner))
    assert status["available"] is True
    assert status["corpus_state"] == "ACTIVE"
    assert status["db_mode"] == "spanner_basic"
    assert status["file_count"] == 2 and status["active_files"] == 2
    assert _CORPUS_ID not in json.dumps(status) and "acme-proj" not in json.dumps(status)
