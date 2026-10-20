# The RAG Arena: Progressive Enterprise RAG Benchmark on Google Cloud (v3.0 Spec)

> **v3.0 reset (2026-10-20).** v2.0 shipped 7 engines and a 4-step tabbed UI, but a line-by-line audit
> (`docs/audits/2026-10-20-v2-authenticity-audit.md`) found that stage progress was timer-driven,
> several "live" paths silently fell back to local token matching, Level 4's agent trace was
> Python-scripted, and every engine carried hardcoded evaluation constants. v3.0 rebuilds the product
> around one rule — **nothing is shown that was not measured** — and ships **one level at a time**.
> A level is not started until the previous level has passed its Definition of Done (§9).

---

## 1. Executive Summary & Target Audience

**The RAG Arena** is a Google Cloud reference application that walks **enterprise architects, CIO/CTO
offices, data & AI leaders, and engineers** through the production RAG maturity curve on one cohesive
domain — **Enterprise IT & Cloud Procurement** (RFPs, SOWs, CLIN rate cards, SLA matrices, architecture
exhibits, superseding amendments).

The user picks a level, asks a question, and **watches the real pipeline execute stage by stage**:
every node in the pipeline lights up when the backend actually enters that stage, shows the exact SQL
or API request that was sent, the rows/scores/tokens that came back, and the measured duration. The
same stage telemetry drives the comparison page and the leaderboard.

---

## 2. Non-Negotiable Principles (v3.0)

| # | Principle | Enforcement |
| :-- | :-- | :-- |
| P1 | **Measured, not simulated.** Every SSE stage event originates inside the engine at the moment the stage starts/ends, carrying measured `duration_ms` and the real request/response summary. | `StageEmitter` contract (§6.2); `routes.py` is forbidden from synthesizing `pipeline_stage` events or timers. Unit test asserts no `asyncio.sleep`/`wait(timeout=…)` stage pacing in `routes.py`. |
| P2 | **Fail loud in live mode.** No `except Exception: pass`, no silent fallback to local files, no impersonation of a managed service. A failed stage emits `stage_failed` with the exception class and message, then `engine_error`. | Lint rule + test `test_no_silent_fallbacks.py` (AST scan of `backend/app/engines/**` for bare `except` + `pass`/`return None`). |
| P3 | **No hardcoded scores.** Engines never set `eval_metrics`. Live metrics come from the LLM judge; replay metrics come from **recordings of real live runs**, never hand-typed. | `EngineResult.eval_metrics` removed; `data/recordings/**` carry a provenance header (`recorded_at`, `project_id`, `model_ids`, `git_sha`). |
| P4 | **One level at a time.** `ENABLED_LEVELS` gates the registry, the UI navigation, and the Compare page. A level is enabled only after its Definition of Done (§9) is signed off by the owner. | `backend/app/core/config.py: enabled_levels`; locked levels render a "Locked — pending Level n sign-off" card. |
| P5 | **Honest labels.** A stage is labelled with the service that actually ran (e.g. "Discovery Engine Ranking API" only if `rank()` returned; otherwise the stage is `skipped` with the reason). | `stage_completed.payload.service_used`, `reranker_used`, `adk_runner_used` flags surfaced in UI. |
| P6 | **Offline replay stays zero-credential.** `DEMO_MODE=replay` streams recorded stage events with their original relative timing (scaled by `REPLAY_SPEED`). | `DEMO_MODE=record` writes recordings during live runs; CI runs replay only. |

---

## 3. Architecture Levels & Delivery Order

Levels are delivered strictly in this order. Vertex AI Vector Search 2.0 (`vertex_vector_v2`) and AlloyDB
ScaNN (`alloydb_scann`) are **parked** (code retained under `backend/app/engines/parked/`, excluded from
the registry) until Levels 1–5 are signed off.

| Order | Engine ID | Name | Retrieval | Synthesis | What it proves |
| :-- | :-- | :-- | :-- | :-- | :-- |
| 1 | `level1_naive` | Naive Vector RAG | BigQuery `VECTOR_SEARCH` (COSINE, top-5) over 360-char sliding windows, `gemini-embedding-2` | `gemini-4-argon` | Misses exact CLIN IDs, scrambles tables, no amendment awareness |
| 2 | `level2_hybrid` | BigQuery Hybrid + Reranker | `VECTOR_SEARCH` + `SEARCH()` inverted index → RRF (k=60) → Discovery Engine Ranking API (`semantic-ranker-default@latest`) top-50→5 | `gemini-4-argon` | Exact identifier recall; reranker precision |
| 3 | `level3_multimodal` | Layout-Aware Multimodal RAG | **Document AI Layout Parser** (tables → Markdown, blocks → bbox) + **Gemini vision transcription of diagram pages** at ingestion; `gemini-embedding-2` multimodal column; metadata pre-filter; amendment authority headers | `gemini-4-argon` | Tables and diagrams answered correctly |
| 4 | `level4_agentic` | Agentic RAG (Google ADK) | **Real ADK `Runner`** (`gemini-4-argon`) choosing among 4 tools: `search_procurement_corpus` (Level 3 retrieval), `check_amendments` (BigQuery `amends_section` lookup), `calculate_contract_pricing` (AST-safe math), `verify_citations` (claim ↔ chunk check) | ADK final response | Multi-hop supersession + deterministic math + self-verification |
| 5 | `managed_vertex` | Managed Vertex AI Search | Discovery Engine data store built from the GCS corpus; `SearchServiceClient.search` with extractive segments + summary | Vertex AI Search summary | Build vs. Buy baseline |

### Per-level pipeline stages (canonical stage IDs)

| Stage ID | L1 | L2 | L3 | L4 | L5 | Payload (all measured) |
| :-- | :-: | :-: | :-: | :-: | :-: | :-- |
| `query_prep` | ✓ | ✓ (acronym expansion) | ✓ | ✓ | ✓ | `original_query`, `prepared_query`, `expansions[]` |
| `embed_query` | ✓ | ✓ | ✓ | (inside tool) | — | `model`, `dimensions`, `token_count`, `duration_ms` |
| `retrieve` | ✓ | ✓ | ✓ | (inside tool) | ✓ | `sql` or `api_request` (parameterized), `rows_returned`, `top_k`, `scores[]`, `bytes_processed` (BQ), `duration_ms` |
| `rerank` | — | ✓ | ✓ | (inside tool) | — | `service_used`, `candidates_in`, `candidates_out`, `scores_before[]`, `scores_after[]`, `duration_ms` |
| `agent_turn` | — | — | — | ✓ (repeated) | — | `turn_index`, `tool_name`, `tool_args`, `tool_result_summary`, `llm_tokens`, `duration_ms` |
| `synthesize` | ✓ | ✓ | ✓ | ✓ (final) | ✓ | `model`, `thinking_level`, `prompt_tokens`, `output_tokens`, `duration_ms`, `answer` |
| `evaluate` | ✓ | ✓ | ✓ | ✓ | ✓ | `judge_model`, `groundedness`, `qa_relevance`, `context_precision`, `reference_answer_used`, `duration_ms` |

Ingestion is a separate, visible job per level (§6.3) with its own stages:
`discover_docs → parse → chunk → embed → load_bigquery → build_indexes → verify_counts`.

---

## 4. The Enterprise Procurement Corpus (unchanged from v2.0)

3 solicitations in `data/sample_docs/procurement_corpus/` (18 generated interconnected PDFs + CUAD MSAs)
built by `scripts/build_procurement_corpus.py`; mirrored to `gs://<GCS_CORPUS_BUCKET>/sample_docs/`.
12 golden questions across 4 failure modes in `data/golden_benchmarks/procurement_benchmark.json`.

---

## 5. Storage Schema (BigQuery, `rag_arena` dataset)

```sql
-- One row per chunk per tier. CLUSTER BY keeps per-level VECTOR_SEARCH pre-filters cheap.
CREATE TABLE IF NOT EXISTS `rag_arena.document_chunks` (
  chunk_id STRING NOT NULL,
  tier_profile STRING NOT NULL,          -- 'naive' | 'hybrid' | 'multimodal'
  ingest_run_id STRING NOT NULL,         -- FK -> ingest_runs.run_id (provenance)
  solicitation_id STRING NOT NULL,
  doc_name STRING NOT NULL,
  doc_type STRING NOT NULL,
  element_type STRING NOT NULL,          -- 'narrative_text' | 'table' | 'diagram'
  section_id STRING,
  amendment_number INT64 DEFAULT 0,
  effective_date DATE,
  amends_section STRING,
  page_number INT64 NOT NULL,
  bbox STRUCT<page INT64, x0 FLOAT64, y0 FLOAT64, x1 FLOAT64, y1 FLOAT64>,
  content STRING NOT NULL,
  layout_source STRING,                  -- 'pymupdf' | 'documentai_layout' | 'gemini_vision'
  embedding ARRAY<FLOAT64>,              -- gemini-embedding-2 text mode (L1/L2)
  embedding_multimodal ARRAY<FLOAT64>    -- gemini-embedding-2 multimodal (L3/L4)
)
CLUSTER BY tier_profile, solicitation_id;

-- Ingestion provenance: one row per visible ingestion job.
CREATE TABLE IF NOT EXISTS `rag_arena.ingest_runs` (
  run_id STRING NOT NULL, level STRING NOT NULL, tier_profile STRING NOT NULL,
  started_at TIMESTAMP, finished_at TIMESTAMP, status STRING,
  docs INT64, pages INT64, chunks INT64, embedded INT64, loaded INT64,
  embedding_model STRING, layout_source STRING, git_sha STRING, stage_log JSON
);

-- Query provenance: one row per engine run (feeds Compare + Leaderboard).
CREATE TABLE IF NOT EXISTS `rag_arena.query_runs` (
  run_id STRING NOT NULL, engine STRING NOT NULL, question_id STRING, query STRING,
  started_at TIMESTAMP, latency_ms INT64, cost_usd FLOAT64,
  groundedness FLOAT64, qa_relevance FLOAT64, context_precision FLOAT64,
  stage_log JSON, answer STRING, citations JSON, demo_mode STRING, git_sha STRING
);
```

Indexes: `CREATE VECTOR INDEX … ON document_chunks(embedding)` and `(embedding_multimodal)` (IVF, COSINE);
`CREATE SEARCH INDEX … ON document_chunks(content)`. Index build state is reported by the ingestion job
(`build_indexes` stage polls `INFORMATION_SCHEMA.VECTOR_INDEXES` / `SEARCH_INDEXES` with a bounded wait).

---

## 6. Backend Contracts

### 6.1 Engine contract

```python
class BaseRAGEngine(ABC):
    engine_id: str
    engine_label: str
    stages: tuple[str, ...]                       # canonical stage IDs in execution order (§3)

    async def run_query(
        self, query: str, *, question_id: str | None, solicitation_filter: str | None,
        emitter: StageEmitter,                    # REQUIRED; engines emit, routes forward
    ) -> EngineResult: ...
```

`EngineResult` (frozen dataclass, pure Python) carries `answer`, `citations`, `retrieved_chunks`,
`stage_log: tuple[StageRecord, ...]` (one per stage with `started_at`, `duration_ms`, `payload`),
`usage: TokenUsage` (from `response.usage_metadata`), `cost_usd` (computed from `usage` × published
per-model prices in `pricing.py`), `flags: EngineFlags` (`reranker_used`, `adk_runner_used`,
`layout_source`). **No `eval_metrics` field.**

### 6.2 `StageEmitter` contract

```python
class StageEmitter(Protocol):
    async def started(self, stage: str, *, label: str, service: str, request: dict) -> StageHandle
    async def completed(self, handle: StageHandle, *, payload: dict) -> None   # duration measured here
    async def failed(self, handle: StageHandle, *, error: BaseException) -> None
    async def skipped(self, stage: str, *, reason: str) -> None
```

Blocking SDK calls run in `asyncio.to_thread`; the emitter is called from the coroutine before and after.
`routes.py` only forwards emitter events to the SSE queue and appends `answer_complete`, `eval_*`,
`engine_error`, `battle_complete`. It owns **no stage logic and no timers**.

### 6.3 SSE protocol (v3)

`POST /api/v1/battle/stream` — frame `data: {json}\n\n`, bounded at `MAX_SSE_FRAMES=600`.

| event | fields |
| :-- | :-- |
| `run_started` | `run_id`, `engines[]`, `demo_mode`, `question_id`, `ts` |
| `stage_started` | `run_id`, `engine`, `stage`, `label`, `service`, `request` (redacted SQL/API summary), `ts` |
| `stage_completed` | `run_id`, `engine`, `stage`, `duration_ms`, `payload` (§3 table), `ts` |
| `stage_failed` | `run_id`, `engine`, `stage`, `duration_ms`, `error_type`, `error`, `ts` |
| `stage_skipped` | `run_id`, `engine`, `stage`, `reason`, `ts` |
| `agent_turn` | `run_id`, `engine`, `turn_index`, `tool_name`, `tool_args`, `tool_result_summary`, `duration_ms`, `ts` |
| `answer_complete` | `run_id`, `engine`, `answer`, `citations[]`, `latency_ms`, `cost_usd`, `usage`, `flags`, `ts` |
| `eval_complete` | `run_id`, `engine`, `metrics`, `judge_model`, `reference_answer_used`, `duration_ms`, `ts` |
| `engine_error` | `run_id`, `engine`, `error_type`, `error`, `failed_stage`, `ts` |
| `stream_warning` | `run_id`, `message`, `ts` |
| `battle_complete` | `run_id`, `engines_completed[]`, `engines_failed[]`, `ts` |

Ingestion jobs: `POST /api/v1/ingest/jobs {level}` → `{job_id}`; `GET /api/v1/ingest/jobs/{job_id}/stream`
emits `job_started`, `stage_started|completed|failed` (stages §3), `doc_progress` (`doc_name`, `pages`,
`chunks`), `embed_progress` (`done`, `total`, `batch_ms`), `job_complete` (`counts_by_tier` read back from
BigQuery). `GET /api/v1/ingest/status` returns live `COUNT(*) GROUP BY tier_profile` and index states.

### 6.4 Modes

| `DEMO_MODE` | Behaviour |
| :-- | :-- |
| `live` | Real GCP calls; fail loud; writes `query_runs`/`ingest_runs`. |
| `record` | `live` **plus** writes `data/recordings/<engine>/<question_id>.json` (full event stream with relative timestamps + provenance header). |
| `replay` | No credentials. Streams recordings with original timing × `REPLAY_SPEED` (default 1.0). Ad-hoc questions not in recordings return `engine_error("No recording for this question in replay mode")` — never an improvised answer. |

### 6.5 Evaluation

`evaluate_engine_result()` is the only source of metrics: structured-output judge (`gemini-4-argon`,
`thinking_level=LOW`, `temperature=0`) returning Groundedness, QA Relevance (vs. golden
`reference_answer` when `question_id` is known), Context Precision. The Compare page's Battle Score
(`0–100`) = 40% QA Relevance + 35% Groundedness + 15% Context Precision + 10% Efficiency, computed from
`query_runs` rows. Diagnostics text is generated from `flags`, `stage_log`, and metric deltas — no
per-engine static strings.

---

## 7. Frontend (v3 — full rebuild)

### 7.1 Shell

- **Full-width workspace**: no `max-w-*` on `main`; content uses `w-full px-6 2xl:px-10` and CSS grid
  panes with `minmax()` columns. Side panes collapse below `xl`.
- **Routing** (`react-router-dom`): `/` (Overview + level ladder), `/ingest` (ingestion jobs),
  `/levels/:levelId` (one page per level), `/compare`, `/leaderboard`.
- **Theme**: `ThemeProvider` with `light | dark | system`, persisted in `localStorage("rag-arena.theme")`,
  `system` follows `matchMedia("(prefers-color-scheme: dark)")`; Tailwind `darkMode: "class"`.
- **Design tokens** (CSS variables in `src/styles/tokens.css`, mapped in `tailwind.config.js`), earthy
  and low-chroma:

| Token | Light | Dark | Use |
| :-- | :-- | :-- | :-- |
| `--surface-0/1/2` | `#F6F2EA` / `#EFE9DD` / `#E6DECF` (linen, sand) | `#1B1A17` / `#23211D` / `#2B2823` (charcoal bark) | page, panels, cards |
| `--ink-1/2/3` | `#2B2520` / `#5A5148` / `#8A8075` | `#EDE7DC` / `#B8AFA2` / `#8A8075` | text hierarchy |
| `--line` | `#D9CFBF` | `#3A362F` | borders |
| `--accent-moss` | `#5F7A5A` | `#8FA98A` | primary / selected / running |
| `--accent-clay` | `#B5653F` | `#D08A66` | warnings, amendments |
| `--accent-ochre` | `#C29A3B` | `#D9B56A` | highlights, winner |
| `--accent-slate` | `#5F7C8A` | `#8FA9B5` | info, citations |
| `--status-ok/err` | `#4F7D5C` / `#A24A3C` | `#7FAE8C` / `#D07A6A` | status |

  Components use only semantic classes (`bg-surface-1`, `text-ink-2`, `border-line`, `text-moss`).
  Motion respects `prefers-reduced-motion`; no infinite decorative animations.

### 7.2 Level page (`/levels/:levelId`) — the "simulation with real data"

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Level 2 · BigQuery Hybrid + Reranker        [Ingest status ●]  [Theme ◐]     │
├────────────────────┬─────────────────────────────────────┬───────────────────┤
│ Question picker    │ STAGE RAIL (horizontal)             │ Document viewer   │
│ (golden + custom)  │ ○ prep ─● embed ─◐ retrieve ─○ rerank─○ synth ─○ eval  │ bbox highlight    │
│ [Run level ▶]      │ each node: live timer while running,│ of selected       │
│                    │ measured ms + service once done     │ citation          │
├────────────────────┴─────────────────────────────────────┤                   │
│ STAGE DETAIL (selected node): request sent (SQL/API),    │                   │
│ response summary (rows, scores, tokens), raw payload     │                   │
├──────────────────────────────────────────────────────────┴───────────────────┤
│ ANSWER + CITATIONS                         │ METRICS (judge) · cost · latency│
└──────────────────────────────────────────────────────────────────────────────┘
```

- `useBattleStream(engineIds)` keeps an **append-only event log** plus a derived per-engine timeline
  (`stages[stage] = {status, startedAt, durationMs, request, payload}`); nothing is inferred from timers.
- A running node shows an elapsed counter driven by `stage_started.ts` → now; it stops on
  `stage_completed` and displays the backend-measured `duration_ms` (never the client estimate).
- Clicking a node opens Stage Detail with the exact `request`/`payload` from the event.
- Level 4 renders `agent_turn` events as an expanding tool-call timeline inside the `agent_turn` node.
- Ingestion status pill reads `GET /api/v1/ingest/status`; if the level's tier has 0 rows, the Run
  button is disabled with a link to `/ingest`.

### 7.2.1 Live Architecture Diagram (transparency view)

Every level page and every ingestion job page renders the **architecture diagram of that level** next to
the stage rail. The diagram is a data-driven SVG of GCP services and data flows; the node(s) that the
currently running stage touches are highlighted live, and the related details for that node are shown
alongside.

- **Source of truth**: `GET /api/v1/levels` returns, per level, an `ArchitectureDescriptor`:

  ```json
  {
    "level_id": "level2_hybrid",
    "nodes": [
      {"id": "user", "label": "User question", "service": "Browser", "kind": "actor", "col": 0, "row": 1, "stage_ids": ["query_prep"]},
      {"id": "embed", "label": "Query embedding", "service": "Vertex AI · gemini-embedding-2", "kind": "model", "col": 1, "row": 0, "stage_ids": ["embed_query"]},
      {"id": "bq", "label": "document_chunks", "service": "BigQuery VECTOR_SEARCH + SEARCH()", "kind": "store", "col": 2, "row": 1, "stage_ids": ["retrieve"]},
      {"id": "rank", "label": "Cross-encoder rerank", "service": "Discovery Engine Ranking API", "kind": "api", "col": 3, "row": 1, "stage_ids": ["rerank"]},
      {"id": "llm", "label": "Answer synthesis", "service": "Vertex AI · gemini-4-argon", "kind": "model", "col": 4, "row": 1, "stage_ids": ["synthesize"]},
      {"id": "judge", "label": "LLM-as-a-judge", "service": "Vertex AI · gemini-4-argon", "kind": "model", "col": 5, "row": 1, "stage_ids": ["evaluate"]}
    ],
    "edges": [{"from": "user", "to": "embed", "label": "text"}, {"from": "embed", "to": "bq", "label": "3072-d vector"}, ...],
    "ingest_nodes": [...], "ingest_edges": [...]
  }
  ```

  Descriptors are **design metadata** (which services exist and how they connect) and live in
  `backend/app/core/architectures.py`; they never carry timings, scores, or results. All dynamic state on
  the diagram comes from SSE events.
- **Highlight rules**: a node is `idle` until any of its `stage_ids` starts, `active` (pulsing outline,
  elapsed counter) while a mapped stage is running, `done` (filled, shows `duration_ms` and a one-line
  result summary such as `rows=50 · 12.4 MB scanned` or `tokens=1,842`) after `stage_completed`, `failed`
  (clay outline + error type) after `stage_failed`, `skipped` (dashed, reason) after `stage_skipped`.
  Edges animate a flow marker only while the source node is `done` and the target is `active`.
- **Details pane** follows the highlighted node automatically while a run is in progress (user can pin a
  node to stop following). It shows: service and region, the exact request sent (parameterized SQL / API
  body), the response summary, the code reference (`module:function`), measured duration, and — for
  Level 4 — the current tool turn. Clicking any node at any time opens the same pane for that node.
- **Ingestion jobs** reuse the component with `ingest_nodes/ingest_edges`
  (`GCS corpus → parser (PyMuPDF / Document AI) → chunker → gemini-embedding-2 → BigQuery load → vector / search
  index`), highlighted by the job's stage events.
- Replay mode drives the same diagram from recordings; there is no separate "demo" animation path.

### 7.3 Ingest page (`/ingest`)

One card per level: last run provenance (from `ingest_runs`), live counts by tier, "Run ingestion"
button that opens a job stream with the same stage-rail component (`discover_docs → … → verify_counts`),
per-document progress rows, and a post-run artifacts panel (sample chunks, embedding dims, index states).

### 7.4 Compare & Leaderboard

`/compare` runs the selected **enabled** levels concurrently on one question and shows the stage rails
stacked, then the Battle Score table. `/leaderboard` aggregates `query_runs` (live) or recordings
(replay); every row displays its provenance (`recorded_at`, models, git sha). Parked engines never appear.

### 7.5 Salvage list

Keep: `types/arena.ts` (extended), `DocumentViewer.tsx` (re-tokenized), SSE reader logic
(`consumeSseResponse`), `QueryBar` validation helpers. Everything else is rewritten under `src/app/`,
`src/features/{levels,ingest,compare,leaderboard}/`, `src/components/ui/`, `src/theme/`.

---

## 8. Repository Structure (v3 target)

```text
rag-arena/
├── backend/app/
│   ├── api/routes.py                  # SSE forwarder + REST; no stage logic
│   ├── core/{config,models,gcp_clients,pricing,telemetry}.py   # telemetry.py = StageEmitter, StageRecord
│   ├── engines/{base,level1_naive,level2_hybrid,level3_multimodal,level4_agentic,managed_vertex}.py
│   ├── engines/parked/{vertex_vector_v2,alloydb_scann}.py      # excluded from registry
│   ├── engines/replay_engine.py       # recording player
│   ├── ingestion/{parsers,layout_documentai,vision_transcribe,pipeline,bq_indexer,jobs}.py
│   ├── evaluation/{evaluator,scoring}.py
│   └── recording/{recorder,store}.py
├── backend/tests/                     # offline; replay fixtures only
├── frontend/src/{app,features,components/ui,theme,styles,lib,types}/
├── data/{sample_docs,golden_benchmarks,recordings}/
├── docs/audits/2026-10-20-v2-authenticity-audit.md
├── scripts/{build_procurement_corpus.py,bootstrap_gcp.sh,record_runs.py}
├── infra/main.tf
├── spec.md · task.md · README.md
```

---

## 9. Definition of Done — per level (gate to start the next level)

| # | Gate | Evidence required (pasted into `task.md`) |
| :-- | :-- | :-- |
| D1 | Ingestion job for the level runs from the UI on the configured GCP project (`GCP_PROJECT_ID` in `.env`); `ingest_runs` row written; `GET /api/v1/ingest/status` shows non-zero counts for the tier. | `job_id`, counts by tier from BigQuery. |
| D2 | Engine emits every canonical stage itself; `routes.py` contains no stage synthesis or timers. | `curl` transcript of `stage_started/completed` with `duration_ms`; grep proof on `routes.py`. |
| D3 | Live run on ≥ 3 golden questions produces an answer with ≥ 1 citation and judge metrics; failures surface as `stage_failed` + `engine_error`. | 3 run_ids + `query_runs` rows. |
| D4 | UI level page shows the stage rail animating from real events, the level's architecture diagram highlighting the node of the running stage with its details pane following, stage detail with exact request/payload, citations highlighted in the viewer, theme switch works in light/dark/system. | Screenshot set (light + dark). |
| D5 | `DEMO_MODE=record` captured recordings for the 12 golden questions; `DEMO_MODE=replay` plays them offline. | `ls data/recordings/<engine>/` (12 files) + replay curl. |
| D6 | Tests green: `pytest backend/tests -q` and `npm run build`; new tests cover the level's stage emission and fail-loud paths. | Quoted test counts. |
| D7 | Owner live demo sign-off. | Date + initials in `task.md`. |

---

## 10. Constraints

- GCP project, bucket, gcloud profile (`CLOUDSDK_CONFIG`) and ADC path come only from the git-ignored `.env`
  / environment (`.env.example` is the template); the repository, docs, tests, and recordings never contain
  real project ids, bucket names, account emails, or profile paths. Identity scrub tokens for the public
  mirror live in `PUBLISH_SCRUB_TOKENS` (env), not in `publish.json`.
- **No `git commit` / `git push` without explicit owner instruction.**
- Pure-Python frozen dataclasses (no `pydantic-core` binary); Starlette + Uvicorn; React + Vite + Tailwind.
- Secrets/project IDs only in git-ignored `.env` (`.env.example` is the template); the frontend never
  hardcodes project or bucket names (it reads them from `GET /api/v1/health`).
- Every loop, retry, poll, and request has an explicit bound and timeout.
