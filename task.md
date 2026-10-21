# The RAG Arena (v3.0) — Task Tracker

> Rule: **one level at a time.** A phase's checkbox is ticked only with evidence pasted under it.
> Gate = Definition of Done D1–D7 in `spec.md §9`. Nothing in a later phase starts before the gate.
> `git commit`/`git push` only on explicit owner instruction.

## Phase 0 — Foundation reset (backend contracts + new UI shell) — *no GCP calls needed to finish* ✅ 2026-10-20

Evidence: `PYTHONPATH=backend .venv/bin/pytest backend/tests -q` → `76 passed`; `npm run build` → `✓ built in 3.74s` (`index-BiMvTIzW.js 241.76 kB`); `npm test` → `# pass 8 / # fail 0`; replay smoke (`DEMO_MODE=replay ALLOW_DEV_FIXTURES=1`, `question_id=dev_q01`) streams `run_started → stage_started/completed ×5 (duration_ms 0/61/182/250/120) → answer_complete → eval_complete → battle_complete` through the Vite proxy; ad-hoc question → `engine_error ReplayUnavailableError`; headless DOM trace shows rail/diagram/details following `replayed:true` frames (33→661 ms); light/dark/system screenshots captured. Audit: `docs/audits/2026-10-20-v2-authenticity-audit.md`.
Deviations recorded: Levels 2–5 run their v2 bodies under one measured `legacy` stage and emit `stage_skipped` for canonical stages until their phase; `evaluate` stage is emitted by the evaluator; ingest job embed/load/index stages fail loud (`IngestBackendUnavailable`) until Phase 1 wiring; `_AMENDMENT_LEDGER` and `search_procurement_corpus` stub remain in L4 until Phase 4.

### 0.A Backend
- [x] Write `docs/audits/2026-10-20-v2-authenticity-audit.md` (verbatim findings; the reason for v3).
- [x] Add `backend/app/core/telemetry.py`: `StageEmitter` protocol, `QueueStageEmitter`, `StageRecord`, `StageHandle` (monotonic timing, bounded payload size).
- [x] Add `backend/app/core/pricing.py`: per-model USD/1M tokens table; `cost_from_usage(usage)`; remove tier multipliers.
- [x] `models.py`: add `TokenUsage`, `EngineFlags`, `StageRecord`; `EngineResult.stage_log`, `.usage`, `.flags`; **delete `EngineResult.eval_metrics`**.
- [x] `base.py`: `run_query(..., emitter)` signature; `async with self.stage(emitter, "retrieve", service=..., request=...)` helper; `to_thread` wrapper that measures duration.
- [x] `config.py`: `ENABLED_LEVELS` (default `level1_naive`), `DEMO_MODE in {live, record, replay}`, `REPLAY_SPEED`, `MAX_SSE_FRAMES=600`, `INGEST_JOB_TIMEOUT_SECONDS`.
- [x] `engines/__init__.py`: registry filtered by `ENABLED_LEVELS`; move `vertex_vector_v2.py`, `alloydb_scann.py` to `engines/parked/` (not registered).
- [x] `core/architectures.py` + `GET /api/v1/levels`: per-level `ArchitectureDescriptor` (nodes with `stage_ids`, edges, `ingest_nodes/edges`, grid `col/row`) for all 5 levels; design metadata only — no timings or scores; test asserts every canonical stage of a level maps to ≥ 1 node.
- [x] `routes.py`: delete `_build_stage_event`, timer waits, `latency_ms // 3`; forward emitter events; emit `run_started`, `answer_complete`, `eval_complete`, `engine_error`, `battle_complete` per spec §6.3.
- [x] `recording/{recorder,store}.py`: `DEMO_MODE=record` writes `data/recordings/<engine>/<question_id>.json` with provenance header; `replay_engine.py` plays recordings with relative timing; ad-hoc questions in replay → `engine_error`.
- [x] `ingestion/jobs.py` + routes `POST /api/v1/ingest/jobs`, `GET /api/v1/ingest/jobs/{id}/stream`, `GET /api/v1/ingest/status`; `bq_indexer.py` DDL for `ingest_runs`, `query_runs`, `layout_source`, `ingest_run_id`.
- [x] `evaluation/battlefield.py`: delete `_ARCHITECTURE_BLUEPRINTS` static strings and per-engine strengths/verdicts; `scoring.py` computes Battle Score + diagnostics from `flags`/`stage_log`/metrics only.
- [x] Tests: `test_telemetry.py` (ordering, duration measured, failure emits `stage_failed`), `test_no_silent_fallbacks.py` (AST scan), `test_routes_no_timers.py`, `test_recording_roundtrip.py`, `test_ingest_jobs_api.py` (mocked BQ). Update existing tests for the new `run_query` signature; delete tests that asserted hand-typed metrics.
- [x] Evidence: `PYTHONPATH=backend .venv/bin/pytest backend/tests -q` → `__ passed`.

### 0.B Frontend shell (full rebuild)
- [x] Add `react-router-dom`; create `src/app/{App,routes,Shell}.tsx` with full-width layout (no `max-w-*` on `main`).
- [x] `src/theme/ThemeProvider.tsx` (`light|dark|system`, localStorage, `matchMedia` listener) + `ThemeToggle`; `tailwind.config.js` `darkMode:"class"` + semantic colors mapped to CSS vars; `src/styles/tokens.css` earthy palette (spec §7.1); `prefers-reduced-motion` guard.
- [x] `src/components/ui/` primitives: `Panel`, `Pill`, `Button`, `Tabs`, `DataTable`, `KeyValue`, `CodeBlock` (for SQL/API payloads).
- [x] `src/lib/sse.ts` (salvaged reader, 600-frame bound, abort timeout) + `src/features/levels/useBattleStream.ts` (append-only event log → per-engine stage timeline).
- [x] `src/features/levels/StageRail.tsx` + `StageDetail.tsx` (status from events only; elapsed counter stops on `stage_completed` and shows backend `duration_ms`).
- [x] `src/features/levels/ArchitectureDiagram.tsx` (spec §7.2.1): data-driven SVG from `GET /api/v1/levels` descriptors; node states `idle|active|done|failed|skipped` derived from the same event timeline; auto-follow details pane with pin; edge flow marker only between `done` source and `active` target; reused by the ingest job page with `ingest_nodes/edges`.
- [x] `src/features/ingest/{IngestPage,IngestJobStream}.tsx`; `src/features/levels/LevelPage.tsx` (layout spec §7.2); `OverviewPage` level ladder with locked cards; `DocumentViewer` re-tokenized.
- [x] Delete: `ArenaDashboard.tsx`, `ArchitectureInspector.tsx`, `ChunkingAnimator.tsx`, `PipelineFlowVisualizer.tsx`, `BattlefieldArena.tsx`, `MetricRadar.tsx`, `ArenaBattle.tsx`, inline keyframes in `index.html`.
- [x] Evidence: `npm run build` clean; screenshots light/dark/system of Overview + Level 1 page in replay mode.

---

## Phase 1 — Level 1: Naive Vector RAG (`level1_naive`) — branch `phase-1/level1-live`
- [x] Ingestion job `naive` tier: `discover_docs → parse (PyMuPDF, bbox) → chunk (360/60) → embed (batched, bounded concurrency) → load_bigquery → build_indexes (poll INFORMATION_SCHEMA, bounded) → verify_counts`; writes `ingest_runs`. (`be2a845`, `0300986`)
- [x] Engine stages emitted: `query_prep`, `embed_query`, `retrieve` (parameterized SQL + `rows_returned` + `bytes_processed` + scores), `synthesize` (usage from `usage_metadata`), `evaluate`. (`1adb425`)
- [x] Level 1 page wired; ingest status pill; run disabled when tier count = 0. (`frontend` live polish commit)
- [x] Record 8 golden questions (`DEMO_MODE=record`); replay verified offline. (`23ddce5`)
- [ ] Gate evidence D1–D7:
  - D1 ingestion (live, 2026-10-21): job `ing_db965791a174` → `job_complete`; stage `duration_ms` discover_docs 2 · parse 1211 (526 pages, parser `pymupdf`, `empty_docs []`) · chunk 107 (5067) · embed 199918 (5067 × 3072-d, 159 batches, `RETRIEVAL_DOCUMENT`) · load_bigquery 70993 (6 staging load jobs + transactional tier replace) · build_indexes 63243 · verify_counts 641; `counts_by_tier {"naive": 5067}`; `ingest_runs` row with `git_sha b63953c`. Index states (BigQuery-reported): vector index `ACTIVE (coverage 0%)` → `VECTOR_SEARCH` brute-forces until coverage builds; search index `TEMPORARILY DISABLED` (table < 10 GB).
  - D2 stage emission: battle stream emits `stage_started/stage_completed` for query_prep, embed_query, retrieve, synthesize, evaluate with backend-measured `duration_ms`; `test_routes_no_timers.py` guards `routes.py`.
  - D3 live runs: `run_901106d37279` (q_exact_clin_lookup, G 1.0 / QA 1.0 / CP 0.6, 5 citations, \$0.0098, 20.3 s), `run_ecc591d1aafe` (q_amendment_rate_comparison, 1.0 / 0.75 / 0.75, \$0.0235), `run_ec6a11ff6988` (q_sla_credit_calculation, 1.0 / 0.5 / 0.6, \$0.0217); `query_runs` rows written (live 6, record 8). Retrieve scans ≈128 MB per query (5067 × 3072-d brute force).
  - D4 UI (live, 2026-10-21): `run_f5e46f79dcba` q_vpc_failover_diagram — rail/diagram/details follow real events (Embed 247 ms · Retrieve 996 ms [BigQuery cache hit, 0 B] · Synthesize 17.9 s · Evaluate 2.71 s), 5 citations → bbox viewer, judge metrics 1.0/1.0/0.85, real parameterized SQL in stage detail; `/ingest` shows `ing_db965791a174` from `bigquery:ingest_runs` and verbatim index states. Screenshots (light idle/3 s/15 s/done/retrieve-detail, dark done, ingest light/dark) kept outside the repo. `npm test` 9/9, `npm run build` clean.
  - D5 recordings: 8 files in `data/recordings/level1_naive/` (headers `project_id "${GCP_PROJECT_ID}"`, `git_sha b63953c`); replay of `q_vpc_failover_diagram` reproduces recorded durations exactly [0, 208, 1748, 17004, 2680] ms; ad-hoc question in replay → `engine_error ReplayUnavailableError`. Recorded QA relevance: exact_clin 1.0 · vpc_diagram 1.0 · zero_trust 1.0 · soc_p1 0.85 · amendment 0.7 · lakehouse 0.67 · sla 0.5 · cross_cost 0.45 (Level 1 failure modes as designed).
  - D6 tests: `PYTHONPATH=backend .venv/bin/pytest backend/tests -q` → `111 passed`; frontend `npm run build` clean.
  - D7 owner sign-off: `____ (date)`

## Phase 2 — Level 2: BigQuery Hybrid + Reranker (`level2_hybrid`)
- [ ] Ingestion job `hybrid` tier (contextual lineage headers) + `SEARCH INDEX` build verified via `INFORMATION_SCHEMA.SEARCH_INDEXES`.
- [ ] Stages: `query_prep` (expansions listed), `embed_query`, `retrieve` (RRF SQL, vector/lexical ranks per row), `rerank` (Discovery Engine `rank()`; on failure → `stage_failed` + `engine_error`, never heuristic fallback; `scores_before/after`), `synthesize`, `evaluate`.
- [ ] UI: rerank node shows before/after score deltas table.
- [ ] Record + replay; gate D1–D7 evidence: `…`

## Phase 3 — Level 3: Layout-Aware Multimodal RAG (`level3_multimodal`)
- [ ] Provision Document AI Layout Parser processor (`bootstrap_gcp.sh`, `infra/main.tf`); `ingestion/layout_documentai.py` (tables → Markdown, blocks → bbox, `layout_source='documentai_layout'`).
- [ ] `ingestion/vision_transcribe.py`: render diagram pages → `gemini-4-argon` vision transcription (components + flows), `layout_source='gemini_vision'`; single-page PDF bytes for `embedding_multimodal`.
- [ ] Ingestion job `multimodal` tier with per-page progress; `embedding_multimodal` vector index built.
- [ ] Stages: `query_prep`, `embed_query` (multimodal), `retrieve` (pre-filter + authority headers shown in payload), `rerank`, `synthesize`, `evaluate`.
- [ ] UI: viewer renders page image with table/diagram bbox overlays; stage detail shows the transcription used.
- [ ] Record + replay; gate D1–D7 evidence: `…`

## Phase 4 — Level 4: Agentic RAG with Google ADK (`level4_agentic`)
- [ ] Real ADK path: `InMemoryRunner` + `session_service.create_session`, `run_async`, Vertex env (`GOOGLE_GENAI_USE_VERTEXAI`, project, location); bounded `max_turns=8`, timeout; ADK events → `agent_turn` SSE as they happen; final ADK text is the answer.
- [ ] Tools hit real data: `search_procurement_corpus` → Level 3 retrieval; `check_amendments` → BigQuery `amends_section` query (delete `_AMENDMENT_LEDGER`); `calculate_contract_pricing` AST-safe; `verify_citations` → claim/chunk check; unsupported claims trigger one bounded repair turn.
- [ ] Delete procedural fallback and hardcoded `CLIN-0004-AB` default; failure → `stage_failed` + `engine_error`.
- [ ] UI: `agent_turn` node expands into tool-call timeline (args, result summary, duration).
- [ ] Record + replay; gate D1–D7 evidence: `…`

## Phase 5 — Managed Vertex AI Search (`managed_vertex`)
- [ ] Provision Discovery Engine data store from `gs://<bucket>/sample_docs/` and wait for import (bounded poll) in `bootstrap_gcp.sh` / `infra/main.tf`.
- [ ] Stages: `query_prep`, `retrieve` (real `extractive_segments` with page spans → citations), `synthesize` (summary), `evaluate`; failure → `engine_error` (no local impersonation).
- [ ] Record + replay; gate D1–D7 evidence: `…`

## Phase 6 — Compare, Leaderboard, Public Packaging
- [ ] `/compare`: concurrent run of enabled levels, stacked stage rails, Battle Score from `query_runs`.
- [ ] `/leaderboard`: aggregated from `query_runs` (live) or recordings (replay); provenance shown per row; delete hand-typed `precomputed_runs.json` metrics.
- [ ] README rewrite (v3 claims only), `publish.json` scrub check, `docker compose up` in replay.
- [ ] **Cloud Run deployment (spec §8.1)**: root multi-stage `Dockerfile` (SPA build + Uvicorn serving `/api` and static SPA with route fallback); runtime service account with least-privilege IAM (no key files); `deploy/cloudrun.env.example` + git-ignored `deploy/cloudrun.env`; `infra/main.tf` Cloud Run v2 service + Artifact Registry + IAP; `scripts/deploy_cloudrun.sh`; optional Cloud Run Job for long ingestion. Gate: IAP-protected URL, live run + ingestion job succeed from the deployed UI, image leak scan clean.
- [ ] Decide fate of parked `vertex_vector_v2` / `alloydb_scann` (re-enable one at a time under the same gate, or remove).

---

## v2.0 history (closed)
Phases 1–7 of v2.0 (corpus, 7 engines, 4-step UI, I1–I4 hardening, live GCP bootstrap) are recorded in
`git log` up to `df45aff` and in `docs/audits/2026-10-20-v2-authenticity-audit.md`.
