# The RAG Arena (v2.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build The RAG Arena, a full-stack enterprise RAG benchmarking platform on Google Cloud comparing 4 progressive RAG tiers (Naive, Hybrid+Rerank, Multimodal, and Google ADK Agentic) against Managed Vertex AI Search over a 25-document procurement corpus, with zero-credential replay mode and public GitHub packaging.

**Architecture:** A FastAPI Python backend implementing 5 pluggable RAG engines with Server-Sent Events (SSE) streaming, decoupled LLM-as-a-judge evaluation, normalized bounding-box provenance, and temporal contract amendment supersession over BigQuery (`VECTOR_SEARCH` + `SEARCH()` RRF), paired with a React + TypeScript + Tailwind CSS frontend featuring side-by-side battle views, an ADK reasoning timeline, and an SVG bounding-box document viewer.

**Tech Stack:** Python 3.11+, FastAPI, `google-genai`, `google-adk`, `google-cloud-bigquery`, `google-cloud-discoveryengine`, `google-cloud-documentai`, `PyMuPDF` (`fitz`), Pytest, React 18, TypeScript, Vite, Tailwind CSS, Docker Compose, Terraform.

---

### Task 1: Deterministic Procurement Corpus Compiler & Golden Benchmarks (`Phase 1`)

**Files:**
- Create: `scripts/build_procurement_corpus.py`
- Modify: `scripts/download_procurement_corpus.sh`
- Create: `data/golden_benchmarks/procurement_benchmark.json`
- Create: `data/golden_benchmarks/precomputed_runs.json`
- Test: `backend/tests/test_corpus_and_benchmarks.py`

- [x] **Step 1: Write the failing test for corpus alignment, benchmark questions, and precomputed replay fixtures**

Create `backend/tests/test_corpus_and_benchmarks.py`:
```python
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = PROJECT_ROOT / "data" / "sample_docs" / "procurement_corpus"
BENCHMARK_FILE = PROJECT_ROOT / "data" / "golden_benchmarks" / "procurement_benchmark.json"
REPLAY_FILE = PROJECT_ROOT / "data" / "golden_benchmarks" / "precomputed_runs.json"


def test_interconnected_solicitation_pdfs_exist():
    expected_pdfs = [
        CORPUS_DIR / "solicitation_01_cloud_infrastructure" / "01_RFP_Base_Solicitation.pdf",
        CORPUS_DIR / "solicitation_01_cloud_infrastructure" / "03_Technical_Exhibit_Architecture.pdf",
        CORPUS_DIR / "solicitation_01_cloud_infrastructure" / "04_Pricing_Schedule_CLINs.pdf",
        CORPUS_DIR / "solicitation_01_cloud_infrastructure" / "06_Amendment_0001_Scope_Update.pdf",
        CORPUS_DIR / "solicitation_02_data_analytics_platform" / "01_RFP_Data_Platform.pdf",
        CORPUS_DIR / "solicitation_02_data_analytics_platform" / "02_Architecture_Requirements.pdf",
        CORPUS_DIR / "solicitation_02_data_analytics_platform" / "05_Amendments_and_Addenda.pdf",
        CORPUS_DIR / "solicitation_03_cybersecurity_operations" / "02_Zero_Trust_Architecture_Specs.pdf",
        CORPUS_DIR / "solicitation_03_cybersecurity_operations" / "03_Incident_Response_SLAs.pdf",
        CORPUS_DIR / "solicitation_03_cybersecurity_operations" / "04_Pricing_Labor_Categories.pdf",
    ]
    for pdf_path in expected_pdfs:
        assert pdf_path.exists(), f"Missing required solicitation PDF: {pdf_path}"
        raw = pdf_path.read_bytes()
        assert raw.startswith(b"%PDF-"), f"Invalid PDF header in {pdf_path}"
        assert len(raw) > 500, f"PDF too small: {pdf_path}"


def test_golden_benchmark_schema_and_categories():
    assert BENCHMARK_FILE.exists(), "Missing procurement_benchmark.json"
    data = json.loads(BENCHMARK_FILE.read_text())
    assert "questions" in data
    questions = data["questions"]
    assert len(questions) >= 8
    categories = {q["failure_mode_category"] for q in questions}
    assert {
        "exact_clause_and_clin",
        "table_matrix_extraction",
        "diagram_topology",
        "multi_hop_amendment_and_math",
    }.issubset(categories)


def test_precomputed_replay_runs_cover_all_engines():
    assert REPLAY_FILE.exists(), "Missing precomputed_runs.json"
    runs = json.loads(REPLAY_FILE.read_text())
    assert "questions" in runs
    assert "leaderboard" in runs
    first_q = next(iter(runs["questions"].values()))
    expected_engines = {
        "level1_naive",
        "level2_hybrid",
        "level3_multimodal",
        "level4_agentic",
        "managed_vertex",
    }
    assert expected_engines.issubset(set(first_q.keys()))
    l4 = first_q["level4_agentic"]
    assert len(l4["agent_steps"]) >= 2
    assert len(l4["citations"]) >= 1
    bbox = l4["citations"][0]["bbox"]
    assert 0.0 <= bbox["x0"] < bbox["x1"] <= 1.0
    assert 0.0 <= bbox["y0"] < bbox["y1"] <= 1.0
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest backend/tests/test_corpus_and_benchmarks.py -v`
Expected: FAIL (`AssertionError: Missing required solicitation PDF`)

- [x] **Step 3: Implement `scripts/build_procurement_corpus.py`, update `scripts/download_procurement_corpus.sh`, and create golden benchmark & replay JSON files**

Create `scripts/build_procurement_corpus.py` to deterministically generate valid multi-page PDF documents (including embedded vector diagrams `/Subtype /Form`, CLIN tables, SLA matrices, and `Amendment_0001` / `Amendment_0002` superseding clauses) for all 18 solicitation files listed in `spec.md`, and generate `data/golden_benchmarks/procurement_benchmark.json` and `data/golden_benchmarks/precomputed_runs.json`.

- [x] **Step 4: Run test to verify it passes**

Run: `python3 scripts/build_procurement_corpus.py && pytest backend/tests/test_corpus_and_benchmarks.py -v`
Expected: PASS (`3 passed`)

---

### Task 2: Backend Core, Schemas, Upload Guardrails, and Ingestion Pipeline (`Phase 2`)

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/requirements.txt`
- Create: `backend/app/__init__.py`
- Create: `backend/app/core/__init__.py`
- Create: `backend/app/core/config.py`
- Create: `backend/app/core/models.py`
- Create: `backend/app/core/gcp_clients.py`
- Create: `backend/app/ingestion/__init__.py`
- Create: `backend/app/ingestion/upload_guard.py`
- Create: `backend/app/ingestion/parsers.py`
- Create: `backend/app/ingestion/bq_indexer.py`
- Test: `backend/tests/test_ingestion_and_guard.py`

- [x] **Step 1: Write the failing test for upload guardrails, bounding-box parsing, and BigQuery DDL generation**

Create `backend/tests/test_ingestion_and_guard.py`:
```python
import pytest
from app.core.config import get_settings
from app.core.models import BoundingBox, DocumentChunk
from app.ingestion.upload_guard import UploadGuardError, validate_pdf_upload
from app.ingestion.parsers import parse_pdf_Fallback_or_fitz
from app.ingestion.bq_indexer import build_bigquery_ddl, build_hybrid_rrf_sql


def test_settings_default_to_2026_models_and_replay_mode():
    settings = get_settings()
    assert settings.demo_mode in ("replay", "live")
    assert "2.0" not in settings.gemini_fast_model
    assert "3.1" not in settings.gemini_fast_model
    assert settings.gemini_fast_model == "gemini-4-argon"
    assert settings.gemini_pro_model == "gemini-4-argon"
    assert settings.gemini_thinking_level == "high"
    assert settings.text_embedding_model == "gemini-embedding-2"
    assert settings.multimodal_embedding_model == "gemini-embedding-2"


def test_upload_guard_rejects_non_pdf_and_oversized_files():
    with pytest.raises(UploadGuardError, match="Invalid PDF magic header"):
        validate_pdf_upload(b"NOT_A_PDF_CONTENT", filename="malicious.exe")

    oversized = b"%PDF-1.4\n" + (b"0" * (10 * 1024 * 1024 + 100))
    with pytest.raises(UploadGuardError, match="exceeds 10 MB limit"):
        validate_pdf_upload(oversized, filename="huge.pdf")


def test_upload_guard_accepts_valid_solicitation_pdf():
    from pathlib import Path
    sample_pdf = (
        Path(__file__).resolve().parents[2]
        / "data/sample_docs/procurement_corpus/solicitation_01_cloud_infrastructure/06_Amendment_0001_Scope_Update.pdf"
    )
    meta = validate_pdf_upload(sample_pdf.read_bytes(), filename=sample_pdf.name)
    assert meta.page_count >= 1
    assert meta.session_id
    assert meta.expires_at is not None


def test_bigquery_ddl_and_hybrid_rrf_sql_include_provenance_columns():
    ddl = build_bigquery_ddl(project_id="demo-proj", dataset_id="rag_arena")
    assert "CREATE TABLE IF NOT EXISTS `demo-proj.rag_arena.document_chunks`" in ddl
    assert "amendment_number INT64" in ddl
    assert "amends_section STRING" in ddl
    assert "CREATE VECTOR INDEX" in ddl
    assert "CREATE SEARCH INDEX" in ddl

    sql = build_hybrid_rrf_sql(project_id="demo-proj", dataset_id="rag_arena")
    assert "VECTOR_SEARCH" in sql
    assert "SEARCH(" in sql
    assert "60 +" in sql
```

- [x] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=backend pytest backend/tests/test_ingestion_and_guard.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app'`)

- [x] **Step 3: Implement `config.py`, `models.py`, `gcp_clients.py`, `upload_guard.py`, `parsers.py`, and `bq_indexer.py`**

Implement bounded, validated units following the Engineering Canon (explicit bounds on file sizes, page counts, and query timeouts; ≥2 input validations at unit boundaries; zero silent errors).

- [x] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=backend pytest backend/tests/test_ingestion_and_guard.py -v`
Expected: PASS (`4 passed`)

---

### Task 3: The 4 Progressive RAG Engines + Managed Vertex AI Search + Replay Engine (`Phase 3`)

**Files:**
- Create: `backend/app/engines/__init__.py`
- Create: `backend/app/engines/base.py`
- Create: `backend/app/engines/level1_naive.py`
- Create: `backend/app/engines/level2_hybrid.py`
- Create: `backend/app/engines/level3_multimodal.py`
- Create: `backend/app/engines/level4_agentic.py`
- Create: `backend/app/engines/managed_vertex.py`
- Create: `backend/app/engines/replay_engine.py`
- Test: `backend/tests/test_engines.py`

- [x] **Step 1: Write the failing test for all 5 RAG engines, Level 4 ADK tools (amendment supersession & safe math), and Replay Engine**

Create `backend/tests/test_engines.py`:
```python
import pytest
from app.engines.level4_agentic import (
    calculate_contract_pricing,
    check_amendments,
    verify_citations,
)
from app.engines.replay_engine import ReplayRAGEngine


def test_level4_safe_calculator_tool_evaluates_math_and_blocks_unsafe_expr():
    res = calculate_contract_pricing(
        expression="base_hours * amended_rate * (1 - discount)",
        variables={"base_hours": 2000, "amended_rate": 245.0, "discount": 0.05},
    )
    assert res["status"] == "success"
    assert res["result"] == pytest.approx(465500.0)

    bad = calculate_contract_pricing(
        expression="__import__('os').system('ls')",
        variables={},
    )
    assert bad["status"] == "error"


def test_level4_amendment_tool_detects_superseding_clauses():
    res = check_amendments(
        solicitation_id="solicitation_01",
        section_or_clin_id="CLIN-0004-AB",
    )
    assert res["status"] == "success"
    assert res["has_amendment"] is True
    assert "Amendment 0001" in res["superseding_summary"]
    assert "$245" in res["superseding_summary"]


@pytest.mark.asyncio
async def test_replay_engine_returns_progressive_tier_differentiation():
    engine_l1 = ReplayRAGEngine(engine_id="level1_naive")
    engine_l4 = ReplayRAGEngine(engine_id="level4_agentic")

    q_id = "q_amendment_rate_comparison"
    r1 = await engine_l1.run_query(query="Compare Cloud Security Architect rates...", question_id=q_id)
    r4 = await engine_l4.run_query(query="Compare Cloud Security Architect rates...", question_id=q_id)

    assert r4.eval_metrics.groundedness > r1.eval_metrics.groundedness
    assert r4.eval_metrics.qa_relevance > r1.eval_metrics.qa_relevance
    assert len(r4.agent_steps) >= 2
```

- [x] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=backend pytest backend/tests/test_engines.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.engines'`)

- [x] **Step 3: Implement `base.py`, `level1_naive.py`, `level2_hybrid.py`, `level3_multimodal.py`, `level4_agentic.py` (with Google ADK agent & tools), `managed_vertex.py`, and `replay_engine.py`**

Implement all 5 engines plus the deterministic `ReplayRAGEngine`. In `level4_agentic.py`, wire the 4 plain-function tools (`search_procurement_corpus`, `check_amendments`, `calculate_contract_pricing`, `verify_citations`) into a Google ADK `Agent` (when `google-adk` is installed) and expose direct deterministic tool execution with AST-validated arithmetic (no `eval()`).

- [x] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=backend pytest backend/tests/test_engines.py -v`
Expected: PASS (`3 passed`)

---

### Task 4: Async Evaluation Service & FastAPI SSE Streaming API (`Phase 4`)

**Files:**
- Create: `backend/app/evaluation/__init__.py`
- Create: `backend/app/evaluation/evaluator.py`
- Create: `backend/app/api/__init__.py`
- Create: `backend/app/api/routes.py`
- Create: `backend/app/main.py`
- Test: `backend/tests/test_api_and_sse.py`

- [x] **Step 1: Write the failing test for SSE battle streaming, async evaluation events, benchmarks endpoint, and guarded PDF upload**

Create `backend/tests/test_api_and_sse.py`:
```python
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health_and_benchmarks_endpoints():
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    bench = client.get("/api/v1/benchmarks")
    assert bench.status_code == 200
    questions = bench.json()["questions"]
    assert len(questions) >= 8


def test_sse_battle_stream_emits_retrieval_agent_answer_and_eval_events():
    payload = {
        "query": "What is the amended hourly rate for a Cloud Security Architect in Solicitation 01 vs Solicitation 03?",
        "question_id": "q_amendment_rate_comparison",
        "engines": ["level1_naive", "level4_agentic", "managed_vertex"],
    }
    with client.stream("POST", "/api/v1/battle/stream", json=payload) as response:
        assert response.status_code == 200
        events = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

    event_types = {e["event"] for e in events}
    assert "retrieval_complete" in event_types
    assert "agent_step" in event_types
    assert "answer_complete" in event_types
    assert "eval_complete" in event_types


def test_leaderboard_endpoint_returns_5_engine_aggregates():
    resp = client.get("/api/v1/leaderboard")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["engines"]) == 5
```

- [x] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=backend pytest backend/tests/test_api_and_sse.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.main'`)

- [x] **Step 3: Implement `evaluator.py`, `routes.py`, and `main.py`**

Implement the FastAPI endpoints including SSE battle streaming (`POST /api/v1/battle/stream`), benchmark catalog (`GET /api/v1/benchmarks`), leaderboard aggregation (`GET /api/v1/leaderboard`), and guarded PDF upload (`POST /api/v1/ingest/upload`).

- [x] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=backend pytest backend/tests/test_api_and_sse.py -v`
Expected: PASS (`3 passed`)

---

### Task 5: React + TypeScript Arena UI, Bounding-Box PDF Viewer, & Public Packaging (`Phase 5`)

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/index.html`
- Create: `frontend/src/types/arena.ts`
- Create: `frontend/src/components/QueryBar.tsx`
- Create: `frontend/src/components/ArenaBattle.tsx`
- Create: `frontend/src/components/DocumentViewer.tsx`
- Create: `frontend/src/components/MetricRadar.tsx`
- Create: `frontend/src/components/DocumentUpload.tsx`
- Create: `frontend/src/pages/ArenaDashboard.tsx`
- Create: `frontend/src/pages/Leaderboard.tsx`
- Create: `frontend/src/App.tsx`
- Create: `frontend/src/main.tsx`
- Create: `scripts/bootstrap_gcp.sh`
- Create: `infra/main.tf`
- Create: `docker-compose.yml`
- Create: `backend/Dockerfile`
- Test: `backend/tests/test_publish_manifest.py`

- [x] **Step 1: Write the failing test verifying public packaging, `publish.json` scrub rules, and frontend/infra completeness**

Create `backend/tests/test_publish_manifest.py`:
```python
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_publish_manifest_and_public_readme_have_zero_internal_leaks():
    manifest_path = PROJECT_ROOT / "publish.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["strategy"] == "mirror_and_scrub"
    assert "customer-work/public/2026" in manifest["target_path"]

    readme_text = (PROJECT_ROOT / "README.md").read_text()
    for forbidden in manifest["forbidden_patterns"]:
        assert forbidden not in readme_text, f"Forbidden internal string '{forbidden}' found in README.md"


def test_frontend_and_deployment_artifacts_exist():
    required_files = [
        PROJECT_ROOT / "frontend" / "package.json",
        PROJECT_ROOT / "frontend" / "src" / "App.tsx",
        PROJECT_ROOT / "frontend" / "src" / "components" / "ArenaBattle.tsx",
        PROJECT_ROOT / "frontend" / "src" / "components" / "DocumentViewer.tsx",
        PROJECT_ROOT / "frontend" / "src" / "components" / "MetricRadar.tsx",
        PROJECT_ROOT / "frontend" / "src" / "pages" / "Leaderboard.tsx",
        PROJECT_ROOT / "scripts" / "bootstrap_gcp.sh",
        PROJECT_ROOT / "infra" / "main.tf",
        PROJECT_ROOT / "docker-compose.yml",
        PROJECT_ROOT / "backend" / "Dockerfile",
    ]
    for f in required_files:
        assert f.exists(), f"Missing required artifact: {f}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=backend pytest backend/tests/test_publish_manifest.py -v`
Expected: FAIL (`AssertionError: Missing required artifact`)

- [ ] **Step 3: Implement the React + TypeScript frontend components, `bootstrap_gcp.sh`, `infra/main.tf`, `backend/Dockerfile`, and `docker-compose.yml`**

Implement the complete frontend workspace and GCP deployment automation.

- [ ] **Step 4: Run full test suite to verify everything passes**

Run: `PYTHONPATH=backend pytest backend/tests/ -v`
Expected: PASS (`All tests passed`)
