# The RAG Arena — Progressive Enterprise RAG on Google Cloud (v3)

The RAG Arena is a Google Cloud reference application that climbs the production RAG maturity curve one level at a time on **Enterprise IT & Cloud Procurement** (RFPs, SOWs, CLIN rate cards, SLA matrices, architecture exhibits, superseding amendments). You pick a level, ask a question, and watch the real pipeline execute: every stage lights up when the backend enters it, shows the exact SQL or API request sent, what came back, and the measured duration, cost and LLM-judge scores.

**Status: Levels 1–5 live · Compare and Cloud Run in progress.** All five levels have run end-to-end on a live GCP project (ingestion, queries, recordings). Compare and Cloud Run deployment are planned and marked as such below.

---

## Why v3: measured, not simulated

v2 shipped seven engines, but an audit ([`docs/audits/2026-10-20-v2-authenticity-audit.md`](docs/audits/2026-10-20-v2-authenticity-audit.md))
found timer-driven stage events, silent fallbacks to local files, a Python-scripted "agent" trace and
hardcoded evaluation scores. v3 rebuilds the product around six rules:

| # | Principle | How it is enforced in the code |
| :-- | :-- | :-- |
| P1 | **Measured, not simulated** — every stage event is emitted inside the engine when the stage starts/ends, with a backend-measured `duration_ms`. | `core/telemetry.py` (`StageEmitter`); `routes.py` only forwards events — `test_routes_no_timers.py` guards it. |
| P2 | **Fail loud** — no silent fallback or impersonation; a failed stage emits `stage_failed`, then `engine_error`. | `test_no_silent_fallbacks.py` (AST scan of `engines/**`). |
| P3 | **No hardcoded scores** — live metrics come only from the LLM judge; replay metrics come from recordings of real live runs. | `EngineResult` has no `eval_metrics`; recordings carry a provenance header. |
| P4 | **One level at a time** — `ENABLED_LEVELS` gates the registry, navigation and Compare. | `core/config.py`; locked levels render a "Locked" card. |
| P5 | **Honest labels** — a stage is labelled with the service that actually ran. | `stage_started.service`, `EngineFlags` (`reranker_used`, `adk_runner_used`, `layout_source`). |
| P6 | **Zero-credential replay** — `DEMO_MODE=replay` streams recorded events with their original timing. | `engines/replay_engine.py`; ad-hoc questions in replay return `engine_error`, never an improvised answer. |

---

## The architecture ladder

| Level | Engine id | Retrieval & synthesis | What it proves | Status |
| :-- | :-- | :-- | :-- | :-- |
| 1 | `level1_naive` | BigQuery `VECTOR_SEARCH` (COSINE, top-5) over 360-char windows, `gemini-embedding-2`, selectable synthesis model | The baseline — and its failure modes: identifiers blurred, tables scrambled, no amendment awareness | ✅ live |
| 2 | `level2_hybrid` | Acronym expansion; BigQuery `VECTOR_SEARCH` ⨝ `SEARCH()` lexical over one-chunk-per-page lineage chunks → RRF (k=60) top-50 → Discovery Engine Ranking API (`semantic-ranker-default@latest`) → top-5 | Exact identifier recall; reranker precision | ✅ live |
| 3 | `level3_multimodal` | Document AI Layout Parser v1.5 (release candidate) elements + Gemini vision transcription of diagram pages → element-aware chunks with `gemini-embedding-2` multimodal vectors; hybrid RRF + Ranking API; authority headers (latest amendment first) | Tables and diagrams answered correctly | ✅ live |
| 4 | `level4_agentic` | Real Google ADK `LlmAgent` + `InMemoryRunner` (Gemini on Vertex AI, ≤8 turns) with `search_procurement_corpus` (Level 3 hybrid RRF + rerank), `check_amendments` (BigQuery supersession query), `calculate_contract_pricing` (AST calculator) and `verify_citations` (claim check + one repair turn) | Multi-hop supersession, deterministic math, self-verification | ✅ live |
| 5 | `managed_vertex` | Vertex AI Search (Enterprise + LLM add-on) over the same 45 PDFs imported from Cloud Storage: extractive segments with page numbers, answered either by the managed summary or by a selectable model over the segments | Build vs. buy baseline | ✅ live |

`ENABLED_LEVELS` defaults to all five levels (Level 5 needs the `VAIS_*` keys in live mode). Vertex AI Vector Search 2.0 and AlloyDB ScaNN are parked in `backend/app/engines/parked/`.

---

## Level 1 architecture

**Query path** (`backend/app/engines/level1_naive.py`, `backend/app/evaluation/`):

```mermaid
flowchart LR
    U["User question<br/>(browser)"] --> QP["query_prep<br/>whitespace normalisation (local)"]
    QP --> EQ["embed_query<br/>Vertex AI gemini-embedding-2<br/>RETRIEVAL_QUERY · 3072-d"]
    EQ --> RT["retrieve<br/>BigQuery VECTOR_SEARCH<br/>document_chunks_v3 · COSINE · top-5"]
    RT --> SY["synthesize<br/>selectable model<br/>Gemini (google-genai) or Claude (rawPredict)"]
    SY --> EV["evaluate<br/>LLM judge (selectable, must differ<br/>from the synthesis model)"]
    EV --> OUT["answer + citations + metrics<br/>latency · tokens · run cost"]
```

**Ingestion path** (`backend/app/ingestion/{jobs,live_backends}.py`): `discover_docs → parse` (PyMuPDF) `→ chunk` (360/60) `→ embed` (`gemini-embedding-2`) `→ load_bigquery` (staging + transactional tier replace) `→ build_indexes → verify_counts`.

The UI draws the same graph live from `GET /api/v1/levels` (design metadata only — nodes, edges, code
references); all state on it comes from SSE events.

![Level 1 run in progress: the stage rail, the live architecture diagram highlighting the active node, and the details pane following it](docs/screenshots/03-level1-running.png)

---

## Level 2 architecture

**Query path** (`backend/app/engines/level2_hybrid.py`, `backend/app/ingestion/bq_indexer.py`,
`backend/app/core/ranking.py`):

```mermaid
flowchart LR
    U["User question"] --> QP["query_prep<br/>acronym expansion (16-term map)"]
    QP --> EQ["embed_query<br/>gemini-embedding-2 · 3072-d"]
    QP --> LX["lexical terms<br/>stopwords dropped, ids kept whole"]
    EQ --> VS["VECTOR_SEARCH<br/>tier hybrid · COSINE · top-50"]
    LX --> SR["SEARCH() + weighted STRPOS<br/>tier hybrid · top-50"]
    VS --> RRF["RRF k=60<br/>FULL OUTER JOIN · top-50 candidates"]
    SR --> RRF
    RRF --> RK["rerank<br/>Discovery Engine Ranking API<br/>semantic-ranker-default@latest"]
    RK --> SY["synthesize (top-5)<br/>selectable model"]
    SY --> EV["evaluate<br/>LLM judge (selectable, must differ<br/>from the synthesis model)"]
```

Both legs and the fusion run as **one parameterized BigQuery statement**, scoring
`1/(60 + vector_rank) + 1/(60 + lexical_rank)` (SQL shape in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).

**Ingestion path** (`hybrid` tier, same job runner): one chunk per page prefixed with a Contextual Lineage header (solicitation · document · doc type · section), then embed → staged load → vector + search index → verify.

CUAD agreements are labelled `doc_type = COMMERCIAL_AGREEMENT`, so the lineage header tells the ranker and the
synthesis model what kind of document a page comes from.

![Level 2 architecture diagram after a completed run](docs/screenshots/18-level2-architecture-done.png)

---

## Level 3 architecture

**Query path** (`backend/app/engines/level3_multimodal.py`):

```mermaid
flowchart LR
    U["User question"] --> QP["query_prep<br/>acronyms + element intent<br/>(table / diagram / amendment)"]
    QP --> EQ["embed_query<br/>gemini-embedding-2 (multimodal space)"]
    EQ --> RT["retrieve<br/>VECTOR_SEARCH(embedding_multimodal) + SEARCH()<br/>tier multimodal → RRF k=60 top-50"]
    RT --> RK["rerank<br/>Discovery Engine Ranking API"]
    RK --> SY["synthesize (top-5)<br/>authority headers, latest amendment first<br/>[TABLE] / [DIAGRAM transcription] tags"]
    SY --> EV["evaluate<br/>LLM judge (≠ synthesis)"]
```

**Ingestion path** (`multimodal` tier; `backend/app/ingestion/{layout_documentai,vision_transcribe,multimodal,multimodal_job}.py`):

```mermaid
flowchart LR
    C["discover_docs<br/>45 PDFs"] --> D["parse · Document AI Layout Parser<br/>online, ≤15-page splits<br/>headings · paragraphs · tables · figures"]
    D --> G["parse · Gemini vision<br/>diagram pages rendered to PNG<br/>→ components + connections (strict JSON)"]
    G --> K["chunk<br/>tables atomic (Markdown) · diagrams 1 per page<br/>· text grouped by heading"]
    K --> E["embed<br/>gemini-embedding-2 multimodal<br/>(diagram = text + page image)"]
    E --> L["load_bigquery → build_indexes → verify_counts"]
```

![Level 3 citation drawer on a Gemini-vision diagram chunk: whole-page region plus the transcription](docs/screenshots/19-level3.png)

- **Element boxes come from the PDF text layer** (`page.search_for`, else the full page; `bbox_source` = `documentai` | `pdf_text_layer` | `page`) because Layout Parser v1.5 returns table structure without bounding boxes.
- **Unusable vision answers are not invented around**: pages whose transcription is empty, non-JSON or has no labelled components (e.g. logo/signature pages) are listed in `vision_failed_pages` and get no diagram chunk.
- **Known limitation: BigQuery allows one vector index per table.** The `embedding` index exists, so `VECTOR_SEARCH` on `embedding_multimodal` brute-forces; at scale the multimodal tier needs its own table.

---

## Level 4 architecture

**Query path** (`backend/app/engines/{level4_agentic,agent_runtime,agent_tools,agent_calculator,agent_verify}.py`; reuses the Level 3 `multimodal` tier):

```mermaid
flowchart LR
    U["User question"] --> QP["query_prep<br/>acronyms + element intent"]
    QP --> AT["agent_turn · Google ADK InMemoryRunner<br/>LlmAgent (Gemini on Vertex AI, ≤8 turns)"]
    AT <--> T1["search_procurement_corpus<br/>Level 3 embed + RRF + Ranking API"]
    AT <--> T2["check_amendments<br/>BigQuery amends_section / section_id"]
    AT <--> T3["calculate_contract_pricing<br/>AST-safe arithmetic"]
    AT <--> T4["verify_citations<br/>claim ↔ chunk check (+1 repair turn)"]
    AT --> SY["synthesize<br/>final ADK response + tool ledger citations"]
    SY --> EV["evaluate<br/>LLM judge (≠ synthesis)"]
```

![Level 4 architecture diagram and Node details pane showing 4 ADK turns, 14 tool calls, calculate_contract_pricing and verify_citations](docs/screenshots/20-level4.png)

- **Real ADK loop**: every model turn (`kind="model_turn"`), tool call (`kind="tool_call"`) and tool result (`kind="tool_result"`) streams as an `agent_turn` SSE event while the runner executes; if `verify_citations` leaves unsupported claims on the final answer, the engine runs one bounded repair turn (`repair=true`).
- **Gemini-only agent model**: `google-adk`'s Claude adapter requires the undeclared `anthropic` package, so Level 4 requires a Gemini synthesis model (`gemini-4-argon` or `gemini-3.8-flash`; selecting Claude returns HTTP 400). Claude models remain selectable as the LLM judge.

---

## Level 5 architecture (managed baseline)

**Query path** (`backend/app/engines/{managed_vertex,vais_search}.py`, `backend/app/core/vertex_search.py`):

```mermaid
flowchart LR
    U["User question"] --> QP["query_prep<br/>acronym expansion"]
    QP --> RT["retrieve · Vertex AI Search<br/>servingConfigs/default_search:search<br/>extractive segments + page numbers"]
    RT --> SA["synthesize (mode a)<br/>managed summary (summarySpec,<br/>same search call)"]
    RT --> SB["synthesize (mode b)<br/>selectable model over the segments"]
    SA --> EV["evaluate · LLM judge (≠ synthesis)"]
    SB --> EV
```

**Ingestion** (`backend/app/ingestion/{gcs_sync,managed_job}.py`): `discover_docs → upload_gcs` (md5 diff, no deletes) `→ import_documents` (`documents:import`, INCREMENTAL, bounded LRO poll) `→ verify_counts`; parse / chunk / embed / index are `stage_skipped` — "managed by Vertex AI Search". Provisioned by `scripts/provision_vertex_search.py` (default digital parser: layout-based chunking would disable extractive segments).

![Level 5: Vertex AI Search results with extractive segments and the managed summary](docs/screenshots/21-level5.png)

The synthesis picker adds a Level-5-only pseudo model `vertex-ai-search-summary` (HTTP 400 on other levels). Citations are page-level (`bbox_source: "page"`).

---

## What each stage shows

Every value is read from the stage event payloads the engine emits (and that recordings store verbatim in
`data/recordings/`).

| Stage | Level 1 | Level 2 adds | Level 3 adds | Level 4 (`level4_agentic`) |
| :-- | :-- | :-- | :-- | :-- |
| `query_prep` | original & prepared query | 16-acronym expansion + diff | `element_intent` + matched keywords | same as Level 3 |
| `embed_query` | `gemini-embedding-2` 3072-d, tokens | — | multimodal space | runs inside `search_procurement_corpus` tool calls |
| `retrieve` | parameterized `VECTOR_SEARCH` SQL, scores, `job_id`, bytes, cache hit | hybrid RRF top-50 (`vector_rank`, `lexical_rank`, index usage) | `embedding_multimodal`, element / layout / bbox badges | runs inside `search_procurement_corpus` + `check_amendments` tools |
| `rerank` | — | Discovery Engine Ranking API before/after ranks & cost | element badges joined per row | runs inside `search_procurement_corpus` (`reranker_used`) |
| `agent_turn` | — | — | — | live ADK `model_turn` / `tool_call` / `tool_result` stream (`turn_count`, `tool_call_count`, `tools_used`, calculator expression/result, `verify_citations` before/after repair counts) |
| `synthesize` | prompt + top-5 chunks, model, tokens | `[doc · section · page]` labels | `authority_headers` (latest amendment first), `element_mix` | final ADK response (`source: "adk_final_response"`), aggregated agent + tool tokens & cost, citations ordered by authority |
| `evaluate` | judge model (≠ synthesis), G / QA / CP, rationale, tokens & cost | — | — | — |

Also in the UI:

- **Citations** open a drawer with the real PDF page (rendered by PyMuPDF) and the cited excerpt located on it
  with `page.search_for`; when the excerpt can't be found it says so instead of guessing.
- **Model picker** per run (synthesis + judge from the verified catalog); **Ingestion** (`/ingest`) jobs with
  their own stage rail and BigQuery-reported index states; **Leaderboard** per (engine, synthesis, judge) pair;
  light / dark / system themes.

---

## Model catalog

Catalog in `backend/app/core/models_catalog.py`, prices in `backend/app/core/pricing.py` (published Vertex AI
rates with source URL and `price_as_of`), availability in `data/model_verification.json` (written by
`scripts/verify_models.py` — re-run it on your project).

| Model | Id | Provider path | Endpoint | Input $/1M | Output $/1M | Price as of | Verified |
| :-- | :-- | :-- | :-- | --: | --: | :-- | :-- |
| Gemini 4 Argon | `gemini-4-argon` | Vertex AI, google-genai `generate_content` | `global` | 2.00 | 10.00 | 2026-10-20 | ✅ HTTP 200, 2026-10-21 |
| Gemini 3.8 Flash | `gemini-3.8-flash` | Vertex AI, google-genai `generate_content` | `global` | 0.75 | 3.75 | 2026-10-21 | ✅ HTTP 200, 2026-10-21 |
| Claude Opus 5.5 | `claude-opus-5-5` | Anthropic on Vertex AI, `:rawPredict` | `global` | 4.00 | 20.00 | 2026-10-21 | ✅ HTTP 200, 2026-10-21 |
| Claude Fable 5.1 | `claude-fable-5-1` | Anthropic on Vertex AI, `:rawPredict` | `global` | 10.00 | 50.00 | 2026-10-21 | ✅ HTTP 200, 2026-10-21 |

- Gemini 4 Argon and Gemini 3.8 Flash prices are introductory global rates (through 2026-11-30 and 2026-12-31;
  Flash $1.50 / $7.50 from 2027-01-01); output rates include thinking tokens.
- Defaults: synthesis `GEMINI_FAST_MODEL` (`gemini-4-argon`), judge `JUDGE_MODEL` (`claude-opus-5-5`).
  **Judge ≠ synthesis is enforced server-side** (HTTP 400 before streaming); there is no fallback model.
- **Claude Fable** also requires Anthropic data sharing to be enabled on your project (one-time
  `setPublisherModelConfig`; see [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md)); until then it is marked unavailable.

Other billable services (details in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)):

| Service | Used by | Price | Source, as of |
| :-- | :-- | :-- | :-- |
| `gemini-embedding-2` | all levels | $0.20 / 1M input tokens | `pricing.py`, 2026-10-20 |
| Discovery Engine Ranking API | Levels 2–3 (`rerank`) | $1.00 / 1,000 counts; 1 count = 1 query of ≤ 100 docs (50 records = $0.001) | `pricing.py`, 2026-10-21 |
| Vertex AI Search (Enterprise + LLM add-on) | Level 5 | $4 / 1,000 queries + $4 / 1,000 for generative answers; index storage free ≤ 10 GiB | `pricing.py`, 2026-10-23 |
| Document AI Layout Parser | Level 3 ingestion | $10 / 1,000 pages; online requests ≤ 15 pages, so PDFs are split | `multimodal_job.py`, 2026-10-22 |

---

## Results so far (Levels 1–5)

Each level ran the 8 golden questions live (`DEMO_MODE=record`) with two (synthesis, judge) pairs: **Argon** =
`gemini-4-argon` + `claude-opus-5-5`, **Flash** = `gemini-3.8-flash` + `claude-opus-5-5` (Level 4 Flash has 7/8 recorded: `q_sla_credit_calculation` timed out at 120 s across 6 live attempts when single Flash turns stalled 46–110 s, so it is left unrecorded and returns `ReplayUnavailableError` in replay). G / QA / CP =
groundedness / QA relevance / context precision from the LLM judge (0–1); latency = engine run without the
judge; run cost = engine + judge, mean per question (total in brackets).

| Level | Argon G / QA / CP | Argon latency | Argon run cost (total) | Flash G / QA / CP | Flash latency | Flash run cost (total) |
| :-- | :-: | --: | --: | :-: | --: | --: |
| 1 · naive | 0.956 / 0.693 / 0.715 | 24.6 s | $0.0354 ($0.283) | 0.934 / 0.672 / 0.715 | 14.9 s | $0.0229 ($0.183) |
| 2 · hybrid + rerank | 0.971 / 0.838 / 0.644 | 28.6 s | $0.0471 ($0.377) | 0.984 / 0.855 / 0.637 | 13.8 s | $0.0309 ($0.247) |
| 3 · multimodal | 0.932 / 0.825 / 0.694 | 26.7 s | $0.0428 ($0.342) | 0.963 / 0.825 / 0.704 | 18.4 s | $0.0271 ($0.217) |
| 4 · agentic (ADK) | 0.929 / 0.981 / 0.637 | 47.2 s | $0.1183 ($0.946) | 0.984 / 0.973 / 0.750 *(7/8)* | 44.0 s | $0.0477 ($0.334) |
| 5 · Vertex AI Search, Argon over segments | 0.974 / 0.905 / 0.706 | 26.9 s | $0.0542 ($0.434) | — | — | — |
| 5 · Vertex AI Search managed summary | 0.531 / 0.312 / 0.706 | 3.5 s | $0.0326 ($0.261) | — | — | — |

Level 2 lifts QA relevance on amendment and table questions, and Level 3 recovers context precision on tables and
diagrams. Level 4 closes the multi-hop and arithmetic gaps (`q_soc_p1_response_matrix` QA 0.55 → 1.00,
`q_cross_solicitation_total_cost` QA 0.35 → 1.00 with exact `$2,765,500.00` via `calculate_contract_pricing`) at ~1.7× latency and ~2.8× Argon cost.
Level 5's retrieval is strong (Argon over its segments: QA 0.905), but its managed summary often says the sources lack facts that the top segment contains (QA 0.312) — fastest and cheapest, least correct.
Per-question scores, SQL, tool traces, prompts, judge rationales and provenance headers are in [`data/recordings/`](data/recordings/).

---

## Quickstart

Requirements: Python 3.11+ (3.12 in the Docker image), Node.js 20+. Live mode also needs the Google Cloud CLI
(`gcloud`, `bq`).

### (a) Zero-credential replay

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

# terminal 1 — API on :8000, plays data/recordings/** (no GCP calls, no credentials)
DEMO_MODE=replay .venv/bin/python -m uvicorn app.main:app --app-dir backend --port 8000

# terminal 2 — UI on :5173 (Vite proxies /api to :8000)
cd frontend && npm install && npm run dev
```

Open `http://localhost:5173`, pick a level, a golden question and a recorded pair (`gemini-4-argon` or
`gemini-3.8-flash` with judge `claude-opus-5-5`), and press Run; events replay with their recorded timing and
ad-hoc questions return `engine_error`. All five levels are enabled by default. (`docker-compose.yml` predates v3 — use the two commands above.)

### (b) Live on your own GCP project

1. **Configure.** `cp .env.example .env` (git-ignored), then set at least:
   ```bash
   DEMO_MODE=live                      # or record: live + writes data/recordings/**
   GCP_PROJECT_ID=your-gcp-project-id
   GCP_LOCATION=us-central1            # BigQuery location
   VERTEX_GENAI_LOCATION=global
   BQ_DATASET=rag_arena
   BQ_TABLE=document_chunks_v3         # v3 ingestion refuses BQ_TABLE=document_chunks (the frozen v2 table)
   ```
2. **Authenticate** with Application Default Credentials: `gcloud auth application-default login`
   (optionally isolate a profile via `CLOUDSDK_CONFIG` / `GOOGLE_APPLICATION_CREDENTIALS` in `.env`).
3. **Bootstrap** APIs, the corpus bucket, the dataset and the Level 3 Layout Parser processor. The script reads
   environment variables (not `.env`) and creates a v2-schema table named by `BQ_TABLE`, so pass the v2 name —
   the ingestion jobs create the v3 table themselves. Put the printed `DOCAI_PROCESSOR_ID` in `.env`.
   ```bash
   set -a; source .env; set +a
   BQ_TABLE=document_chunks ./scripts/bootstrap_gcp.sh
   ```
4. **Verify models** (writes `data/model_verification.json`; failing models are disabled with the error):
   `PYTHONPATH=backend .venv/bin/python scripts/verify_models.py`
5. **Run** the backend and UI as in (a), open `/ingest`, and run the ingestion job of each level you enable
   (Level 3 needs the `DOCAI_*` keys). A level's Run button stays
   disabled while its tier has 0 rows.

IAM for the backend principal: `roles/aiplatform.user`, `roles/bigquery.jobUser`, `roles/bigquery.dataEditor` on
`rag_arena`. Step-by-step setup, all `.env` keys, the Fable data-sharing call and troubleshooting:
[docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md).

---

## API & SSE protocol

| Method & path | Purpose |
| :-- | :-- |
| `GET /api/v1/health` | Mode, enabled levels, configured models, project/bucket from `.env` (the UI never hardcodes them) |
| `GET /api/v1/levels` | All five levels with `enabled`, stages and architecture descriptor |
| `GET /api/v1/models` | Verified catalog, defaults, and `recorded_pairs` playable in replay |
| `GET /api/v1/benchmarks` | Golden procurement benchmark (8 questions, 4 failure modes) |
| `POST /api/v1/battle/stream` | Run: `{query, question_id?, engines?, solicitation_filter?, synthesis_model?, judge_model?}` → SSE |
| `GET /api/v1/leaderboard` | Aggregates of recorded runs per (engine, synthesis, judge) with provenance |
| `POST /api/v1/ingest/jobs` · `GET /api/v1/ingest/jobs/{id}` · `GET /api/v1/ingest/jobs/{id}/stream` | Start / inspect / stream an ingestion job (`{level}`) |
| `GET /api/v1/ingest/status` | Live `COUNT(*)` by tier, index states, recent `ingest_runs` |
| `GET /api/v1/documents/page` · `GET /api/v1/documents/highlights` | Rendered PDF page (PNG) and excerpt highlight rectangles |
| `POST /api/v1/ingest/upload` | Guarded PDF upload (uploaded sessions are not indexed for Levels 1–3 yet) |

Run stream frames are `data: {json}\n\n`, bounded by `MAX_SSE_FRAMES` (600):

| Event | Key fields |
| :-- | :-- |
| `run_started` | `run_id`, `engines[]`, `demo_mode`, `question_id` |
| `stage_started` | `engine`, `stage`, `label`, `service`, `request` |
| `stage_completed` | `engine`, `stage`, `duration_ms`, `payload` |
| `agent_turn` | `engine`, `turn_index`, `kind` (`model_turn`\|`tool_call`\|`tool_result`), `tool_name`, `tool_args`, `tool_result_summary`, `duration_ms`, `tokens`, `repair` |
| `stage_failed` / `stage_skipped` | `error_type`, `error` / `reason` |
| `answer_complete` | `answer`, `citations[]`, `latency_ms`, `cost_usd`, `usage`, `flags` |
| `eval_complete` | `metrics`, `judge_model`, `reference_answer_used`, `duration_ms`, judge tokens & cost |
| `engine_error` | `error_type`, `error`, `failed_stage` |
| `stream_warning` · `battle_complete` | `message` · `engines_completed[]`, `engines_failed[]` |

Per-level `stage_completed.payload` fields are listed in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (§3).
Ingestion streams emit `job_started`, `stage_started|completed|failed`, `doc_progress`, `embed_progress` and
`job_complete` (counts read back from BigQuery).

---

## Repository layout

```text
backend/app/
  api/          routes.py (REST + SSE forwarder), documents.py, query_runs.py
  core/         config, telemetry (StageEmitter), models_catalog, pricing, ranking, architectures, …
  engines/      level1_naive, level2_hybrid, level3_multimodal, level4_agentic, agent_runtime, agent_tools,
                agent_calculator, agent_verify, agent_ledger, common_stages, synthesis, replay_engine;
                managed_vertex, vais_search; parked/
  evaluation/   judges, evaluator, scoring
  ingestion/    jobs, live_backends, parsers, bq_indexer, documentai_client, layout_documentai,
                vision_transcribe, multimodal, multimodal_job, …
  recording/    recorder, store, codec, redaction
backend/tests/  25 offline test modules + shared fakes
frontend/src/   app, pages, features/{levels,ingest}, components, lib, theme, styles, types
data/           sample_docs/procurement_corpus (45 PDFs), golden_benchmarks, model_verification.json,
                recordings/<engine>/<synthesis>__<judge>/<question_id>.json
docs/           ARCHITECTURE.md, SETUP_GUIDE.md, audits/, screenshots/
scripts/        bootstrap_gcp.sh, verify_models.py, provision_documentai.py, build/download corpus, …
infra/main.tf   Terraform (APIs, bucket, dataset, table, Layout Parser processor)
```

`docker-compose.yml` predates v3 and is being updated; `.env.example` is the configuration template.

---

## Testing

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests -q   # 245 passed (2026-10-23)
cd frontend && npm test                                 # 124 node test runner checks
cd frontend && npm run build                            # tsc + vite build
```

Backend tests are offline (fakes for Vertex AI, BigQuery, Document AI and Google ADK) and include the principle guards: no
timers in `routes.py`, no silent fallbacks, recording redaction, judge ≠ synthesis, and the public-docs leak scan.

## Security & leak hygiene

- **Identity only from the git-ignored `.env`** (project, bucket, credential paths); code defaults are empty.
- **Recorder redaction**: identity values become `${…}` placeholders and `assert_redacted` refuses to write
  otherwise. **Safe mode** (`?safe=1` or the header toggle) masks them in the UI for screen sharing.
- **No key files** (ADC only); parameterized queries, validated identifiers, and an explicit bound and timeout on
  every loop, retry, poll and request.

## Roadmap

| Phase | Scope | Status |
| :-- | :-- | :-- |
| 1 | Level 1 Naive Vector RAG | ✅ live |
| 2 | Level 2 BigQuery Hybrid RRF + Discovery Engine Ranking API (rerank before/after scores) | ✅ live |
| 3 | Level 3 Document AI Layout Parser + Gemini vision, multimodal embeddings, authority headers | ✅ live |
| 4 | Level 4 real Google ADK runner with retrieval, amendment, pricing and citation-check tools | ✅ live |
| 5 | Managed Vertex AI Search baseline (managed summary vs. model over extractive segments) | ✅ live |
| 6 | **Next:** Compare page, leaderboard from `query_runs`, updated compose, **Cloud Run** deployment (single container, least-privilege service account, IAP-protected live service, optional public replay service) | 🔒 planned |

## Data & license

- **Generated procurement corpus**: 18 interconnected solicitation PDFs across 3 solicitations (`scripts/build_procurement_corpus.py`; fictional, for benchmarking only).
- **CUAD**: 27 commercial agreements from the [Contract Understanding Atticus Dataset (CUAD) v1](https://zenodo.org/records/4595826) by The Atticus Project ([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)), fetched by `scripts/download_procurement_corpus.sh`.
- **Golden benchmark**: 8 questions across 4 failure modes in `data/golden_benchmarks/procurement_benchmark.json`.

## Acknowledgements

Built on Google Cloud (Vertex AI, BigQuery, Cloud Storage, Discovery Engine, Document AI, Google ADK), Anthropic Claude models on Vertex AI, PyMuPDF, Starlette, React, Vite and Tailwind CSS. Contract data from The Atticus Project's CUAD.
