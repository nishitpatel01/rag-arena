"""v1.6.0 Step 2 TDD: PDF active-content blocklist, xref/tmpfs bounds, >30-page upload parsing, and Vision cap alignment."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

import pymupdf
import pytest
from starlette.testclient import TestClient

from app.api.uploads_api import estimate_cost
from app.ingestion.jobs import IngestBackends
from app.ingestion.layout_documentai import split_pdf_ranges
from app.ingestion.parsers import chunk_pages, extract_pdf_pages
from app.ingestion.upload_bucket import BucketState
from app.ingestion.uploads import MAX_XREF_OBJECTS, PdfRejected, validate_pdf_file
from app.main import create_app
from fakes import live_settings
from test_uploads_api import FakeManager, FakeServices, FakeStore, _ALICE, _RowsLoader, _TEST_AUDIENCE, _TEST_CERTS, _workspace


def _make_pdf(pages: int = 2) -> pymupdf.Document:
    doc = pymupdf.open()
    for idx in range(pages):
        doc.new_page().insert_text((72, 72), f"Enterprise clause section {idx + 1} content.")
    return doc


def test_pdf_active_content_blocks_dangerous_actions_while_allowing_uri_links() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="rag_arena_pdf_sec_"))

    # 1. Legitimate https:// /URI hyperlink annotation MUST pass
    uri_doc = _make_pdf(1)
    uri_doc[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 72, 200, 100), "uri": "https://cloud.google.com"})
    uri_path = tmp / "legit_uri.pdf"
    uri_path.write_bytes(uri_doc.tobytes())
    uri_doc.close()
    info = validate_pdf_file(uri_path)
    assert info.pages == 1

    # 2. Dangerous PDF dictionary actions MUST fail closed with PdfRejected
    for label, obj_def in (
        ("SubmitForm", "<< /Type /Action /S /SubmitForm /F (https://evil.example/collect) >>"),
        ("GoToR", "<< /Type /Action /S /GoToR /F (../../secret.pdf) >>"),
        ("RichMedia", "<< /Subtype /RichMedia /RichMediaContent << >> >>"),
        ("XFA", "<< /XFA (xml-form-payload) >>"),
        ("AA", "<< /AA << /O << /S /URI /URI (https://ping.example) >> >> >>"),
    ):
        bad_doc = _make_pdf(1)
        xref = bad_doc.get_new_xref()
        bad_doc.update_object(xref, obj_def)
        bad_doc.xref_set_key(bad_doc.pdf_catalog(), "PieceInfo", f"{xref} 0 R")
        bad_path = tmp / f"bad_{label}.pdf"
        bad_path.write_bytes(bad_doc.tobytes())
        bad_doc.close()
        with pytest.raises(PdfRejected):
            validate_pdf_file(bad_path)


def test_pdf_xref_bomb_and_max_pages_bounds_fail_closed() -> None:
    assert MAX_XREF_OBJECTS == 200_000
    tmp = Path(tempfile.mkdtemp(prefix="rag_arena_xref_"))
    doc = _make_pdf(5)
    p = tmp / "five.pdf"
    p.write_bytes(doc.tobytes())
    doc.close()

    with pytest.raises(PdfRejected, match="MAX_UPLOAD_PDF_PAGES"):
        validate_pdf_file(p, max_pages=4)

    # Simulate a crafted xref bomb without allocating 200k real objects on disk
    fake_doc = MagicMock()
    fake_doc.__enter__.return_value = fake_doc
    fake_doc.__exit__.return_value = False
    fake_doc.needs_pass = False
    fake_doc.is_encrypted = False
    fake_doc.page_count = 1
    fake_doc.embfile_count.return_value = 0
    fake_doc.xref_length.return_value = MAX_XREF_OBJECTS + 1
    with patch.object(pymupdf, "open", lambda *a, **kw: fake_doc):
        with pytest.raises(PdfRejected, match="xref"):
            validate_pdf_file(p)


def test_finalize_checks_size_before_downloading_to_tmpfs_and_enforces_page_bound() -> None:
    class GuardedStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.download_calls = 0

        def download_to(self, name: str, dest: Path) -> int:
            self.download_calls += 1
            return super().download_to(name, dest)

    store = GuardedStore()
    settings = live_settings(
        access_mode="iap",
        iap_jwt_audience=_TEST_AUDIENCE,
        max_upload_file_bytes=2048,
        max_upload_pdf_pages=10,
    )
    app = create_app(
        settings=settings,
        query_run_writer=object(),
        ingest_backend_factory=lambda s: IngestBackends(None, _RowsLoader()),  # type: ignore[arg-type]
    )
    app.state.upload_services = FakeServices(FakeManager(BucketState.OK), store)
    app.state.iap_certs_override = _TEST_CERTS
    client = TestClient(app)

    ws = _workspace(client, _ALICE)
    # 1. Oversized GCS object (> 2048 bytes) is rejected with 413 BEFORE download_to touches /tmp!
    big_bytes = b"%PDF-1.7\n" + b"0" * 4096
    sess = client.post(f"/api/v1/uploads/{ws}/files", headers=_ALICE, json={"filename": "oversized.pdf", "size_bytes": len(big_bytes)})
    assert sess.status_code == 201
    obj_name = sess.json()["object"]
    fid = sess.json()["file"]["id"]
    store.objects[obj_name] = big_bytes

    fin_big = client.post(f"/api/v1/uploads/{ws}/files/{fid}/finalize", headers=_ALICE)
    assert fin_big.status_code == 413, fin_big.text
    assert store.download_calls == 0
    assert obj_name not in store.objects

    # 2. PDF exceeding max_upload_pdf_pages (12 > 10) is rejected cleanly with 422
    twelve = _make_pdf(12).tobytes()
    settings_ok_bytes = live_settings(
        access_mode="iap",
        iap_jwt_audience=_TEST_AUDIENCE,
        max_upload_file_bytes=10 * 1024 * 1024,
        max_upload_pdf_pages=10,
    )
    app.state.settings = settings_ok_bytes
    sess2 = client.post(f"/api/v1/uploads/{ws}/files", headers=_ALICE, json={"filename": "twelve.pdf", "size_bytes": len(twelve)})
    store.objects[sess2.json()["object"]] = twelve
    fin_pages = client.post(f"/api/v1/uploads/{ws}/files/{sess2.json()['file']['id']}/finalize", headers=_ALICE)
    assert fin_pages.status_code == 422 and "MAX_UPLOAD_PDF_PAGES" in fin_pages.json()["error"]


def test_uploads_parse_beyond_30_pages_without_silent_truncation() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="rag_arena_45p_"))
    doc = _make_pdf(45)
    pdf_bytes = doc.tobytes()
    doc.close()
    pdf_path = tmp / "45_page_agreement.pdf"
    pdf_path.write_bytes(pdf_bytes)

    # Corpus default stays at 30 pages for golden recording parity
    corpus_pages = extract_pdf_pages(pdf_path)
    assert len(corpus_pages) == 30

    # Upload mode with max_pages=500 extracts and chunks ALL 45 pages!
    upload_pages = extract_pdf_pages(pdf_path, max_pages=500)
    assert len(upload_pages) == 45
    chunks = chunk_pages(
        upload_pages,
        pdf_name="45_page_agreement.pdf",
        solicitation_id="upl_0123456789ab",
        tier_profile="hybrid",
        max_pages=500,
    )
    assert len(chunks) == 45 and chunks[-1].page_number == 45

    # Document AI range splitter also keeps all 45 pages in 3 online 15-page batches
    kept, ranges = split_pdf_ranges(pdf_bytes, max_pages=500)
    assert kept == 45 and [start for start, _ in ranges] == [1, 16, 31]


def test_estimate_cost_caps_vision_pages_to_max_upload_vision_pages() -> None:
    uncapped = estimate_cost("level3_multimodal", pages=50, vision_model="gemini-4-argon", max_vision_pages=100)
    capped = estimate_cost("level3_multimodal", pages=250, vision_model="gemini-4-argon", max_vision_pages=100)
    assert capped["pages"] == 250
    assert capped["documentai_usd"] == 2.5
    # Vision pages are bounded at 100 (exactly 2x the 50-page vision_max_usd, NOT 5x!)
    assert round(capped["vision_max_usd"], 4) == round(uncapped["vision_max_usd"] * 2, 4)
