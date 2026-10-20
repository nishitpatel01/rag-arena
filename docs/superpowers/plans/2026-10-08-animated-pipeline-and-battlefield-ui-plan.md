# Animated RAG Pipeline Visualizer & Game-Style Architecture Battlefield Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an interactive, animated Document Chunking & End-to-End GCP RAG Pipeline Visualizer that updates each architecture's live card as documents flow through the 5 tiers and culminates in a Game-Style Architecture Battlefield comparing all 5 GCP architectures head-to-head.

**Architecture:** We extend the backend with (1) `backend/app/evaluation/battlefield.py` to compute deterministic game-style standings (`battle_score` 0–100, `rank`, `badge`, `gcp_capabilities`, `verdict_reason`, `strengths`, `missed_capabilities`) and side-by-side chunking comparisons across `naive`, `hybrid`, and `multimodal` tiers, and (2) `GET /api/v1/pipeline/chunk-visualizer` plus enriched `pipeline_stage` and `battlefield_summary` SSE frames in `POST /api/v1/battle/stream`. On the frontend, we add `ChunkingAnimator.tsx` (visualizing how raw PDFs are sliced, contextualized, or parsed into atomic tables/diagrams across tiers), `PipelineFlowVisualizer.tsx` (animating each architecture's GCP service nodes as SSE stages stream in), and `BattlefieldArena.tsx` (a game-style post-battle comparison arena with champion podium, XP/score bars, GCP capability loadouts, and per-architecture win/loss diagnostics).

**Tech Stack:** Python 3.11+, Starlette/FastAPI ASGI (`sse-starlette`), React 18, TypeScript, Vite, SVG/CSS Keyframe Animations, Pytest.

---

## File Structure & Responsibilities

1. **Create `backend/app/evaluation/battlefield.py`**:
   - Pure, bounded functions that:
     - Build the side-by-side Document Chunking X-Ray payload (`build_chunk_visualizer_payload`) comparing how a solicitation PDF (or uploaded session PDF) is chunked under `naive` (360-char sliding windows, stripped pipes), `hybrid` (section-aware + Contextual Lineage headers), and `multimodal` (Document AI atomic Markdown tables + Gemini Vision diagram topology + Authority/Supersession headers + normalized bounding boxes).
     - Build the GCP Architecture Blueprint metadata (`get_gcp_architecture_blueprints`) listing the exact Google Cloud services and pipeline stages for all 5 engines.
     - Compute the Game-Style Battlefield Summary (`build_battlefield_summary`) ranking all evaluated `EngineResult` instances (`1..N`) by composite `battle_score` (`0.0–100.0`), assigning game badges (`"Arena Champion"`, `"Multimodal Specialist"`, `"Precision Hybrid"`, `"Turnkey Challenger"`, `"Baseline Contender"`), and generating an evidence-backed `verdict_reason` explaining why each engine won or lost.
2. **Modify `backend/app/api/routes.py`**:
   - Add `GET /api/v1/pipeline/chunk-visualizer` (accepting optional `?solicitation_id=solicitation_01&doc_name=04_Pricing_Schedule_CLINs.pdf`) returning `build_chunk_visualizer_payload(...)`.
   - Enhance `_run_single_engine_worker` and `_stream_battle_events` in `POST /api/v1/battle/stream` to:
     - Emit `{"event": "pipeline_stage", "engine": engine_id, "stage": ..., "gcp_service": ..., "progress_pct": ...}` events (`chunking` -> `embedding` -> `retrieving` -> `reranking_or_agent` -> `synthesizing` -> `evaluated`).
     - Emit `{"event": "battlefield_summary", "rankings": [...], "winner_engine": ..., "total_engines": ...}` right before `battle_complete`.
3. **Modify `frontend/src/types/arena.ts`**:
   - Add TypeScript interfaces for `PipelineStageId`, `PipelineStageEvent`, `ChunkVisualizerTierSummary`, `ChunkVisualizerPayload`, `BattlefieldStanding`, `BattlefieldSummaryEvent`, and extend `BattleEvent` and `EngineStreamState`.
4. **Create `frontend/src/components/ChunkingAnimator.tsx`**:
   - Interactive Document Chunking & Ingestion X-Ray component where users can select any solicitation document (pricing table, architecture diagram, or contract amendment) or their uploaded PDF and watch an animated side-by-side breakdown of how **Level 1 (Naive 360-char slicer)** shreds tables vs. **Level 2 (Contextual Lineage)** vs. **Level 3/4 (Document AI Atomic Tables + Gemini Vision Diagrams + Amendment Headers)**.
5. **Create `frontend/src/components/PipelineFlowVisualizer.tsx`**:
   - Animated 5-Lane End-to-End GCP RAG Pipeline conveyor showing each architecture's Google Cloud building blocks lighting up in real time (`PyMuPDF` / `Document AI` -> `gemini-embedding-2` (Text & Multimodal) -> `BigQuery VECTOR_SEARCH` / `SEARCH() RRF` -> `Vertex AI Ranking API` / `Google ADK 4-Tool Loop` -> `gemini-4-argon (high)`) with live progress bars and stage badges.
6. **Create `frontend/src/components/BattlefieldArena.tsx`**:
   - Game-Style Architecture Battlefield scoreboard and head-to-head comparison arena displayed as engines complete, featuring:
     - **Champion Podium & Battle Score (`0–100 XP`)**
     - **5 Contender Battle Cards** with HP/Accuracy bars (`Groundedness`, `QA Relevance`, `Context Precision`), Speed rating (`ms`), Cost efficiency (`$/1k`), **GCP Capability Loadout chips**, and **"Why It Won / Failed" Diagnostic Verdict**.
     - **Interactive Architecture Comparison Matrix** mapping GCP services to enterprise failure modes.
7. **Modify `frontend/src/pages/ArenaDashboard.tsx` & `frontend/src/components/ArenaBattle.tsx`**:
   - Integrate `ChunkingAnimator`, `PipelineFlowVisualizer`, progressive live updates on `ArenaBattle`, and the culminating `BattlefieldArena` into a cohesive 4-act user journey:
     - **Act 1: Document Chunking & Ingestion X-Ray (`ChunkingAnimator`)**
     - **Act 2: Live GCP RAG Pipeline Flow (`PipelineFlowVisualizer`)**
     - **Act 3: Real-Time Engine Results & Bounding-Box Evidence (`ArenaBattle` + `DocumentViewer`)**
     - **Act 4: The GCP Architecture Battlefield (`BattlefieldArena`)**

---

### Task 1: Backend Chunking Visualizer & Game Battlefield Scoring Engine (`backend/app/evaluation/battlefield.py` & `backend/app/api/routes.py`)

**Files:**
- Create: `backend/app/evaluation/battlefield.py`
- Modify: `backend/app/api/routes.py`
- Modify: `backend/tests/test_api_and_sse.py`

- [ ] **Step 1: Write failing tests in `backend/tests/test_api_and_sse.py` for `/api/v1/pipeline/chunk-visualizer`, `pipeline_stage` SSE events, and `battlefield_summary` SSE event**

```python
def test_chunk_visualizer_endpoint_returns_three_tier_comparison() -> None:
    """Verify GET /api/v1/pipeline/chunk-visualizer returns naive, hybrid, and multimodal chunk breakdowns and GCP blueprints."""
    client = TestClient(app)
    resp = client.get(
        "/api/v1/pipeline/chunk-visualizer?solicitation_id=solicitation_01&doc_name=04_Pricing_Schedule_CLINs.pdf"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["solicitation_id"] == "solicitation_01"
    assert data["doc_name"] == "04_Pricing_Schedule_CLINs.pdf"
    assert set(data["tiers"].keys()) == {"naive", "hybrid", "multimodal"}
    assert data["tiers"]["naive"]["chunk_count"] >= data["tiers"]["multimodal"]["chunk_count"]
    assert len(data["architecture_blueprints"]) == 5


def test_sse_battle_stream_emits_pipeline_stages_and_battlefield_summary() -> None:
    """Verify POST /api/v1/battle/stream emits pipeline_stage events per engine and a final battlefield_summary ranking."""
    client = TestClient(app)
    resp = client.post(
        "/api/v1/battle/stream",
        json={
            "query": "What is the amended hourly rate for Cloud Security Architect in Solicitation 01?",
            "question_id": "q_amendment_rate_comparison",
            "engines": ["level1_naive", "level3_multimodal", "level4_agentic"],
        },
    )
    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)
    stage_events = [e for e in events if e["event"] == "pipeline_stage"]
    assert len(stage_events) >= 6
    assert all("gcp_service" in e and "progress_pct" in e for e in stage_events)

    summary_events = [e for e in events if e["event"] == "battlefield_summary"]
    assert len(summary_events) == 1
    summary = summary_events[0]
    assert summary["winner_engine"] == "level4_agentic"
    assert len(summary["rankings"]) == 3
    assert summary["rankings"][0]["rank"] == 1
    assert summary["rankings"][0]["battle_score"] > summary["rankings"][-1]["battle_score"]
    assert "gcp_capabilities" in summary["rankings"][0]
    assert "verdict_reason" in summary["rankings"][0]
```

- [ ] **Step 2: Run `.venv/bin/pytest backend/tests/test_api_and_sse.py -v` to confirm the new tests fail**

- [ ] **Step 3: Implement `backend/app/evaluation/battlefield.py` and wire `GET /api/v1/pipeline/chunk-visualizer` + SSE events in `backend/app/api/routes.py`**

- [ ] **Step 4: Run `.venv/bin/pytest backend/tests/ -v` to confirm all backend tests pass**

---

### Task 2: Frontend Animated Chunking X-Ray, Live GCP Pipeline Flow & Game Battlefield Arena (`frontend/src/`)

**Files:**
- Modify: `frontend/src/types/arena.ts`
- Create: `frontend/src/components/ChunkingAnimator.tsx`
- Create: `frontend/src/components/PipelineFlowVisualizer.tsx`
- Create: `frontend/src/components/BattlefieldArena.tsx`
- Modify: `frontend/src/pages/ArenaDashboard.tsx`
- Modify: `frontend/src/components/ArenaBattle.tsx`
- Modify: `backend/tests/test_publish_manifest.py`

- [ ] **Step 1: Add structural verification tests in `backend/tests/test_publish_manifest.py` for `ChunkingAnimator.tsx`, `PipelineFlowVisualizer.tsx`, and `BattlefieldArena.tsx`**

- [ ] **Step 2: Run `.venv/bin/pytest backend/tests/test_publish_manifest.py -v` to confirm it fails**

- [ ] **Step 3: Implement `ChunkingAnimator.tsx`, `PipelineFlowVisualizer.tsx`, `BattlefieldArena.tsx`, and update `ArenaDashboard.tsx`, `ArenaBattle.tsx`, and `arena.ts`**

- [ ] **Step 4: Update the interactive preview artifact `rag_arena_ui_preview.html` so the user can immediately experience the animated Chunking X-Ray, Live Pipeline Conveyor, and Game-Style Architecture Battlefield in the side pane**

- [ ] **Step 5: Run `.venv/bin/pytest backend/tests/ -v` and `python3 scripts/publish_mirror_scrub.py`**
