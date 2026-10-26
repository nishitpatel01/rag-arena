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


class FakeServices:
    def __init__(self, manager: FakeManager, store: FakeStore) -> None:
        self._manager, self._store = manager, store

    def bucket_manager(self) -> FakeManager:
        return self._manager

    def store(self) -> FakeStore:
        return self._store


def _client(state: BucketState = BucketState.OK) -> tuple[TestClient, FakeManager, FakeStore]:
    app = create_app(settings=live_settings(), query_run_writer=object())
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
