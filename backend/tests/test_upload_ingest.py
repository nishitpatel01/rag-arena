"""v1.1 step 2: ingestion jobs over an upload workspace (L1/L2) — downloaded from the bucket, scoped to upl_* rows."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.config import ArenaSettings
from app.ingestion.jobs import IngestBackends, IngestJobRunner, UploadSource
from test_ingest_jobs_api import FakeEmbedder, FakeLoader

_CORPUS = Path(__file__).resolve().parents[2] / "data" / "sample_docs" / "procurement_corpus"
_PDF = _CORPUS / "solicitation_01_cloud_infrastructure" / "04_Pricing_Schedule_CLINs.pdf"
_WS = "upl_0123456789ab"


class ScopedLoader(FakeLoader):
    def __init__(self) -> None:
        super().__init__()
        self.scopes: list[str | None] = []

    def load_rows(self, rows: list[dict[str, Any]], scope: str | None = None) -> int:  # type: ignore[override]
        self.scopes.append(scope)
        return super().load_rows(rows)

    def count_scope(self, tier: str, scope: str) -> int:
        return sum(1 for r in self.rows if r["tier_profile"] == tier and r["solicitation_id"] == scope)


def _settings() -> ArenaSettings:
    return ArenaSettings(demo_mode="live", gcp_project_id="test-project", ingest_job_timeout_seconds=120)


def _source(finished: list[Any], fetched: list[str], *, fail_fetch: bool = False) -> UploadSource:
    def fetch(object_name: str, dest: Path) -> int:
        if fail_fetch:
            raise RuntimeError("object vanished")
        fetched.append(object_name)
        dest.write_bytes(_PDF.read_bytes())
        return dest.stat().st_size

    return UploadSource(workspace_id=_WS, files=((f"uploads/{_WS}/f_0123456789ab-Pricing.pdf", "Pricing.pdf"),),
                        fetch=fetch, on_finish=finished.append)


async def _run(runner: IngestJobRunner, level: str, source: UploadSource) -> Any:
    job = runner.start(level, upload=source)
    events = [e async for e in runner.stream(job.job_id)]
    return job, events


def test_upload_job_downloads_parses_and_loads_only_its_scope() -> None:
    loader, finished, fetched = ScopedLoader(), [], []
    runner = IngestJobRunner(_settings(), backend_factory=lambda s: IngestBackends(FakeEmbedder(), loader))
    job, events = asyncio.run(_run(runner, "level1_naive", _source(finished, fetched)))
    assert job.status == "completed", job.error
    assert job.scope == _WS and fetched == [f"uploads/{_WS}/f_0123456789ab-Pricing.pdf"]
    discover = next(e for e in events if e["event"] == "stage_completed" and e["stage"] == "discover_docs")
    assert discover["payload"]["docs"] == 1 and discover["payload"]["doc_names"] == ["Pricing.pdf"]
    assert discover["payload"]["source"] == "upload workspace"
    assert loader.scopes == [_WS] and loader.rows and {r["solicitation_id"] for r in loader.rows} == {_WS}
    assert {r["doc_name"] for r in loader.rows} == {"Pricing.pdf"}
    verify = next(e for e in events if e["event"] == "stage_completed" and e["stage"] == "verify_counts")
    assert verify["payload"]["scope_rows"] == len(loader.rows) and verify["payload"]["scope"] == _WS
    assert [j.status for j in finished] == ["completed"]


def test_level2_upload_job_uses_hybrid_tier() -> None:
    loader, finished = ScopedLoader(), []
    runner = IngestJobRunner(_settings(), backend_factory=lambda s: IngestBackends(FakeEmbedder(), loader))
    job, _ = asyncio.run(_run(runner, "level2_hybrid", _source(finished, [])))
    assert job.status == "completed" and {r["tier_profile"] for r in loader.rows} == {"hybrid"}


def test_upload_job_failure_is_reported_and_callback_still_runs() -> None:
    loader, finished = ScopedLoader(), []
    runner = IngestJobRunner(_settings(), backend_factory=lambda s: IngestBackends(FakeEmbedder(), loader))
    job, events = asyncio.run(_run(runner, "level1_naive", _source(finished, [], fail_fetch=True)))
    assert job.status == "failed" and "object vanished" in (job.error or "")
    assert job.failed_stage == "discover_docs" and events[-1]["event"] == "job_failed"
    assert loader.rows == [] and [j.status for j in finished] == ["failed"]


def test_upload_levels_not_built_yet_are_refused_loudly() -> None:
    runner = IngestJobRunner(_settings())
    with pytest.raises(ValueError, match="not available for uploads"):
        runner.start("level4_agentic", upload=_source([], []))


def test_managed_vertex_upload_job_writes_jsonl_imports_verifies_without_downloading_pdfs() -> None:
    import json
    import httpx
    from app.core.vertex_search import DiscoveryEngineClient, VaisResources
    from app.ingestion.managed_upload_job import ManagedUploadBackends, build_upload_import_records
    from app.ingestion.uploads import upload_doc_id

    upload_bkt = "acme-secret-uploads-bkt"
    obj_name = f"uploads/{_WS}/f_0123456789ab-Pricing.pdf"
    expected_doc_id = upload_doc_id(_WS, "f_0123456789ab")
    assert expected_doc_id == f"{_WS}_f_0123456789ab"

    records = build_upload_import_records(_WS, upload_bkt, ((obj_name, "Pricing.pdf"),))
    assert records == [
        {
            "id": expected_doc_id,
            "structData": {"workspace_id": _WS, "doc_name": "Pricing.pdf"},
            "content": {"mimeType": "application/pdf", "uri": f"gs://{upload_bkt}/{obj_name}"},
        }
    ]

    manifest_writes: list[tuple[str, list[dict[str, Any]]]] = []

    class _FakeUploadStore:
        bucket = upload_bkt

        def write_jsonl_manifest(self, ws: str, recs: list[dict[str, Any]]) -> str:
            manifest_writes.append((ws, recs))
            return f"uploads/{ws}/_vais_import.jsonl"

    calls: list[tuple[str, str, Any]] = []

    def send(method: str, url: str, **kw: Any) -> httpx.Response:
        calls.append((method, url, kw.get("json")))
        req = httpx.Request(method, url)
        if method == "POST" and url.endswith("/documents:import"):
            op = {
                "name": "projects/76543/locations/global/operations/imp-u1",
                "done": True,
                "metadata": {"successCount": "1", "failureCount": "0", "totalCount": "1"},
                "response": {"errorSamples": []},
            }
            return httpx.Response(200, text=json.dumps(op), request=req)
        if method == "GET" and url.endswith(f"/documents/{expected_doc_id}"):
            doc = {
                "id": expected_doc_id,
                "structData": {"workspace_id": _WS, "doc_name": "Pricing.pdf"},
                "content": {"uri": f"gs://{upload_bkt}/{obj_name}"},
                "indexTime": "2026-10-27T00:00:00Z",
            }
            return httpx.Response(200, text=json.dumps(doc), request=req)
        raise AssertionError(f"unexpected VAIS call: {method} {url}")

    writes: list[dict[str, Any]] = []
    vais = DiscoveryEngineClient(
        project_id="test-project", location="global", token_provider=lambda: "t",
        attempt_timeout_seconds=5, budget_seconds=10, sleep=lambda s: None, send=send,
    )
    backends = ManagedUploadBackends(
        store=_FakeUploadStore(),
        vais=vais,
        resources=VaisResources("test-project", "global", "rag-arena-uploads", "rag-arena-uploads-search"),
        write_ingest_run=writes.append,
    )
    fetched: list[str] = []
    finished: list[Any] = []
    runner = IngestJobRunner(
        ArenaSettings(
            demo_mode="live", gcp_project_id="test-project", upload_bucket=upload_bkt,
            vais_uploads_data_store_id="rag-arena-uploads", vais_uploads_engine_id="rag-arena-uploads-search",
            ingest_job_timeout_seconds=120,
        ),
        upload_managed_factory=lambda s: backends,
    )
    # fail_fetch=True proves managed_vertex upload ingestion NEVER downloads PDFs to local disk
    job, events = asyncio.run(_run(runner, "managed_vertex", _source(finished, fetched, fail_fetch=True)))
    assert job.status == "completed", job.error
    assert fetched == [] and len(manifest_writes) == 1 and manifest_writes[0][0] == _WS
    seq = [(e["event"], e.get("stage")) for e in events if e["event"].startswith("stage_")]
    assert seq == [
        ("stage_started", "discover_docs"), ("stage_completed", "discover_docs"),
        ("stage_started", "upload_gcs"), ("stage_completed", "upload_gcs"),
        ("stage_started", "import_documents"), ("stage_completed", "import_documents"),
        *[("stage_skipped", s) for s in ("parse", "chunk", "embed", "load_bigquery", "build_indexes")],
        ("stage_started", "verify_counts"), ("stage_completed", "verify_counts"),
    ]
    import_body = next(b for m, u, b in calls if m == "POST" and u.endswith("/documents:import"))
    assert import_body == {
        "gcsSource": {"inputUris": [f"gs://{upload_bkt}/uploads/{_WS}/_vais_import.jsonl"], "dataSchema": "document"},
        "reconciliationMode": "INCREMENTAL",
    }
    assert not any(u.endswith("/documents") for _, u, _ in calls)  # point GETs only; never lists other users' docs
    blob = json.dumps(events)
    assert upload_bkt not in blob and "76543" not in blob and "projects/" not in blob and "${UPLOAD_BUCKET}" in blob
    assert job.counts_by_tier == {"vertex_ai_search": 1} and writes and writes[0]["tier_profile"] == "vertex_ai_search"
    assert [j.status for j in finished] == ["completed"]


def test_managed_vertex_upload_job_fails_loud_on_missing_or_wrong_workspace_doc() -> None:
    import json
    import httpx
    from app.core.vertex_search import DiscoveryEngineClient, VaisResources
    from app.ingestion.managed_upload_job import ManagedUploadBackends
    from app.ingestion.uploads import upload_doc_id

    upload_bkt = "acme-secret-uploads-bkt"
    expected_doc_id = upload_doc_id(_WS, "f_0123456789ab")

    class _FakeUploadStore:
        bucket = upload_bkt

        def write_jsonl_manifest(self, ws: str, recs: list[dict[str, Any]]) -> str:
            return f"uploads/{ws}/_vais_import.jsonl"

    def send_wrong_ws(method: str, url: str, **kw: Any) -> httpx.Response:
        req = httpx.Request(method, url)
        if method == "POST" and url.endswith("/documents:import"):
            op = {"name": "projects/1/locations/global/operations/imp", "done": True,
                  "metadata": {"successCount": "1", "failureCount": "0", "totalCount": "1"}, "response": {}}
            return httpx.Response(200, text=json.dumps(op), request=req)
        if method == "GET" and url.endswith(f"/documents/{expected_doc_id}"):
            doc = {"id": expected_doc_id, "structData": {"workspace_id": "upl_ffffffffffff", "doc_name": "Pricing.pdf"}}
            return httpx.Response(200, text=json.dumps(doc), request=req)
        raise AssertionError(f"unexpected {method} {url}")

    vais = DiscoveryEngineClient(
        project_id="test-project", location="global", token_provider=lambda: "t",
        attempt_timeout_seconds=5, budget_seconds=10, sleep=lambda s: None, send=send_wrong_ws,
    )
    backends = ManagedUploadBackends(
        store=_FakeUploadStore(), vais=vais,
        resources=VaisResources("test-project", "global", "rag-arena-uploads", "rag-arena-uploads-search"),
        write_ingest_run=lambda r: None,
    )
    runner = IngestJobRunner(_settings(), upload_managed_factory=lambda s: backends)
    job, _ = asyncio.run(_run(runner, "managed_vertex", _source([], [], fail_fetch=True)))
    assert job.status == "failed" and job.failed_stage == "verify_counts"



def test_upload_source_validates_inputs() -> None:
    with pytest.raises(ValueError):
        UploadSource(workspace_id="upl_bad", files=(("uploads/x/y.pdf", "y.pdf"),), fetch=lambda n, d: 0)
    with pytest.raises(ValueError):
        UploadSource(workspace_id=_WS, files=(), fetch=lambda n, d: 0)
    with pytest.raises(ValueError):
        UploadSource(workspace_id=_WS, files=((f"uploads/{_WS}/a.pdf", "../a.pdf"),), fetch=lambda n, d: 0)
    with pytest.raises(ValueError):
        UploadSource(workspace_id=_WS, files=((f"uploads/{_WS}/a.pdf", "a.pdf"), (f"uploads/{_WS}/b.pdf", "a.pdf")),
                     fetch=lambda n, d: 0)


# ---------- step 3a: uploaded documents get honest metadata (no corpus defaults) ----------

def test_uploaded_pages_are_not_given_corpus_labels_or_invented_dates() -> None:
    from app.ingestion.parsers import chunk_pages

    lines = [["Statement of Work", "The vendor shall deliver 12 reports per year."]]
    for tier in ("naive", "hybrid"):
        up = chunk_pages(lines, pdf_name="Pricing_amendment_v2.pdf", solicitation_id=_WS, tier_profile=tier)
        assert up and all(c.doc_type == "UPLOADED_DOCUMENT" and c.effective_date is None and c.amendment_number == 0
                          for c in up), [(c.doc_type, c.effective_date, c.amendment_number) for c in up]
    corpus = chunk_pages(lines, pdf_name="05_Pricing.pdf", solicitation_id="solicitation_01", tier_profile="naive")
    assert corpus[0].doc_type != "UPLOADED_DOCUMENT"  # corpus behaviour unchanged


def test_uploaded_page_metadata_for_level3_is_honest() -> None:
    from app.ingestion.multimodal import page_metadata

    import tempfile

    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Master Services Agreement - amendment notes, no banner")
    path = Path(tempfile.mkdtemp()) / "02_MSA_amendment.pdf"  # corpus-looking name must not leak corpus labels
    doc.save(str(path))
    metas = page_metadata(path, 1, upload=True)
    assert metas[1].doc_type == "UPLOADED_DOCUMENT" and metas[1].effective_date is None
    assert metas[1].amendment_number == 0
    # a metadata banner printed on the page itself is still honoured (it is the document's own statement)
    banner = page_metadata(_PDF, 1, upload=True)
    assert banner[1].doc_type != "UPLOADED_DOCUMENT"
