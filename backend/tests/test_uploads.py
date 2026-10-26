"""v1.1 step 1d: upload workspaces — names, PDF validation, GCS resumable sessions and manifests (offline)."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

import httpx
import pymupdf
import pytest

from app.ingestion.uploads import (
    UPLOAD_PREFIX,
    PdfRejected,
    UploadStore,
    new_file_id,
    new_workspace_id,
    owner_key,
    sanitize_filename,
    validate_pdf_file,
)

_BUCKET = "acme-proj-rag-arena-uploads"


def _pdf_bytes(pages: int = 2, *, js: bool = False, embedded: bool = False, encrypted: bool = False) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1} CLIN 0001 $1,000")
    if embedded:
        doc.embfile_add("payload.txt", b"hidden")
    if js:
        xref = doc.get_new_xref()
        doc.update_object(xref, "<< /S /JavaScript /JS (app.alert(1)) >>")
        doc.xref_set_key(doc.pdf_catalog(), "OpenAction", f"{xref} 0 R")
    if encrypted:
        return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    return doc.tobytes()


def _write(data: bytes) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "f.pdf"
    tmp.write_bytes(data)
    return tmp


# ---------- ids, names, owners ----------

def test_ids_are_unguessable_and_well_formed() -> None:
    ids = {new_workspace_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith("upl_") and len(i) == 16 and i[4:].isalnum() for i in ids)
    assert new_file_id().startswith("f_") and len(new_file_id()) == 14


def test_sanitize_filename() -> None:
    assert sanitize_filename("Contract 2026 (final).PDF") == "Contract_2026_final_.pdf"
    assert sanitize_filename("../../etc/passwd.pdf") == "passwd.pdf"
    assert sanitize_filename("C:\\Users\\x\\scope.pdf") == "scope.pdf"
    long_name = "a" * 300 + ".pdf"
    assert len(sanitize_filename(long_name)) <= 120 and sanitize_filename(long_name).endswith(".pdf")
    for bad in ("", "   ", "notes.txt", "pdf", ".pdf", "evil.pdf.exe", "\x00.pdf"):
        with pytest.raises(ValueError):
            sanitize_filename(bad)


def test_owner_key_hashes_identity_and_is_stable() -> None:
    a = owner_key("accounts.google.com:alice@example.com")
    assert a == owner_key("alice@example.com") and "alice" not in a and len(a) == 64
    assert a != owner_key("bob@example.com")
    with pytest.raises(ValueError):
        owner_key("")


# ---------- PDF validation (security, never size) ----------

def test_valid_pdf_reports_pages_size_and_sha256() -> None:
    data = _pdf_bytes(pages=3)
    info = validate_pdf_file(_write(data))
    assert info.pages == 3 and info.size_bytes == len(data) and len(info.sha256) == 64


def test_real_corpus_pdf_passes_safety_checks() -> None:
    sample = (Path(__file__).resolve().parents[2] / "data" / "sample_docs" / "procurement_corpus"
              / "solicitation_01_cloud_infrastructure" / "06_Amendment_0001_Scope_Update.pdf")
    info = validate_pdf_file(sample)
    assert info.pages >= 1 and info.size_bytes == sample.stat().st_size and len(info.sha256) == 64


def test_large_page_counts_are_accepted_no_app_limit() -> None:
    assert validate_pdf_file(_write(_pdf_bytes(pages=120))).pages == 120


def test_rejections_name_the_reason() -> None:
    cases = {
        "not a PDF": b"MZ\x90\x00 this is an executable renamed to .pdf",
        "empty": b"",
        "encrypted": _pdf_bytes(encrypted=True),
        "JavaScript": _pdf_bytes(js=True),
        "embedded files": _pdf_bytes(embedded=True),
        "unreadable": b"%PDF-1.7\n garbage without objects",
    }
    for reason, data in cases.items():
        with pytest.raises(PdfRejected, match=reason):
            validate_pdf_file(_write(data))


# ---------- GCS store (REST mocked) ----------

class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.generation = 1

    def __call__(self, method: str, url: str, **kw: Any) -> httpx.Response:
        u = httpx.URL(url)
        self.calls.append((method, u.path, kw))
        req = httpx.Request(method, url)
        params = dict(kw.get("params") or {})
        if method == "POST" and u.path.endswith("/o") and params.get("uploadType") == "resumable":
            return httpx.Response(200, headers={"Location": "https://storage.googleapis.com/upload/session/abc"},
                                  request=req)
        if method == "POST" and u.path.endswith("/o") and params.get("uploadType") == "media":
            name = params["name"]
            if "ifGenerationMatch" in params:
                current = self.objects.get(name, {}).get("generation", 0)
                if int(params["ifGenerationMatch"]) != current:
                    return httpx.Response(412, text="precondition failed", request=req)
            self.generation += 1
            self.objects[name] = {"data": kw["content"], "generation": self.generation,
                                  "contentType": kw["headers"]["Content-Type"]}
            return httpx.Response(200, json={"name": name, "generation": str(self.generation)}, request=req)
        if method == "GET" and "/o/" in u.path:
            name = httpx.URL(url).raw_path.decode().split("/o/", 1)[1].split("?", 1)[0]
            from urllib.parse import unquote
            name = unquote(name)
            obj = self.objects.get(name)
            if obj is None:
                return httpx.Response(404, text="not found", request=req)
            if params.get("alt") == "media":
                rng = (kw.get("headers") or {}).get("Range")
                if rng:
                    start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
                    return httpx.Response(206, content=obj["data"][start:end + 1], request=req)
                return httpx.Response(200, content=obj["data"], request=req)
            return httpx.Response(200, json={"name": name, "size": str(len(obj["data"])),
                                             "generation": str(obj["generation"]),
                                             "contentType": obj.get("contentType", "")}, request=req)
        if method == "GET" and u.path.endswith("/o"):
            prefix = params.get("prefix", "")
            items = [{"name": n} for n in sorted(self.objects) if n.startswith(prefix)]
            return httpx.Response(200, json={"items": items}, request=req)
        if method == "DELETE" and "/o/" in u.path:
            from urllib.parse import unquote
            name = unquote(httpx.URL(url).raw_path.decode().split("/o/", 1)[1])
            if self.objects.pop(name, None) is None:
                return httpx.Response(404, text="gone", request=req)
            return httpx.Response(204, request=req)
        raise AssertionError(f"unexpected {method} {url}")


def _store(fake: FakeStorage) -> UploadStore:
    return UploadStore(bucket=_BUCKET, token_provider=lambda: "tok", timeout_seconds=5, send=fake, sleep=lambda _s: None)


def test_resumable_session_is_scoped_create_only_and_origin_bound() -> None:
    fake = FakeStorage()
    uri = _store(fake).start_resumable("upl_0123456789ab", "f_0123456789ab", "scope.pdf",
                                        origin="https://arena.example.com")
    assert uri == "https://storage.googleapis.com/upload/session/abc"
    method, path, kw = fake.calls[0]
    assert method == "POST" and path == f"/upload/storage/v1/b/{_BUCKET}/o"
    assert kw["params"]["name"] == f"{UPLOAD_PREFIX}upl_0123456789ab/f_0123456789ab-scope.pdf"
    assert kw["params"]["ifGenerationMatch"] == "0"  # never overwrites an existing object
    assert kw["headers"]["X-Upload-Content-Type"] == "application/pdf"
    assert kw["headers"]["Origin"] == "https://arena.example.com"


def test_resumable_session_without_origin_omits_header() -> None:
    fake = FakeStorage()
    _store(fake).start_resumable("upl_0123456789ab", "f_0123456789ab", "a.pdf", origin=None)
    assert "Origin" not in fake.calls[0][2]["headers"]


def test_store_rejects_names_outside_the_workspace() -> None:
    store = _store(FakeStorage())
    for ws in ("upl_../x", "../upl_0123456789ab", "upl_short", "UPL_0123456789AB"):
        with pytest.raises(ValueError):
            store.start_resumable(ws, "f_0123456789ab", "a.pdf", origin=None)
    with pytest.raises(ValueError):
        store.start_resumable("upl_0123456789ab", "f_../../x", "a.pdf", origin=None)


def test_manifest_round_trip_with_generation_precondition() -> None:
    fake = FakeStorage()
    store = _store(fake)
    ws = "upl_0123456789ab"
    assert store.read_manifest(ws) is None
    gen = store.write_manifest(ws, {"id": ws, "files": []}, if_generation=0)
    manifest, read_gen = store.read_manifest(ws)
    assert manifest == {"id": ws, "files": []} and read_gen == gen
    with pytest.raises(RuntimeError, match="changed concurrently"):
        store.write_manifest(ws, {"id": ws, "files": [1]}, if_generation=0)


def test_download_streams_to_disk_and_meta_reads_size() -> None:
    fake = FakeStorage()
    store = _store(fake)
    obj = f"{UPLOAD_PREFIX}upl_0123456789ab/f_0123456789ab-a.pdf"
    fake.objects[obj] = {"data": b"%PDF-1.7 hello", "generation": 5, "contentType": "application/pdf"}
    meta = store.object_meta(obj)
    assert meta["size"] == "14" and meta["contentType"] == "application/pdf"
    dest = Path(tempfile.mkdtemp()) / "x.pdf"
    assert store.download_to(obj, dest) == 14 and dest.read_bytes() == b"%PDF-1.7 hello"
    assert store.object_meta(f"{UPLOAD_PREFIX}upl_0123456789ab/f_0123456789ab-missing.pdf") is None


def test_delete_workspace_removes_only_that_prefix() -> None:
    fake = FakeStorage()
    for name in (f"{UPLOAD_PREFIX}upl_0123456789ab/workspace.json", f"{UPLOAD_PREFIX}upl_0123456789ab/f_1-a.pdf",
                 f"{UPLOAD_PREFIX}upl_ffffffffffff/workspace.json", "sample_docs/keep.pdf"):
        fake.objects[name] = {"data": b"x", "generation": 1}
    deleted = _store(fake).delete_workspace("upl_0123456789ab")
    assert deleted == 2
    assert sorted(fake.objects) == ["sample_docs/keep.pdf", f"{UPLOAD_PREFIX}upl_ffffffffffff/workspace.json"]


def test_list_workspace_ids_reads_manifests_only() -> None:
    fake = FakeStorage()
    for name in (f"{UPLOAD_PREFIX}upl_0123456789ab/workspace.json", f"{UPLOAD_PREFIX}upl_0123456789ab/f_1-a.pdf",
                 f"{UPLOAD_PREFIX}upl_ffffffffffff/workspace.json"):
        fake.objects[name] = {"data": json.dumps({"id": "x"}).encode(), "generation": 1}
    assert _store(fake).list_workspace_ids() == ["upl_0123456789ab", "upl_ffffffffffff"]


def test_store_credential_failure_raises_typed_store_error() -> None:
    from app.ingestion.uploads import UploadStoreError

    def broken_token() -> str:
        raise RuntimeError("no ADC")

    store = UploadStore(bucket=_BUCKET, token_provider=broken_token, timeout_seconds=5, send=FakeStorage(),
                        sleep=lambda _s: None)
    with pytest.raises(UploadStoreError, match="credentials"):
        store.list_workspace_ids()
