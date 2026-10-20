"""Architectural hardening regression tests retained from v2 (I1/I3/I4) and re-pointed at v3 contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from starlette.testclient import TestClient

from app.core import gcp_clients
from app.core.config import ArenaSettings
from app.core.models import BoundingBox, DocumentChunk
from app.ingestion import session_store
from app.ingestion.bq_indexer import build_bigquery_ddl, build_hybrid_rrf_sql
from app.ingestion.parsers import _extract_pages_lines
from app.ingestion.pipeline import chunk_to_bigquery_row
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = PROJECT_ROOT / "data" / "sample_docs" / "procurement_corpus"


def _make_sample_chunk(
    chunk_id: str = "up_chunk_1",
    tier: str = "multimodal",
    sol_id: str = "upload_live9999",
    sec_id: str = "CLIN-9999",
    content: str = "CLIN-9999 Cloud Migration Architect rate is $275.00/hr for 1,000 hours.",
) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        tier_profile=tier,
        solicitation_id=sol_id,
        doc_name="Uploaded_Solicitation.pdf",
        doc_type="BASE_RFP",
        element_type="table" if tier == "multimodal" else "narrative_text",
        section_id=sec_id,
        amendment_number=0,
        effective_date="2026-03-01",
        amends_section=None,
        page_number=1,
        bbox=BoundingBox(page=1, x0=0.1, y0=0.1, x1=0.9, y1=0.5),
        content=content,
        score=0.92,
    )


def test_i1_gcp_client_singleton_caching() -> None:
    """Verify gcp_clients caches SDK clients per ArenaSettings and supports cache clearing."""
    import sys
    import types

    gcp_clients.clear_client_cache()
    created_counts = {"genai": 0, "bq": 0, "rank": 0, "search": 0}

    class _FakeGenAIClient:
        def __init__(self, **kwargs: Any) -> None:
            created_counts["genai"] += 1
            self.kwargs = kwargs

    class _FakeBQClient:
        def __init__(self, **kwargs: Any) -> None:
            created_counts["bq"] += 1
            self.kwargs = kwargs

    class _FakeRankClient:
        def __init__(self, **kwargs: Any) -> None:
            created_counts["rank"] += 1

    class _FakeSearchClient:
        def __init__(self, **kwargs: Any) -> None:
            created_counts["search"] += 1

    fake_google = types.ModuleType("google")
    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = _FakeGenAIClient  # type: ignore[attr-defined]
    fake_google.genai = fake_genai  # type: ignore[attr-defined]

    fake_cloud = types.ModuleType("google.cloud")
    fake_bq = types.ModuleType("google.cloud.bigquery")
    fake_bq.Client = _FakeBQClient  # type: ignore[attr-defined]
    fake_de = types.ModuleType("google.cloud.discoveryengine_v1")
    fake_de.RankServiceClient = _FakeRankClient  # type: ignore[attr-defined]
    fake_de.SearchServiceClient = _FakeSearchClient  # type: ignore[attr-defined]
    fake_cloud.bigquery = fake_bq  # type: ignore[attr-defined]
    fake_cloud.discoveryengine_v1 = fake_de  # type: ignore[attr-defined]
    fake_google.cloud = fake_cloud  # type: ignore[attr-defined]

    settings = ArenaSettings(
        demo_mode="live",
        gcp_project_id="cache-test-proj",
        gcp_location="us-central1",
        gemini_fast_model="gemini-4-argon",
        gemini_pro_model="gemini-4-argon",
    )

    with patch.dict(
        sys.modules,
        {
            "google": fake_google,
            "google.genai": fake_genai,
            "google.cloud": fake_cloud,
            "google.cloud.bigquery": fake_bq,
            "google.cloud.discoveryengine_v1": fake_de,
        },
    ):
        g1 = gcp_clients.get_genai_client(settings)
        g2 = gcp_clients.get_genai_client(settings)
        assert g1 is g2
        assert created_counts["genai"] == 1

        b1 = gcp_clients.get_bigquery_client(settings)
        b2 = gcp_clients.get_bigquery_client(settings)
        assert b1 is b2
        assert created_counts["bq"] == 1

        r1 = gcp_clients.get_rank_client(settings)
        r2 = gcp_clients.get_rank_client(settings)
        assert r1 is r2
        assert created_counts["rank"] == 1

        s1 = gcp_clients.get_search_client(settings)
        s2 = gcp_clients.get_search_client(settings)
        assert s1 is s2
        assert created_counts["search"] == 1

        gcp_clients.clear_client_cache()
        g3 = gcp_clients.get_genai_client(settings)
        assert g3 is not g1
        assert created_counts["genai"] == 2
        gcp_clients.clear_client_cache()


def test_i1_and_i4_session_store_canonical_key_bound_and_file_persistence() -> None:
    """Verify session_store never exceeds _MAX_SESSIONS and persists across worker memory resets."""
    session_store.clear_uploaded_sessions()
    for idx in range(60):
        sid = f"sess{idx:04d}abcdef"
        chunk = _make_sample_chunk(chunk_id=f"c_{idx}", sol_id=f"upload_{sid[:8]}")
        session_store.store_uploaded_session_chunks(
            sid,
            {"naive": [chunk], "hybrid": [chunk], "multimodal": [chunk]},
        )

    assert len(session_store._UPLOADED_SESSIONS) <= session_store._MAX_SESSIONS

    latest_sid = "sess0059abcdef"
    latest_sol_id = f"upload_{latest_sid[:8]}"
    assert len(session_store.get_uploaded_session_chunks(latest_sid, "multimodal")) == 1
    assert len(session_store.get_uploaded_session_chunks(latest_sol_id, "multimodal")) == 1

    # Simulate another Uvicorn worker with an empty in-memory dictionary
    session_store._UPLOADED_SESSIONS.clear()
    reloaded = session_store.get_uploaded_session_chunks(latest_sol_id, "multimodal")
    assert len(reloaded) == 1
    assert reloaded[0].chunk_id == "c_59"

    session_store.clear_uploaded_sessions()


def test_i1_and_i4_upload_content_length_guard_and_mime_multipart_boundary() -> None:
    """Verify pre-read Content-Length enforcement and standard MIME multipart boundary parsing."""
    app = create_app(settings=ArenaSettings(demo_mode="replay"))
    client = TestClient(app)

    # 1. Oversized Content-Length header rejected immediately
    resp_oversized = client.post(
        "/api/v1/ingest/upload",
        content=b"%PDF-1.4 small body %%EOF",
        headers={
            "Content-Type": "application/pdf",
            "Content-Length": str(50 * 1024 * 1024),
        },
    )
    assert resp_oversized.status_code == 400
    assert "exceeds maximum" in resp_oversized.json()["error"].lower()

    # 2. Multipart form-data with decoy %PDF- / %%EOF in an earlier text field
    sample_pdf = (
        CORPUS_DIR
        / "solicitation_01_cloud_infrastructure"
        / "06_Amendment_0001_Scope_Update.pdf"
    )
    valid_pdf_bytes = sample_pdf.read_bytes()

    boundary = "----RagArenaBoundary987654321"
    multipart_body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="metadata_note"\r\n\r\n'
        "Decoy header with %PDF-fake-stream and trailing %%EOF marker inside text field\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="boundary_test.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("latin-1") + valid_pdf_bytes + f"\r\n--{boundary}--\r\n".encode("latin-1")

    resp_multi = client.post(
        "/api/v1/ingest/upload",
        content=multipart_body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert resp_multi.status_code == 200
    payload = resp_multi.json()
    assert payload["status"] == "parsed" and payload["indexed"] is False
    assert payload["upload"]["filename"] == "boundary_test.pdf"
    session_store.clear_uploaded_sessions()


def test_i3_bigquery_ddl_clustering_dual_embeddings_and_single_page_pdf_slice() -> None:
    """Verify BigQuery DDL includes clustering + multimodal vector column and pipeline slices 1 PDF page."""
    ddl = build_bigquery_ddl("demo-proj", "rag_arena", "document_chunks")
    assert "CLUSTER BY tier_profile, solicitation_id" in ddl
    assert "embedding ARRAY<FLOAT64>" in ddl
    assert "embedding_multimodal ARRAY<FLOAT64>" in ddl
    assert "ingest_run_id STRING NOT NULL" in ddl and "layout_source STRING" in ddl
    assert "CREATE TABLE IF NOT EXISTS `demo-proj.rag_arena.ingest_runs`" in ddl
    assert "CREATE TABLE IF NOT EXISTS `demo-proj.rag_arena.query_runs`" in ddl
    for col in ("embedding_model STRING", "stage_log JSON", "groundedness FLOAT64", "demo_mode STRING", "git_sha STRING"):
        assert col in ddl

    sql_default = build_hybrid_rrf_sql("demo-proj", "rag_arena", "document_chunks")
    assert "'embedding'" in sql_default
    sql_mm = build_hybrid_rrf_sql(
        "demo-proj",
        "rag_arena",
        "document_chunks",
        embedding_column="embedding_multimodal",
    )
    assert "'embedding_multimodal'" in sql_mm

    mm_chunk = _make_sample_chunk(tier="multimodal")
    row = chunk_to_bigquery_row(mm_chunk, embedding=[0.1, 0.2, 0.3], ingest_run_id="ing_1", layout_source="pymupdf")
    assert row["embedding_multimodal"] == [0.1, 0.2, 0.3]
    assert row["ingest_run_id"] == "ing_1" and row["layout_source"] == "pymupdf"

    # Verify extract_single_page_pdf_bytes extracts only page 2 from a multi-page corpus PDF
    multi_page_pdf = (
        CORPUS_DIR
        / "solicitation_01_cloud_infrastructure"
        / "01_RFP_Base_Solicitation.pdf"
    )
    full_bytes = multi_page_pdf.read_bytes()
    full_pages = _extract_pages_lines(full_bytes)
    assert len(full_pages) >= 2

    from app.ingestion.pipeline import extract_single_page_pdf_bytes

    sliced_bytes = extract_single_page_pdf_bytes(multi_page_pdf, page_number=2)
    assert len(sliced_bytes) < len(full_bytes)
    sliced_pages = _extract_pages_lines(sliced_bytes)
    assert len(sliced_pages) == 1
    assert sliced_pages[0] == full_pages[1]


def test_live_bigquery_hybrid_rrf_sql_handles_hyphens_and_token_fallback() -> None:
    """Verify sanitize_search_query preserves clause IDs while build_hybrid_rrf_sql sanitizes hyphens via REGEXP_REPLACE + token fallback."""
    from app.ingestion.bq_indexer import build_hybrid_rrf_sql, sanitize_search_query

    sanitized = sanitize_search_query(
        "What is the amended hourly rate for CLIN-0004-AB (Cloud Security Architect) under SEC-4.2.1?"
    )
    assert "CLIN-0004-AB" in sanitized
    assert "SEC-4.2.1" in sanitized
    assert "(" not in sanitized
    assert "?" not in sanitized

    sql = build_hybrid_rrf_sql("example-project", "rag_arena", "document_chunks")
    assert "SEARCH(content, REGEXP_REPLACE(@query_text, r'[^a-zA-Z0-9 ]', ' '))" in sql
    assert "UNNEST(SPLIT(LOWER(REGEXP_REPLACE(@query_text" in sql
    assert "COUNTIF(" in sql


