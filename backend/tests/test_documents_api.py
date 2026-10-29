"""Document viewer API: real page PNG rendering + excerpt highlight lookup on the LOCAL corpus (PyMuPDF)."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from starlette.testclient import TestClient

from app.api import documents
from app.core.config import ArenaSettings
from app.ingestion.parsers import pymupdf_available
from app.main import create_app

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_SOL = "solicitation_01"
_PRICING = "04_Pricing_Schedule_CLINs.pdf"
_PRICING_EXCERPT = (
    "SOL-2026-CLOUD-01 04_Pricing_Schedule_CLINs.pdf SCHEDULE B: CONTRACT LINE ITEM NUMBER (CLIN) PRICING MATRIX "
    "DOC_TYPE=PRICING_CLIN | SECTION=CLIN-0004-AB | ELEMENT=table"
)


def _client() -> TestClient:
    return TestClient(create_app(settings=ArenaSettings(demo_mode="replay")))


def _page(client: TestClient, **params: object):
    base = {"solicitation_id": _SOL, "doc_name": _PRICING, "page": 1}
    base.update(params)
    return client.get("/api/v1/documents/page", params=base)


def _highlights(client: TestClient, text: str, **params: object):
    base = {"solicitation_id": _SOL, "doc_name": _PRICING, "page": 1, "text": text}
    base.update(params)
    return client.get("/api/v1/documents/highlights", params=base)


def test_pymupdf_is_installed_for_document_viewer() -> None:
    assert pymupdf_available()


def test_page_renders_real_png_with_private_cache_header() -> None:
    resp = _page(_client())
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "private, max-age=3600"
    assert resp.content[:8] == _PNG_MAGIC
    assert len(resp.content) > 5_000
    assert resp.headers["x-page-count"] == "1"
    assert _highlights(_client(), _PRICING_EXCERPT).json()["page_count"] == 1


def test_page_unknown_doc_is_404() -> None:
    client = _client()
    assert _page(client, doc_name="99_Does_Not_Exist.pdf").status_code == 404
    assert _page(client, solicitation_id="solicitation_99").status_code == 404
    # Real doc name under the wrong solicitation is still unknown (exact pair match only).
    assert _page(client, solicitation_id="solicitation_02").status_code == 404


def test_page_rejects_traversal_and_absolute_paths() -> None:
    client = _client()
    attacks = [
        "../04_Pricing_Schedule_CLINs.pdf",
        "../../../../etc/passwd",
        "/etc/passwd",
        "solicitation_01_cloud_infrastructure/04_Pricing_Schedule_CLINs.pdf",
        "..%2F..%2Fetc%2Fpasswd",
        "..\\..\\windows\\win.ini",
        "04_Pricing_Schedule_CLINs.pdf\x00.png",
    ]
    for name in attacks:
        resp = _page(client, doc_name=name)
        assert resp.status_code in (400, 404), (name, resp.status_code)
        assert resp.headers["content-type"].startswith("application/json"), name
    for sol in ["../solicitation_01", "/tmp", "solicitation_01/.."]:
        assert _page(client, solicitation_id=sol).status_code in (400, 404), sol


def test_page_bounds_and_bad_params_are_400() -> None:
    client = _client()
    assert _page(client, page=0).status_code == 400
    assert _page(client, page=-1).status_code == 400
    assert _page(client, page=2).status_code == 400  # 04_Pricing has exactly one page
    assert _page(client, page="abc").status_code == 400
    assert _page(client, dpi="high").status_code == 400
    missing = client.get("/api/v1/documents/page", params={"solicitation_id": _SOL, "page": 1})
    assert missing.status_code == 400
    assert "doc_name" in missing.json()["error"]


def test_dpi_is_clamped_to_72_200() -> None:
    assert documents.clamp_dpi(10) == 72
    assert documents.clamp_dpi(72) == 72
    assert documents.clamp_dpi(110) == 110
    assert documents.clamp_dpi(5000) == 200
    client = _client()
    low = _page(client, dpi=1)
    high = _page(client, dpi=99999)
    assert low.status_code == 200 and high.status_code == 200
    assert low.headers["x-render-dpi"] == "72"
    assert high.headers["x-render-dpi"] == "200"
    assert len(high.content) > len(low.content)


def test_oversized_render_is_rejected() -> None:
    original = documents.MAX_PNG_BYTES
    documents.MAX_PNG_BYTES = 1_000
    try:
        resp = _page(_client())
    finally:
        documents.MAX_PNG_BYTES = original
    assert resp.status_code == 413
    assert "exceeds" in resp.json()["error"]


def test_corpus_dir_override_only_serves_discovered_docs() -> None:
    src = documents.DEFAULT_CORPUS_DIR / "solicitation_01_cloud_infrastructure" / _PRICING
    with tempfile.TemporaryDirectory() as tmp:
        sol_dir = Path(tmp) / "solicitation_07_test"
        sol_dir.mkdir()
        shutil.copy(src, sol_dir / "only.pdf")
        (Path(tmp) / "secret.pdf").write_bytes(src.read_bytes())  # outside any solicitation_ dir
        app = create_app(settings=ArenaSettings(demo_mode="replay"))
        app.state.corpus_dir = Path(tmp)
        client = TestClient(app)
        ok = client.get("/api/v1/documents/page", params={"solicitation_id": "solicitation_07", "doc_name": "only.pdf", "page": 1})
        assert ok.status_code == 200
        hidden = client.get("/api/v1/documents/page", params={"solicitation_id": "solicitation_07", "doc_name": "secret.pdf", "page": 1})
        assert hidden.status_code == 404


def test_highlights_match_known_excerpt_with_normalized_rects() -> None:
    resp = _highlights(_client(), _PRICING_EXCERPT)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["matched"] is True
    assert body["method"] == "pymupdf.search_for"
    assert body["page_width"] == 612 and body["page_height"] == 792
    query_words = [w for w in body["query_used"].split() if w != "|"]
    assert len(query_words) >= 6 and " ".join(query_words) in _PRICING_EXCERPT.replace(" |", "")
    assert len(body["rects"]) >= 1
    for r in body["rects"]:
        assert 0.0 <= r["x0"] < r["x1"] <= 1.0
        assert 0.0 <= r["y0"] < r["y1"] <= 1.0
    # Title/header block sits in the top quarter of the page.
    assert min(r["y0"] for r in body["rects"]) < 0.25


def test_highlights_falls_back_to_longest_word_run() -> None:
    # First 80 chars cannot match (garbage prefix); a >= 6 word run from the real table row can.
    text = "zzqx yyqv wwpt " * 6 + "CLIN-0004-AB | Cloud Security Architect Labor | Hour | 2,000"
    body = _highlights(_client(), text).json()
    assert body["matched"] is True
    assert "Cloud Security Architect Labor" in body["query_used"]
    assert body["attempts"] <= documents.MAX_SEARCH_ATTEMPTS


def test_highlights_nonsense_text_never_invents_rects() -> None:
    body = _highlights(_client(), "purple elephant quantum lasagna orbit " * 4).json()
    assert body["matched"] is False
    assert body["rects"] == []
    assert body["method"] == "pymupdf.search_for"


def test_highlights_validates_text_and_page() -> None:
    client = _client()
    assert _highlights(client, "").status_code == 400
    assert _highlights(client, "x" * 601).status_code == 400
    assert _highlights(client, _PRICING_EXCERPT, page=5).status_code == 400
    assert _highlights(client, _PRICING_EXCERPT, doc_name="../x.pdf").status_code in (400, 404)


def test_normalize_rect_clamps_to_unit_square() -> None:
    r = documents.normalize_rect((-10.0, 700.0, 650.0, 820.0), 612.0, 792.0)
    assert r == {"x0": 0.0, "y0": round(700 / 792, 5), "x1": 1.0, "y1": 1.0}
    assert documents.normalize_rect((700.0, 10.0, 800.0, 20.0), 612.0, 792.0) is None


def test_word_runs_prefers_longest_contiguous_match() -> None:
    page_words = ["a", "|", "b", "c", "d", "e", "f", "g", "h", "x"]
    runs = documents.candidate_runs(["q", "b", "c", "d", "e", "f", "g", "h", "z"], page_words, min_words=6)
    assert runs[0] == "b c d e f g h"
    assert documents.candidate_runs(["b", "c"], page_words, min_words=6) == []


def test_word_runs_skip_table_separators_and_return_page_text() -> None:
    # Chunk text drops the '|' cell separators that the rendered table row carries.
    page_words = ["|", "CLIN-1", "|", "Cloud", "SQL", "|", "Hour", "|", "24", "|", "$3,800/mo", "|"]
    runs = documents.candidate_runs(["CLIN-1", "Cloud", "SQL", "Hour", "24", "$3,800/mo"], page_words, min_words=6)
    assert runs == ["CLIN-1 | Cloud SQL | Hour | 24 | $3,800/mo"]


def test_highlights_match_table_rows_without_separators() -> None:
    text = "CLIN-0003-AA Cloud SQL Enterprise Plus HA Instance 24 $3,800/mo $91,200 CLIN-0004-AB Cloud Security Architect Labor"
    body = _highlights(_client(), text).json()
    assert body["matched"] is True, body
    assert "|" in body["query_used"]


def test_upload_workspace_document_page_and_highlights_enforce_ownership_ready_status_and_replay_404() -> None:
    import pymupdf

    from fakes import live_settings
    from test_uploads_api import FakeManager, FakeServices, FakeStore, _ALICE, _BOB, _upload, _workspace

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "CLIN-9901-ZZ | Dedicated GPU Cluster | Month | 12 | $18,750.00")
    doc.new_page().insert_text((72, 72), "Page two failover topology us-east4")
    pdf_bytes = doc.tobytes()

    store = FakeStore()
    services = FakeServices(FakeManager(), store)
    app = create_app(settings=live_settings())
    app.state.upload_services = services
    with TestClient(app) as client:
        ws = _workspace(client)
        _upload(client, store, ws, pdf_bytes, name="Pricing.pdf")
        # Start a second session without finalizing — must never be served
        pending = client.post(
            f"/api/v1/uploads/{ws}/files",
            headers=_ALICE,
            json={"filename": "Pending.pdf", "size_bytes": len(pdf_bytes)},
        )
        assert pending.status_code == 201
        store.objects[pending.json()["object"]] = pdf_bytes

        ok_page = client.get(
            "/api/v1/documents/page",
            headers=_ALICE,
            params={"solicitation_id": ws, "doc_name": "Pricing.pdf", "page": 1},
        )
        assert ok_page.status_code == 200, ok_page.text
        assert ok_page.headers["content-type"] == "image/png"
        assert ok_page.headers["x-page-count"] == "2"
        assert ok_page.content[:8] == _PNG_MAGIC

        ok_hl = client.get(
            "/api/v1/documents/highlights",
            headers=_ALICE,
            params={
                "solicitation_id": ws,
                "doc_name": "Pricing.pdf",
                "page": 1,
                "text": "CLIN-9901-ZZ Dedicated GPU Cluster Month 12 $18,750.00",
            },
        )
        assert ok_hl.status_code == 200, ok_hl.text
        hl_body = ok_hl.json()
        assert hl_body["matched"] is True and hl_body["page_count"] == 2 and len(hl_body["rects"]) >= 1

        # Non-owner gets 404 (never leaks workspace existence)
        assert (
            client.get(
                "/api/v1/documents/page",
                headers=_BOB,
                params={"solicitation_id": ws, "doc_name": "Pricing.pdf", "page": 1},
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/api/v1/documents/highlights",
                headers=_BOB,
                params={"solicitation_id": ws, "doc_name": "Pricing.pdf", "page": 1, "text": "CLIN-9901-ZZ"},
            ).status_code
            == 404
        )
        # Unfinalized file gets 404
        assert (
            client.get(
                "/api/v1/documents/page",
                headers=_ALICE,
                params={"solicitation_id": ws, "doc_name": "Pending.pdf", "page": 1},
            ).status_code
            == 404
        )

    # Replay mode refuses upload workspace document reads with 404
    replay_client = _client()
    assert (
        replay_client.get(
            "/api/v1/documents/page",
            headers=_ALICE,
            params={"solicitation_id": ws, "doc_name": "Pricing.pdf", "page": 1},
        ).status_code
        == 404
    )

