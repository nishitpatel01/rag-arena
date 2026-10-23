"""Level 5 ingestion + provisioning tests (offline; GCS and Discovery Engine REST mocked)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from app.core.vertex_search import DiscoveryEngineClient, OperationTimeoutError, VaisResources
from app.ingestion.gcs_sync import GCS_PREFIX, GcsClient, local_objects, plan_sync, sync_corpus
from app.ingestion.jobs import IngestJobRunner
from app.ingestion.managed_job import ManagedBackends, import_body
from app.main import create_app
from fakes import live_settings

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import provision_vertex_search as prov  # noqa: E402

_BUCKET = "acme-proj-rag-arena-corpus"


def _corpus(root: Path, files: dict[str, bytes]) -> list[Path]:
    paths = []
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        paths.append(p)
    return paths


def _md5(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode()


class _Router:
    """Answers httpx-style calls by (method, url-substring) rules; records calls."""

    def __init__(self, rules: list[tuple[str, str, Any]]) -> None:
        self.rules = rules
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kw})
        for i, (m, needle, answer) in enumerate(self.rules):
            if m == method and needle in url:
                status, body = answer(kw) if callable(answer) else answer
                return httpx.Response(status, text=json.dumps(body), request=httpx.Request(method, url))
        raise AssertionError(f"unexpected call {method} {url}")


def test_gcs_sync_uploads_only_missing_or_changed_and_never_deletes() -> None:
    root = Path(tempfile.mkdtemp())
    files = {"solicitation_01_x/a.pdf": b"%PDF-a", "solicitation_01_x/b.pdf": b"%PDF-b-new", "solicitation_02_y/c.pdf": b"%PDF-c"}
    objs = local_objects(root, _corpus(root, files))
    assert [o.name for o in objs] == [GCS_PREFIX + r for r in files]
    remote = {GCS_PREFIX + "solicitation_01_x/a.pdf": _md5(b"%PDF-a"), GCS_PREFIX + "solicitation_01_x/b.pdf": _md5(b"%PDF-b-old")}
    plan = plan_sync(objs, remote)
    assert [o.name.rsplit("/", 1)[-1] for o in plan.upload] == ["b.pdf", "c.pdf"] and dict(plan.reasons)[GCS_PREFIX + "solicitation_02_y/c.pdf"] == "missing"

    def upload(kw: dict[str, Any]) -> tuple[int, Any]:
        return 200, {"name": kw["params"]["name"], "md5Hash": _md5(kw["content"])}

    router = _Router([
        ("GET", "/storage/v1/b/", (200, {"items": [{"name": n, "md5Hash": m} for n, m in remote.items()]})),
        ("POST", "/upload/storage/v1/b/", upload),
    ])
    result = sync_corpus(GcsClient(bucket=_BUCKET, token_provider=lambda: "t", timeout_seconds=5, send=router, sleep=lambda s: None), objs)
    assert result.uploaded == (GCS_PREFIX + "solicitation_01_x/b.pdf", GCS_PREFIX + "solicitation_02_y/c.pdf")
    assert result.skipped == (GCS_PREFIX + "solicitation_01_x/a.pdf",) and result.bytes_uploaded == len(b"%PDF-b-new") + len(b"%PDF-c")
    assert {c["method"] for c in router.calls} == {"GET", "POST"}  # never DELETE
    assert router.calls[0]["params"]["prefix"] == GCS_PREFIX
    with pytest.raises(ValueError):
        GcsClient(bucket=_BUCKET, token_provider=lambda: "t", timeout_seconds=5, send=router).list_md5("other/")


def test_gcs_upload_md5_mismatch_fails_loud_and_errors_redact_bucket() -> None:
    root = Path(tempfile.mkdtemp())
    objs = local_objects(root, _corpus(root, {"solicitation_01_x/a.pdf": b"%PDF"}))
    bad = _Router([("POST", "/upload/", (200, {"md5Hash": "zzz"}))])
    with pytest.raises(RuntimeError):
        GcsClient(bucket=_BUCKET, token_provider=lambda: "t", timeout_seconds=5, send=bad).upload(objs[0])
    denied = _Router([("GET", "/storage/v1/b/", (403, {"error": f"no access to {_BUCKET}"}))])
    try:
        GcsClient(bucket=_BUCKET, token_provider=lambda: "t", timeout_seconds=5, send=denied).list_md5()
    except RuntimeError as exc:
        assert _BUCKET not in str(exc) and "${GCS_CORPUS_BUCKET}" in str(exc)
    else:
        raise AssertionError("403 must fail loud")


def test_import_body_explicit_uris_incremental_and_prefix_guard() -> None:
    body = import_body(_BUCKET, [GCS_PREFIX + "s/a.pdf"])
    assert body == {"gcsSource": {"inputUris": [f"gs://{_BUCKET}/{GCS_PREFIX}s/a.pdf"], "dataSchema": "content"},
                    "reconciliationMode": "INCREMENTAL"}
    for names in ([], ["elsewhere/a.pdf"], [GCS_PREFIX + f"{i}.pdf" for i in range(101)]):
        with pytest.raises(ValueError):
            import_body(_BUCKET, names)


def _de_client(router: _Router) -> DiscoveryEngineClient:
    return DiscoveryEngineClient(project_id="acme-proj", location="global", token_provider=lambda: "t",
                                 attempt_timeout_seconds=5, budget_seconds=10, sleep=lambda s: None, send=router)


def _job_setup(import_answer: Any, docs_answer: Any, op_polls: list[Any]):
    root = Path(tempfile.mkdtemp())
    corpus = root / "procurement_corpus"
    _corpus(corpus, {"solicitation_01_cloud/a.pdf": b"%PDF-a", "solicitation_02_data/b.pdf": b"%PDF-b"})
    gcs_router = _Router([
        ("GET", "/storage/v1/b/", (200, {"items": [{"name": GCS_PREFIX + "solicitation_01_cloud/a.pdf", "md5Hash": _md5(b"%PDF-a")}]})),
        ("POST", "/upload/", lambda kw: (200, {"md5Hash": _md5(kw["content"])})),
    ])
    rules = [("POST", "documents:import", import_answer), ("GET", "/documents", docs_answer)]
    rules += [("GET", "operations/import-1", p) for p in op_polls]
    de_router = _Router(rules)
    writes: list[dict[str, Any]] = []
    settings = live_settings(enabled_levels=("managed_vertex",), vais_data_store_id="rag-arena-procurement",
                             vais_engine_id="rag-arena-procurement-search", gcs_corpus_bucket=_BUCKET, gcp_project_id="acme-proj")
    backends = ManagedBackends(
        gcs=GcsClient(bucket=_BUCKET, token_provider=lambda: "t", timeout_seconds=5, send=gcs_router, sleep=lambda s: None),
        vais=_de_client(de_router), resources=VaisResources("acme-proj", "global", "rag-arena-procurement", "rag-arena-procurement-search"),
        bucket=_BUCKET, write_ingest_run=writes.append,
    )
    runner = IngestJobRunner(settings, corpus_dir=corpus, managed_factory=lambda s: backends)
    return runner, writes, de_router, gcs_router


def _run_job(runner: IngestJobRunner):
    async def go():
        job = runner.start("managed_vertex")
        await runner._tasks[job.job_id]
        return job
    return asyncio.run(go())


_URIS = [f"gs://{_BUCKET}/{GCS_PREFIX}solicitation_01_cloud/a.pdf", f"gs://{_BUCKET}/{GCS_PREFIX}solicitation_02_data/b.pdf"]
_DOCS = (200, {"documents": [{"id": f"d{i}", "content": {"uri": u}, "indexTime": "2026-10-23T00:00:00Z"} for i, u in enumerate(_URIS)]})
_DONE = {"name": "projects/1/locations/global/operations/import-1", "done": True,
         "metadata": {"successCount": "2", "failureCount": "0", "totalCount": "2"}, "response": {"errorSamples": []}}


def test_managed_ingest_job_honest_stage_set_and_counts() -> None:
    runner, writes, de_router, gcs_router = _job_setup((200, _DONE), _DOCS, [])
    job = _run_job(runner)
    assert job.status == "completed", job.error
    seq = [(e["event"], e.get("stage")) for e in job.events if e["event"].startswith("stage_")]
    assert seq == [
        ("stage_started", "discover_docs"), ("stage_completed", "discover_docs"),
        ("stage_started", "upload_gcs"), ("stage_completed", "upload_gcs"),
        ("stage_started", "import_documents"), ("stage_completed", "import_documents"),
        *[("stage_skipped", s) for s in ("parse", "chunk", "embed", "load_bigquery", "build_indexes")],
        ("stage_started", "verify_counts"), ("stage_completed", "verify_counts"),
    ]
    payload = {e["stage"]: e.get("payload") for e in job.events if e["event"] == "stage_completed"}
    assert payload["upload_gcs"]["uploaded"] == 1 and payload["upload_gcs"]["skipped"] == 1
    assert payload["import_documents"]["success_count"] == 2 and payload["import_documents"]["failure_count"] == 0
    assert payload["verify_counts"]["document_count"] == 2 and payload["verify_counts"]["missing"] == []
    skipped = [e for e in job.events if e["event"] == "stage_skipped"]
    assert all(e["reason"] == "managed by Vertex AI Search" for e in skipped)
    assert job.counts_by_tier == {"vertex_ai_search": 2} and writes[0]["tier_profile"] == "vertex_ai_search"
    blob = json.dumps(job.events)
    assert _BUCKET not in blob and "projects/1/" not in blob
    import_call = next(c for c in de_router.calls if c["url"].endswith("documents:import"))
    assert import_call["json"]["reconciliationMode"] == "INCREMENTAL" and import_call["json"]["gcsSource"]["inputUris"] == _URIS


def test_import_failure_counts_fail_the_stage_with_samples_verbatim() -> None:
    failed = {**_DONE, "metadata": {"successCount": "1", "failureCount": "1"},
              "response": {"errorSamples": [{"code": 3, "message": "Unsupported file gs://%s/x" % _BUCKET}]}}
    runner, writes, _, _ = _job_setup((200, failed), _DOCS, [])
    job = _run_job(runner)
    assert job.status == "failed" and job.failed_stage == "import_documents" and "Unsupported file" in job.error
    assert _BUCKET not in job.error and writes == []


def test_import_poll_is_bounded_by_the_job_budget() -> None:
    pending = {"name": "projects/1/locations/global/operations/import-1", "done": False, "metadata": {"successCount": "0"}}
    runner, _, _, _ = _job_setup((200, pending), _DOCS, [(200, pending)])
    runner.settings = live_settings(enabled_levels=("managed_vertex",), vais_data_store_id="rag-arena-procurement",
                                    vais_engine_id="rag-arena-procurement-search", gcs_corpus_bucket=_BUCKET,
                                    gcp_project_id="acme-proj", ingest_job_timeout_seconds=35)
    job = _run_job(runner)
    assert job.status == "failed" and job.failed_stage == "import_documents" and "OperationTimeoutError" in job.error


def test_verify_counts_missing_document_fails_loud() -> None:
    one = (200, {"documents": [{"id": "d0", "content": {"uri": _URIS[0]}}]})
    runner, _, _, _ = _job_setup((200, _DONE), one, [])
    job = _run_job(runner)
    assert job.status == "failed" and job.failed_stage == "verify_counts" and "missing" in job.error


def test_ingest_status_has_managed_vertex_entry() -> None:
    runner, _, _, _ = _job_setup((200, _DONE), _DOCS, [])
    store = {"name": "x", "displayName": "rag-arena-procurement"}
    engine = {"searchEngineConfig": {"searchTier": "SEARCH_TIER_ENTERPRISE", "searchAddOns": ["SEARCH_ADD_ON_LLM"]}}
    backends = runner.managed_factory(runner.settings)
    backends.vais._send = _Router([("GET", "/dataStores/rag-arena-procurement/branches", _DOCS),
                                   ("GET", "/dataStores/rag-arena-procurement", (200, store)),
                                   ("GET", "/engines/rag-arena-procurement-search", (200, engine))])
    status = asyncio.run(runner.status())
    mv = status["managed_vertex"]
    assert mv["data_store_state"] == "ACTIVE" and mv["document_count"] == 2 and mv["search_tier"] == "SEARCH_TIER_ENTERPRISE"
    replay = TestClient(create_app(settings=live_settings(demo_mode="replay", enabled_levels=("level1_naive", "managed_vertex"))))
    entry = replay.get("/api/v1/ingest/status").json()["managed_vertex"]
    assert entry["available"] is False and "DEMO_MODE=replay" in entry["reason"]


def test_operation_poll_bounded_and_timeout() -> None:
    router = _Router([("GET", "operations/x", (200, {"name": "projects/1/operations/x", "done": False}))])
    clock = iter(range(0, 10_000, 5))
    client = DiscoveryEngineClient(project_id="p", location="global", token_provider=lambda: "t", attempt_timeout_seconds=5,
                                   budget_seconds=10, sleep=lambda s: None, clock=lambda: float(next(clock)), send=router)
    with pytest.raises(OperationTimeoutError):
        client.poll_operation({"name": "projects/1/operations/x", "done": False}, deadline_seconds=60, interval_seconds=5)
    assert len(router.calls) <= 13


def _prov_settings():
    return live_settings(gcp_project_id="acme-proj", vais_location="global")


def test_provisioning_creates_only_our_ids_when_absent() -> None:
    op = {"name": "projects/1/operations/c", "done": True, "response": {}}
    router = _Router([
        ("GET", "/dataStores/rag-arena-procurement", (404, {"error": {"code": 404}})),
        ("GET", "/engines/rag-arena-procurement-search", (404, {"error": {"code": 404}})),
        ("POST", "/collections/default_collection/dataStores", (200, op)),
        ("POST", "/collections/default_collection/engines", (200, op)),
    ])
    assert prov.provision(_de_client(router), _prov_settings()) == {"data_store": "created", "engine": "created"}
    posts = [c for c in router.calls if c["method"] == "POST"]
    assert posts[0]["params"] == {"dataStoreId": "rag-arena-procurement"} and posts[1]["params"] == {"engineId": "rag-arena-procurement-search"}
    assert posts[0]["json"]["contentConfig"] == "CONTENT_REQUIRED" and "documentProcessingConfig" not in posts[0]["json"]
    assert posts[1]["json"]["searchEngineConfig"] == {"searchTier": "SEARCH_TIER_ENTERPRISE", "searchAddOns": ["SEARCH_ADD_ON_LLM"]}
    assert all("rag-arena-procurement" in c["url"] or c["method"] == "POST" for c in router.calls)
    assert {c["method"] for c in router.calls} <= {"GET", "POST"}


def test_provisioning_is_idempotent_and_refuses_foreign_resources() -> None:
    store = {"displayName": "rag-arena-procurement", "contentConfig": "CONTENT_REQUIRED"}
    engine = {"displayName": "rag-arena-procurement-search", "dataStoreIds": ["rag-arena-procurement"],
              "searchEngineConfig": {"searchTier": "SEARCH_TIER_ENTERPRISE", "searchAddOns": ["SEARCH_ADD_ON_LLM"]}}
    router = _Router([("GET", "/dataStores/rag-arena-procurement", (200, store)), ("GET", "/engines/", (200, engine))])
    assert prov.provision(_de_client(router), _prov_settings()) == {"data_store": "exists", "engine": "exists"}
    assert all(c["method"] == "GET" for c in router.calls)
    foreign = _Router([("GET", "/dataStores/rag-arena-procurement", (200, {"displayName": "someone-else"}))])
    with pytest.raises(RuntimeError):
        prov.provision(_de_client(foreign), _prov_settings())
    assert all(c["method"] == "GET" for c in foreign.calls)
