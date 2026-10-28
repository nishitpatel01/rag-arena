"""v1.1 step 1d/1e: upload API — bucket status/create, workspaces, resumable sessions, finalize, delete (offline)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf
from starlette.testclient import TestClient

from app.ingestion.upload_bucket import BucketState, BucketStatus
from app.ingestion.uploads import UPLOAD_PREFIX
from app.main import create_app
from fakes import live_settings

_ALICE = {"X-Goog-Authenticated-User-Email": "accounts.google.com:alice@example.com"}
_BOB = {"X-Goog-Authenticated-User-Email": "accounts.google.com:bob@example.com"}


def _pdf(pages: int = 2, js: bool = False) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    if js:
        xref = doc.get_new_xref()
        doc.update_object(xref, "<< /S /JavaScript /JS (app.alert(1)) >>")
        doc.xref_set_key(doc.pdf_catalog(), "OpenAction", f"{xref} 0 R")
    return doc.tobytes()


class FakeManager:
    def __init__(self, state: BucketState = BucketState.OK) -> None:
        self.state = state
        self.ensure_calls = 0
        self.check_calls = 0

    def _status(self) -> BucketStatus:
        return BucketStatus(state=self.state, bucket="acme-uploads", message=f"state {self.state.value}",
                            remedy="do something" if self.state not in (BucketState.OK, BucketState.CREATED) else "")

    def check(self) -> BucketStatus:
        self.check_calls += 1
        return self._status()

    def ensure(self) -> BucketStatus:
        self.ensure_calls += 1
        if self.state is BucketState.NOT_FOUND:
            self.state = BucketState.CREATED
        return self._status()


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.manifests: dict[str, tuple[dict[str, Any], int]] = {}
        self.sessions: list[tuple[str, str, str, str | None]] = []
        self.deleted: list[str] = []

    def start_resumable(self, ws: str, file_id: str, safe_name: str, *, origin: str | None) -> str:
        self.sessions.append((ws, file_id, safe_name, origin))
        return f"https://storage.googleapis.com/upload/session/{file_id}"

    def object_meta(self, name: str) -> dict[str, Any] | None:
        data = self.objects.get(name)
        return None if data is None else {"name": name, "size": str(len(data)), "contentType": "application/pdf"}

    def download_to(self, name: str, dest: Path) -> int:
        Path(dest).write_bytes(self.objects[name])
        return len(self.objects[name])

    def delete_object(self, name: str) -> bool:
        self.deleted.append(name)
        return self.objects.pop(name, None) is not None

    def read_manifest(self, ws: str) -> tuple[dict[str, Any], int] | None:
        return self.manifests.get(ws)

    def write_manifest(self, ws: str, manifest: dict[str, Any], *, if_generation: int) -> int:
        current = self.manifests.get(ws, (None, 0))[1]
        if current != if_generation:
            raise RuntimeError("workspace manifest changed concurrently; reload and retry")
        self.manifests[ws] = (manifest, current + 1)
        return current + 1

    def delete_workspace(self, ws: str) -> int:
        names = [n for n in self.objects if n.startswith(f"{UPLOAD_PREFIX}{ws}/")]
        for n in names:
            self.objects.pop(n)
        return len(names) + (1 if self.manifests.pop(ws, None) else 0)

    def list_workspace_ids(self) -> list[str]:
        return sorted(self.manifests)

    def write_jsonl_manifest(self, ws: str, records: list[dict[str, Any]]) -> str:
        import json as _json

        name = f"{UPLOAD_PREFIX}{ws}/_vais_import.jsonl"
        self.objects[name] = ("\n".join(_json.dumps(r, sort_keys=True) for r in records) + "\n").encode("utf-8")
        return name


class FakeServices:
    def __init__(self, manager: FakeManager, store: FakeStore) -> None:
        self._manager, self._store = manager, store
        self.deleted_vais_docs: list[str] = []
        self.fail_vais_delete = False

    def bucket_manager(self) -> FakeManager:
        return self._manager

    def store(self) -> FakeStore:
        return self._store

    def delete_vais_docs(self, doc_ids: list[str]) -> int:
        if self.fail_vais_delete:
            raise RuntimeError("Discovery Engine 503 unavailable")
        self.deleted_vais_docs.extend(doc_ids)
        return len(doc_ids)


class _RowsLoader:
    """Ingest loader stand-in for upload API tests: records scoped deletes only."""

    table_fqn = "test-project.rag_arena.document_chunks"

    def __init__(self) -> None:
        self.deleted_scopes: list[str] = []

    def delete_scope(self, scope: str) -> int:
        self.deleted_scopes.append(scope)
        return 0


def _client(state: BucketState = BucketState.OK) -> tuple[TestClient, FakeManager, FakeStore]:
    from app.ingestion.jobs import IngestBackends

    loader = _RowsLoader()
    app = create_app(settings=live_settings(), query_run_writer=object(),
                     ingest_backend_factory=lambda s: IngestBackends(None, loader))  # type: ignore[arg-type]
    manager, store = FakeManager(state), FakeStore()
    app.state.upload_services = FakeServices(manager, store)
    return TestClient(app), manager, store


def _workspace(client: TestClient, headers: dict[str, str] = _ALICE) -> str:
    resp = client.post("/api/v1/uploads", headers=headers, json={})
    assert resp.status_code == 201, resp.text
    return resp.json()["workspace"]["id"]


def _upload(client: TestClient, store: FakeStore, ws: str, data: bytes, name: str = "Scope v2.pdf",
            headers: dict[str, str] = _ALICE) -> tuple[str, Any]:
    resp = client.post(f"/api/v1/uploads/{ws}/files", headers=headers, json={"filename": name, "size_bytes": len(data)})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    store.objects[body["object"]] = data  # the browser PUTs the bytes to the session URI
    fin = client.post(f"/api/v1/uploads/{ws}/files/{body['file']['id']}/finalize", headers=headers)
    return body["file"]["id"], fin


# ---------- availability ----------

def test_uploads_do_not_exist_on_the_replay_copy() -> None:
    client = TestClient(create_app(settings=live_settings(demo_mode="replay", gcp_project_id="")))
    for method, path in (("GET", "/api/v1/uploads/bucket"), ("POST", "/api/v1/uploads/bucket"),
                         ("POST", "/api/v1/uploads"), ("GET", "/api/v1/uploads")):
        resp = client.request(method, path, json={} if method == "POST" else None)
        assert resp.status_code == 404, (method, path, resp.status_code)
        assert "live" in resp.json()["error"]


# ---------- bucket status / create ----------

def test_bucket_status_is_read_only_and_always_200() -> None:
    client, manager, _ = _client(BucketState.NOT_FOUND)
    resp = client.get("/api/v1/uploads/bucket")
    assert resp.status_code == 200  # a status report; the state field carries the outcome
    body = resp.json()
    assert body["state"] == "not_found" and body["usable"] is False
    assert manager.check_calls == 1 and manager.ensure_calls == 0


def test_bucket_create_attempts_creation_by_default() -> None:
    client, manager, _ = _client(BucketState.NOT_FOUND)
    resp = client.post("/api/v1/uploads/bucket", json={})
    assert resp.status_code == 200 and resp.json()["state"] == "created"
    assert manager.ensure_calls == 1


def test_bucket_create_permission_denied_is_returned_with_remedy() -> None:
    client, _, _ = _client(BucketState.PERMISSION_DENIED)
    resp = client.post("/api/v1/uploads/bucket", json={})
    assert resp.status_code == 403
    body = resp.json()
    assert body["state"] == "permission_denied" and body["remedy"] and body["usable"] is False


# ---------- workspaces ----------

def test_new_workspace_auto_creates_bucket_then_records_owner_hash_only() -> None:
    client, manager, store = _client(BucketState.NOT_FOUND)
    ws = _workspace(client)
    assert manager.ensure_calls == 1
    manifest, _gen = store.manifests[ws]
    assert manifest["id"] == ws and manifest["files"] == [] and manifest["levels"] == {}
    assert "alice" not in str(manifest) and len(manifest["owner"]) == 64


def test_new_workspace_refused_when_bucket_unusable() -> None:
    client, _, store = _client(BucketState.ORG_POLICY)
    resp = client.post("/api/v1/uploads", headers=_ALICE, json={})
    assert resp.status_code == 409
    assert resp.json()["bucket"]["state"] == "org_policy" and store.manifests == {}


def test_list_and_get_show_only_your_workspaces() -> None:
    client, _, _ = _client()
    mine = _workspace(client, _ALICE)
    theirs = _workspace(client, _BOB)
    listed = [w["id"] for w in client.get("/api/v1/uploads", headers=_ALICE).json()["workspaces"]]
    assert listed == [mine]
    assert client.get(f"/api/v1/uploads/{theirs}", headers=_ALICE).status_code == 404
    assert client.get(f"/api/v1/uploads/{mine}", headers=_ALICE).status_code == 200
    assert client.get("/api/v1/uploads/upl_zzzz", headers=_ALICE).status_code == 404


# ---------- files ----------

def test_file_session_sanitises_name_and_binds_origin() -> None:
    client, _, store = _client()
    ws = _workspace(client)
    resp = client.post(f"/api/v1/uploads/{ws}/files", json={"filename": "../../Scope v2.pdf", "size_bytes": 10},
                       headers={**_ALICE, "Origin": "http://testserver"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["session_uri"].startswith("https://storage.googleapis.com/upload/session/")
    assert body["file"]["name"] == "Scope_v2.pdf" and body["file"]["status"] == "uploading"
    assert body["object"].startswith(f"{UPLOAD_PREFIX}{ws}/{body['file']['id']}-")
    assert store.sessions[-1][3] == "http://testserver"


def test_file_session_rejects_cross_origin_and_non_pdf() -> None:
    client, _, _ = _client()
    ws = _workspace(client)
    bad_origin = client.post(f"/api/v1/uploads/{ws}/files", json={"filename": "a.pdf", "size_bytes": 1},
                             headers={**_ALICE, "Origin": "https://evil.example.com"})
    assert bad_origin.status_code == 400 and "origin" in bad_origin.json()["error"].lower()
    not_pdf = client.post(f"/api/v1/uploads/{ws}/files", json={"filename": "a.exe", "size_bytes": 1}, headers=_ALICE)
    assert not_pdf.status_code == 400


def test_large_declared_sizes_are_accepted_no_app_cap() -> None:
    client, _, _ = _client()
    ws = _workspace(client)
    resp = client.post(f"/api/v1/uploads/{ws}/files", json={"filename": "big.pdf", "size_bytes": 5 * 1024 ** 3},
                       headers=_ALICE)
    assert resp.status_code == 201


def test_finalize_validates_and_marks_ready() -> None:
    client, _, store = _client()
    ws = _workspace(client)
    file_id, fin = _upload(client, store, ws, _pdf(pages=3))
    assert fin.status_code == 200, fin.text
    entry = fin.json()["file"]
    assert entry["status"] == "ready" and entry["pages"] == 3 and len(entry["sha256"]) == 64
    manifest, _ = store.manifests[ws]
    assert [f["status"] for f in manifest["files"]] == ["ready"] and manifest["files"][0]["id"] == file_id


def test_finalize_rejects_unsafe_pdf_and_deletes_the_object() -> None:
    client, _, store = _client()
    ws = _workspace(client)
    _file_id, fin = _upload(client, store, ws, _pdf(js=True))
    assert fin.status_code == 422
    assert "JavaScript" in fin.json()["error"]
    assert fin.json()["file"]["status"] == "rejected"
    assert store.deleted and not any(n.endswith(".pdf") for n in store.objects)


def test_finalize_before_upload_completes_is_409() -> None:
    client, _, store = _client()
    ws = _workspace(client)
    resp = client.post(f"/api/v1/uploads/{ws}/files", json={"filename": "a.pdf", "size_bytes": 1}, headers=_ALICE)
    fin = client.post(f"/api/v1/uploads/{ws}/files/{resp.json()['file']['id']}/finalize", headers=_ALICE)
    assert fin.status_code == 409 and "not found" in fin.json()["error"].lower()


def test_other_users_cannot_touch_your_workspace() -> None:
    client, _, store = _client()
    ws = _workspace(client, _ALICE)
    assert client.post(f"/api/v1/uploads/{ws}/files", json={"filename": "a.pdf", "size_bytes": 1},
                       headers=_BOB).status_code == 404
    assert client.delete(f"/api/v1/uploads/{ws}", headers=_BOB).status_code == 404
    assert ws in store.manifests


def test_delete_workspace_removes_everything() -> None:
    client, _, store = _client()
    ws = _workspace(client)
    _upload(client, store, ws, _pdf())
    resp = client.delete(f"/api/v1/uploads/{ws}", headers=_ALICE)
    assert resp.status_code == 200 and resp.json()["deleted_objects"] >= 2
    assert ws not in store.manifests and client.get(f"/api/v1/uploads/{ws}", headers=_ALICE).status_code == 404


def test_legacy_capped_upload_endpoint_is_gone() -> None:
    client, _, _ = _client()
    assert client.post("/api/v1/ingest/upload", json={}).status_code in (404, 405)


def test_store_failures_return_json_errors_not_500() -> None:
    from app.ingestion.uploads import UploadStoreError

    client, _, store = _client()

    def boom(*_a, **_k):
        raise UploadStoreError(0, "Cloud Storage credentials unavailable: no ADC")

    store.write_manifest = boom  # type: ignore[method-assign]
    resp = client.post("/api/v1/uploads", headers=_ALICE, json={})
    assert resp.status_code == 502 and "credentials" in resp.json()["error"]


# ---------- v1.1 step 2: ingest an upload workspace (L1/L2), battle scope checks, delete BigQuery rows ----------

def _ingest_client() -> tuple[TestClient, FakeStore, Any]:
    import json as _json  # noqa: F401 - parity with SSE helpers

    from app.ingestion.jobs import IngestBackends, IngestJobRunner
    from test_ingest_jobs_api import FakeEmbedder
    from test_upload_ingest import ScopedLoader

    loader = ScopedLoader()
    loader.deleted_scopes = []  # type: ignore[attr-defined]
    loader.delete_scope = lambda scope: loader.deleted_scopes.append(scope) or 0  # type: ignore[attr-defined]
    settings = live_settings(ingest_job_timeout_seconds=120, enabled_levels=("level1_naive", "level2_hybrid"))
    factory = lambda s: IngestBackends(FakeEmbedder(), loader)  # noqa: E731
    app = create_app(settings=settings, query_run_writer=object(), ingest_backend_factory=factory)
    app.state.ingest_runner = IngestJobRunner(settings, backend_factory=factory)
    store = FakeStore()
    app.state.upload_services = FakeServices(FakeManager(), store)
    return TestClient(app), store, loader


def _corpus_pdf() -> bytes:
    path = (Path(__file__).resolve().parents[2] / "data" / "sample_docs" / "procurement_corpus"
            / "solicitation_01_cloud_infrastructure" / "04_Pricing_Schedule_CLINs.pdf")
    return path.read_bytes()


def _sse(text: str) -> list[dict[str, Any]]:
    import json as _json

    return [_json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


def test_ingest_workspace_level1_records_completion_in_manifest() -> None:
    client, store, loader = _ingest_client()
    with client:
        ws = _workspace(client)
        _upload(client, store, ws, _corpus_pdf(), name="Pricing.pdf")
        resp = client.post(f"/api/v1/uploads/{ws}/ingest", headers=_ALICE, json={"level": "level1_naive"})
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job"]["job_id"]
        assert resp.json()["job"]["scope"] == ws
        events = _sse(client.get(f"/api/v1/ingest/jobs/{job_id}/stream").text)
        assert events[-1]["event"] == "job_complete"
        level = client.get(f"/api/v1/uploads/{ws}", headers=_ALICE).json()["workspace"]["levels"]["level1_naive"]
    assert level["status"] == "completed" and level["job_id"] == job_id and level["chunks"] == len(loader.rows) > 0
    assert len(level["file_ids"]) == 1
    assert {r["solicitation_id"] for r in loader.rows} == {ws}


def test_ingest_refuses_unbuilt_levels_no_ready_files_and_parallel_runs() -> None:
    client, store, _ = _ingest_client()
    with client:
        ws = _workspace(client)
        empty = client.post(f"/api/v1/uploads/{ws}/ingest", headers=_ALICE, json={"level": "level1_naive"})
        assert empty.status_code == 409 and "ready" in empty.json()["error"]
        _upload(client, store, ws, _corpus_pdf(), name="Pricing.pdf")
        l4 = client.post(f"/api/v1/uploads/{ws}/ingest", headers=_ALICE, json={"level": "level4_agentic"})
        assert l4.status_code == 400 and "not available for uploads" in l4.json()["error"]
        manifest, gen = store.manifests[ws]
        store.manifests[ws] = ({**manifest, "levels": {"level2_hybrid": {"status": "running"}}}, gen)
        busy = client.post(f"/api/v1/uploads/{ws}/ingest", headers=_ALICE, json={"level": "level2_hybrid"})
        assert busy.status_code == 409 and "already" in busy.json()["error"]
        other = client.post(f"/api/v1/uploads/{ws}/ingest", headers=_BOB, json={"level": "level1_naive"})
        assert other.status_code == 404


def test_duplicate_file_names_in_one_workspace_are_refused() -> None:
    client, _, store = _client()
    ws = _workspace(client)
    _upload(client, store, ws, _pdf(), name="Scope.pdf")
    dup = client.post(f"/api/v1/uploads/{ws}/files", headers=_ALICE, json={"filename": "Scope.pdf", "size_bytes": 1})
    assert dup.status_code == 409 and "already" in dup.json()["error"]


def test_battle_on_a_workspace_requires_ownership_and_ingested_levels() -> None:
    client, _, _store = _client()
    ws = _workspace(client)
    body = {"query": "What is the CLIN 0001 price?", "engines": ["level1_naive"], "solicitation_filter": ws}
    not_ingested = client.post("/api/v1/battle/stream", headers=_ALICE, json=body)
    assert not_ingested.status_code == 400 and "level1_naive" in not_ingested.json()["error"]
    assert client.post("/api/v1/battle/stream", headers=_BOB, json=body).status_code == 404
    unknown = client.post("/api/v1/battle/stream", headers=_ALICE, json={**body, "solicitation_filter": "upl_ffffffffffff"})
    assert unknown.status_code == 404


def test_battle_scope_check_maps_level4_to_level3_data() -> None:
    from app.api.uploads_api import missing_upload_levels

    manifest = {"levels": {"level1_naive": {"status": "completed"}, "level3_multimodal": {"status": "completed"},
                           "level2_hybrid": {"status": "failed"}}}
    assert missing_upload_levels(manifest, ["level1_naive", "level4_agentic"]) == []
    assert missing_upload_levels(manifest, ["level2_hybrid", "managed_vertex"]) == ["level2_hybrid", "managed_vertex"]


def test_battle_with_upload_scope_on_replay_copy_is_refused() -> None:
    client = TestClient(create_app(settings=live_settings(demo_mode="replay", gcp_project_id="")))
    resp = client.post("/api/v1/battle/stream", json={"query": "q", "solicitation_filter": "upl_0123456789ab"})
    assert resp.status_code == 404 and "live" in resp.json()["error"]


def test_delete_workspace_also_deletes_its_bigquery_rows() -> None:
    client, store, loader = _ingest_client()
    with client:
        ws = _workspace(client)
        resp = client.delete(f"/api/v1/uploads/{ws}", headers=_ALICE)
    assert resp.status_code == 200 and loader.deleted_scopes == [ws]
    assert resp.json()["bigquery"] == {"deleted_scope": ws}


# ---------- v1.1 step 3: Level 3 needs an explicit cost confirmation ----------

def test_level3_estimate_and_confirmation_gate() -> None:
    client, store, _ = _ingest_client()
    with client:
        ws = _workspace(client)
        _upload(client, store, ws, _pdf(pages=4), name="Four.pdf")
        est = client.get(f"/api/v1/uploads/{ws}/estimate?level=level3_multimodal", headers=_ALICE)
        assert est.status_code == 200, est.text
        e = est.json()["estimate"]
        assert e["pages"] == 4 and e["documentai_usd"] == 0.04 and e["max_usd"] >= e["documentai_usd"]
        assert "Document AI" in e["basis"] and e["vision_model"]
        unconfirmed = client.post(f"/api/v1/uploads/{ws}/ingest", headers=_ALICE, json={"level": "level3_multimodal"})
        assert unconfirmed.status_code == 409 and unconfirmed.json()["needs_confirmation"] is True
        assert unconfirmed.json()["estimate"]["max_usd"] == e["max_usd"]
        too_low = client.post(f"/api/v1/uploads/{ws}/ingest", headers=_ALICE,
                              json={"level": "level3_multimodal", "confirm_cost_usd": e["max_usd"] / 2})
        assert too_low.status_code == 409
        manifest, _ = store.manifests[ws]
        assert "level3_multimodal" not in manifest["levels"]  # nothing started without confirmation


def test_level1_and_level2_need_no_confirmation() -> None:
    client, store, _ = _ingest_client()
    with client:
        ws = _workspace(client)
        _upload(client, store, ws, _pdf(pages=1), name="One.pdf")
        est = client.get(f"/api/v1/uploads/{ws}/estimate?level=level1_naive", headers=_ALICE).json()["estimate"]
        assert est["requires_confirmation"] is False


# ---------- v1.1 step 4: Level 5 upload ingest, estimate, and synchronous VAIS document delete ----------

def test_managed_vertex_upload_ingest_and_delete_cleans_vais_docs_or_keeps_workspace_on_failure() -> None:
    import json as _json
    import httpx
    from app.core.vertex_search import DiscoveryEngineClient, VaisResources
    from app.ingestion.jobs import IngestBackends, IngestJobRunner
    from app.ingestion.managed_upload_job import ManagedUploadBackends
    from app.ingestion.uploads import upload_doc_id
    from test_ingest_jobs_api import FakeEmbedder
    from test_upload_ingest import ScopedLoader

    loader = ScopedLoader()
    loader.deleted_scopes = []  # type: ignore[attr-defined]
    loader.delete_scope = lambda scope: loader.deleted_scopes.append(scope) or 0  # type: ignore[attr-defined]
    store = FakeStore()
    store.bucket = "acme-uploads"  # type: ignore[attr-defined]
    services = FakeServices(FakeManager(), store)

    def send(method: str, url: str, **kw: Any) -> httpx.Response:
        req = httpx.Request(method, url)
        if method == "POST" and url.endswith("/documents:import"):
            op = {"name": "projects/1/locations/global/operations/imp", "done": True,
                  "metadata": {"successCount": "1", "failureCount": "0", "totalCount": "1"}, "response": {}}
            return httpx.Response(200, text=_json.dumps(op), request=req)
        if method == "GET" and "/documents/upl_" in url:
            doc_id = url.rsplit("/", 1)[-1]
            ws_id = "_".join(doc_id.split("_")[:2])
            doc = {"id": doc_id, "structData": {"workspace_id": ws_id, "doc_name": "Pricing.pdf"},
                   "indexTime": "2026-10-27T00:00:00Z"}
            return httpx.Response(200, text=_json.dumps(doc), request=req)
        raise AssertionError(f"unexpected {method} {url}")

    vais = DiscoveryEngineClient(
        project_id="test-project", location="global", token_provider=lambda: "t",
        attempt_timeout_seconds=5, budget_seconds=10, sleep=lambda s: None, send=send,
    )
    upload_managed = ManagedUploadBackends(
        store=store, vais=vais,
        resources=VaisResources("test-project", "global", "rag-arena-uploads", "rag-arena-uploads-search"),
        write_ingest_run=lambda r: None,
    )
    settings = live_settings(
        ingest_job_timeout_seconds=120,
        enabled_levels=("level1_naive", "managed_vertex"),
        vais_uploads_data_store_id="rag-arena-uploads",
        vais_uploads_engine_id="rag-arena-uploads-search",
    )
    factory = lambda s: IngestBackends(FakeEmbedder(), loader)  # noqa: E731
    app = create_app(
        settings=settings, query_run_writer=object(),
        ingest_backend_factory=factory, ingest_upload_managed_factory=lambda s: upload_managed,
    )
    app.state.ingest_runner = IngestJobRunner(
        settings, backend_factory=factory, upload_managed_factory=lambda s: upload_managed,
    )
    app.state.upload_services = services
    client = TestClient(app)
    with client:
        ws = _workspace(client)
        file_id, _ = _upload(client, store, ws, _corpus_pdf(), name="Pricing.pdf")
        est = client.get(f"/api/v1/uploads/{ws}/estimate?level=managed_vertex", headers=_ALICE)
        assert est.status_code == 200
        assert est.json()["estimate"]["requires_confirmation"] is False
        assert "Vertex AI Search" in est.json()["estimate"]["basis"]

        resp = client.post(f"/api/v1/uploads/{ws}/ingest", headers=_ALICE, json={"level": "managed_vertex"})
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job"]["job_id"]
        events = _sse(client.get(f"/api/v1/ingest/jobs/{job_id}/stream").text)
        assert events[-1]["event"] == "job_complete"
        mv = client.get(f"/api/v1/uploads/{ws}", headers=_ALICE).json()["workspace"]["levels"]["managed_vertex"]
        assert mv["status"] == "completed" and mv["tier_profile"] == "vertex_ai_search"

        # If VAIS delete fails, workspace manifest and GCS objects remain intact so user can retry
        services.fail_vais_delete = True
        failed_del = client.delete(f"/api/v1/uploads/{ws}", headers=_ALICE)
        assert failed_del.status_code == 502 and "Vertex AI Search" in failed_del.json()["error"]
        assert ws in store.manifests

        # Retry succeeds and deletes deterministic doc id before removing GCS objects
        services.fail_vais_delete = False
        ok_del = client.delete(f"/api/v1/uploads/{ws}", headers=_ALICE)
        assert ok_del.status_code == 200, ok_del.text
        assert services.deleted_vais_docs == [upload_doc_id(ws, file_id)]
        assert ok_del.json()["vertex_ai_search"] == {"deleted_docs": 1}
        assert ws not in store.manifests

