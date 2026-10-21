from pathlib import Path

import pytest
from app.core.config import ArenaSettings, get_settings
from app.core.models import (
    AgentStep,
    BattleRequest,
    BoundingBox,
    Citation,
    DocumentChunk,
    EngineResult,
    EvalMetrics,
    UploadMetadata,
)
from app.ingestion.bq_indexer import build_bigquery_ddl, build_hybrid_rrf_sql
from app.ingestion.parsers import parse_pdf_Fallback_or_fitz
from app.ingestion.upload_guard import UploadGuardError, validate_pdf_upload

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = PROJECT_ROOT / "data" / "sample_docs" / "procurement_corpus"


def test_settings_default_to_2026_models_and_replay_mode():
    settings = get_settings()
    assert settings.demo_mode in ("replay", "live")
    assert settings.demo_mode == "replay"
    assert "2.0" not in settings.gemini_fast_model
    assert "1.5" not in settings.gemini_fast_model
    assert "2.5" not in settings.gemini_fast_model
    assert "3.1" not in settings.gemini_fast_model
    assert settings.gemini_fast_model == "gemini-4-argon"
    assert settings.gemini_pro_model == "gemini-4-argon"
    assert settings.gemini_reasoning_model == "gemini-4-argon"
    assert settings.gemini_thinking_level == "high"
    assert settings.text_embedding_model == "gemini-embedding-2"
    assert settings.multimodal_embedding_model == "gemini-embedding-2"
    assert settings.reranker_model == "semantic-ranker-default@latest"
    assert settings.max_upload_bytes == 10 * 1024 * 1024
    assert settings.max_upload_pages == 30

    custom = get_settings(
        {
            "GEMINI_FAST_MODEL": "gemini-4-argon",
            "GEMINI_REASONING_MODEL": "gemini-4-argon",
            "GEMINI_THINKING_LEVEL": "high",
            "CLOUDSDK_CONFIG": "/tmp/custom-gcloud-profile",
            "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/custom-adc.json",
        }
    )
    assert custom.gemini_fast_model == "gemini-4-argon"
    assert custom.gemini_pro_model == "gemini-4-argon"
    assert custom.gemini_reasoning_model == "gemini-4-argon"
    assert custom.gemini_thinking_level == "high"
    assert custom.cloudsdk_config == "/tmp/custom-gcloud-profile"
    assert custom.google_application_credentials == "/tmp/custom-adc.json"

    with pytest.raises(ValueError, match="Invalid demo_mode"):
        ArenaSettings(demo_mode="invalid_mode")

    with pytest.raises(ValueError, match="Deprecated or forbidden Gemini model"):
        ArenaSettings(gemini_fast_model="gemini-2.0-flash")

    with pytest.raises(ValueError, match="Deprecated or forbidden Gemini model"):
        ArenaSettings(gemini_pro_model="gemini-2.5-pro")

    with pytest.raises(ValueError, match="Deprecated or forbidden Gemini model"):
        ArenaSettings(text_embedding_model="gemini-embedding-001")


def test_upload_guard_rejects_non_pdf_and_oversized_files():
    with pytest.raises(UploadGuardError, match="Invalid PDF magic header"):
        validate_pdf_upload(b"NOT_A_PDF_CONTENT", filename="malicious.pdf")

    with pytest.raises(UploadGuardError, match="must end with .pdf"):
        validate_pdf_upload(b"%PDF-1.4\n", filename="malicious.exe")

    oversized = b"%PDF-1.4\n" + (b"0" * (10 * 1024 * 1024 + 100))
    with pytest.raises(UploadGuardError, match="exceeds 10 MB limit"):
        validate_pdf_upload(oversized, filename="huge.pdf")


def test_upload_guard_accepts_valid_solicitation_pdf():
    sample_pdf = (
        CORPUS_DIR
        / "solicitation_01_cloud_infrastructure"
        / "06_Amendment_0001_Scope_Update.pdf"
    )
    meta = validate_pdf_upload(sample_pdf.read_bytes(), filename=sample_pdf.name)
    assert isinstance(meta, UploadMetadata)
    assert meta.page_count >= 1
    assert meta.size_bytes == len(sample_pdf.read_bytes())
    assert len(meta.sha256_hex) == 64
    assert meta.session_id
    assert meta.expires_at is not None


def test_pdf_parser_extracts_chunks_with_normalized_bboxes_and_amendment_metadata():
    amendment_pdf = (
        CORPUS_DIR
        / "solicitation_01_cloud_infrastructure"
        / "06_Amendment_0001_Scope_Update.pdf"
    )
    chunks = parse_pdf_Fallback_or_fitz(
        pdf_path=amendment_pdf,
        solicitation_id="solicitation_01",
        tier_profile="multimodal",
    )
    assert len(chunks) >= 1
    first = chunks[0]
    assert isinstance(first, DocumentChunk)
    assert first.doc_type == "AMENDMENT"
    assert first.amendment_number == 1
    assert first.amends_section is not None
    assert "CLIN-0004-AB" in first.amends_section or "SEC-4.2.1" in first.amends_section
    assert "$245.00/hr" in first.content
    assert 0.0 <= first.bbox.x0 < first.bbox.x1 <= 1.0
    assert 0.0 <= first.bbox.y0 < first.bbox.y1 <= 1.0

    diagram_pdf = (
        CORPUS_DIR
        / "solicitation_01_cloud_infrastructure"
        / "03_Technical_Exhibit_Architecture.pdf"
    )
    diag_chunks = parse_pdf_Fallback_or_fitz(
        pdf_path=diagram_pdf,
        solicitation_id="solicitation_01",
        tier_profile="multimodal",
    )
    assert len(diag_chunks) >= 1
    assert any(c.element_type == "diagram" or "Cloud HA VPN" in c.content for c in diag_chunks)


def test_bigquery_ddl_and_hybrid_rrf_sql_include_provenance_columns():
    ddl = build_bigquery_ddl(project_id="demo-proj", dataset_id="rag_arena")
    assert "CREATE TABLE IF NOT EXISTS `demo-proj.rag_arena.document_chunks`" in ddl
    assert "amendment_number INT64" in ddl
    assert "amends_section STRING" in ddl
    assert "CREATE VECTOR INDEX IF NOT EXISTS" in ddl
    assert "CREATE SEARCH INDEX IF NOT EXISTS" in ddl

    sql = build_hybrid_rrf_sql(project_id="demo-proj", dataset_id="rag_arena")
    assert "VECTOR_SEARCH" in sql
    assert "SEARCH(" in sql
    assert "60 +" in sql

    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        build_bigquery_ddl(project_id="bad; DROP TABLE", dataset_id="rag_arena")


def test_models_validation_and_serialization():
    bbox = BoundingBox(page=1, x0=0.1, y0=0.2, x1=0.9, y1=0.8)
    assert bbox.to_dict()["page"] == 1

    with pytest.raises(ValueError):
        BoundingBox(page=0, x0=0.1, y0=0.2, x1=0.9, y1=0.8)

    with pytest.raises(ValueError):
        BoundingBox(page=1, x0=0.9, y0=0.2, x1=0.1, y1=0.8)


def test_gcp_clients_reject_replay_mode():
    from app.core.gcp_clients import get_bigquery_client, get_genai_client, get_rank_client

    replay_settings = get_settings({"DEMO_MODE": "replay"})
    with pytest.raises(RuntimeError, match="demo_mode is 'replay'"):
        get_genai_client(replay_settings)
    with pytest.raises(RuntimeError, match="demo_mode is 'replay'"):
        get_bigquery_client(replay_settings)
    with pytest.raises(RuntimeError, match="demo_mode is 'replay'"):
        get_rank_client(replay_settings)


def test_tier_specific_chunking_differentiates_naive_hybrid_and_multimodal():
    pricing_pdf = (
        CORPUS_DIR
        / "solicitation_01_cloud_infrastructure"
        / "04_Pricing_Schedule_CLINs.pdf"
    )
    diagram_pdf = (
        CORPUS_DIR
        / "solicitation_01_cloud_infrastructure"
        / "03_Technical_Exhibit_Architecture.pdf"
    )

    naive_chunks = parse_pdf_Fallback_or_fitz(
        pdf_path=pricing_pdf,
        solicitation_id="solicitation_01",
        tier_profile="naive",
    )
    hybrid_chunks = parse_pdf_Fallback_or_fitz(
        pdf_path=pricing_pdf,
        solicitation_id="solicitation_01",
        tier_profile="hybrid",
    )
    mm_chunks = parse_pdf_Fallback_or_fitz(
        pdf_path=pricing_pdf,
        solicitation_id="solicitation_01",
        tier_profile="multimodal",
    )

    assert len(naive_chunks) > len(mm_chunks)
    assert all("|" not in c.content for c in naive_chunks)
    assert all(c.element_type == "narrative_text" for c in naive_chunks)
    assert all(len(c.content) <= 360 for c in naive_chunks)
    assert all("_w" in c.chunk_id and c.chunk_id.endswith("_naive") for c in naive_chunks)

    assert all(
        c.content.startswith("[Contextual Lineage: Solicitation=solicitation_01 |")
        for c in hybrid_chunks
    )
    assert any(c.element_type == "table" and "|" in c.content for c in mm_chunks)

    mm_diag_chunks = parse_pdf_Fallback_or_fitz(
        pdf_path=diagram_pdf,
        solicitation_id="solicitation_01",
        tier_profile="multimodal",
    )
    assert any(
        c.element_type == "diagram"
        and c.content.startswith("[Multimodal Diagram Topology | Visual Elements Extracted]")
        for c in mm_diag_chunks
    )


def test_sanitize_search_query_and_prefiltered_hybrid_rrf_sql():
    from app.engines.level1_naive import build_vector_search_sql
    from app.ingestion.bq_indexer import sanitize_search_query

    cleaned = sanitize_search_query(
        "What is the rate for CLIN-0004-AB & SEC-4.2.1? ($245.00/hr!)"
    )
    assert "CLIN-0004-AB" in cleaned
    assert "SEC-4.2.1" in cleaned
    assert "?" not in cleaned
    assert "&" not in cleaned
    assert "(" not in cleaned

    with pytest.raises(ValueError, match="non-empty"):
        sanitize_search_query("   ")

    sql_unfiltered = build_hybrid_rrf_sql(
        project_id="demo-proj",
        dataset_id="rag_arena",
        include_solicitation_filter=False,
    )
    assert "(SELECT * FROM `demo-proj.rag_arena.document_chunks` WHERE tier_profile = @tier_profile)" in sql_unfiltered

    sql_filtered = build_hybrid_rrf_sql(
        project_id="demo-proj",
        dataset_id="rag_arena",
        include_solicitation_filter=True,
    )
    assert (
        "(SELECT * FROM `demo-proj.rag_arena.document_chunks` WHERE tier_profile = @tier_profile AND solicitation_id = @solicitation_id)"
        in sql_filtered
    )
    assert "AND solicitation_id = @solicitation_id" in sql_filtered

    vec_sql = build_vector_search_sql(
        project_id="demo-proj",
        dataset_id="rag_arena",
        include_solicitation_filter=True,
    )
    assert (
        "(SELECT * FROM `demo-proj.rag_arena.document_chunks` WHERE tier_profile = 'naive' AND solicitation_id = @solicitation_id)"
        in vec_sql
    )


def test_corpus_ingestion_pipeline_and_bigquery_row_serialization():
    from app.ingestion.pipeline import chunk_to_bigquery_row, collect_corpus_chunks

    chunks = collect_corpus_chunks(tiers=("naive", "hybrid", "multimodal"))
    assert len(chunks) > 54
    tiers_present = {c.tier_profile for c in chunks}
    assert tiers_present == {"naive", "hybrid", "multimodal"}

    row = chunk_to_bigquery_row(chunks[0], embedding=[0.125, -0.25, 0.5])
    assert row["chunk_id"] == chunks[0].chunk_id
    assert row["tier_profile"] == chunks[0].tier_profile
    assert isinstance(row["bbox"], dict)
    assert set(row["bbox"].keys()) == {"page", "x0", "y0", "x1", "y1"}
    assert row["embedding"] == [0.125, -0.25, 0.5]


def test_seed_bigquery_live_mode_with_mock_clients():
    import sys

    scripts_dir = str(PROJECT_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from seed_bigquery import run_seed

    called_models: list[str] = []
    inserted_batches: list[list[dict]] = []

    class _FakeEmbedding:
        values = [0.1, 0.2, 0.3, 0.4]

    class _FakeEmbedResp:
        embeddings = [_FakeEmbedding()]

    class _FakeModels:
        def embed_content(self, *, model: str, contents: object) -> _FakeEmbedResp:
            called_models.append(model)
            return _FakeEmbedResp()

    class _FakeGenAIClient:
        models = _FakeModels()

    class _FakeBQClient:
        def insert_rows_json(self, table_fqn: str, rows: list[dict]) -> list:
            assert "document_chunks" in table_fqn
            inserted_batches.append(rows)
            return []

    summary = run_seed(
        dry_run=False,
        corpus_dir=CORPUS_DIR,
        genai_client=_FakeGenAIClient(),
        bq_client=_FakeBQClient(),
    )
    from app.ingestion.pipeline import collect_corpus_chunks

    expected = len(collect_corpus_chunks(CORPUS_DIR))  # measured from the active parser, never a hand-typed count
    assert expected > 0
    assert summary["status"] == "seeded"
    assert summary["inserted_rows"] == expected
    assert set(summary["tier_counts"].keys()) == {"naive", "hybrid", "multimodal"}
    assert sum(summary["tier_counts"].values()) == expected
    assert called_models.count("gemini-embedding-2") == expected
    assert "gemini-embedding-001" not in called_models
    assert sum(len(b) for b in inserted_batches) == expected


def test_pymupdf_extraction_keeps_text_past_the_right_page_edge() -> None:
    """Metadata banners in the generated corpus overflow x=612; mediabox clipping used to drop 'AMENDS=NONE'."""
    from app.ingestion.parsers import _extract_pages_with_pymupdf, pymupdf_available

    assert pymupdf_available()
    pdf = CORPUS_DIR / "solicitation_01_cloud_infrastructure" / "03_Technical_Exhibit_Architecture.pdf"
    lines = _extract_pages_with_pymupdf(pdf.read_bytes())[0]
    meta = [ln for ln in lines if ln.startswith("DOC_TYPE=")]
    assert meta and meta[0].endswith("AMENDS=NONE")
