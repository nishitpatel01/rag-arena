# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [1.6.3](https://github.com/nishitpatel01/rag-arena/compare/v1.6.2...v1.6.3) (2026-11-19)


### Documentation

* **readme:** refresh Order A ladder, Gemini 4 Hydrogen results & 12 v1.6.2 screenshots ([db4f00d](https://github.com/nishitpatel01/rag-arena/commit/db4f00db47cebd66987f1082d7c6dddba9323ad2))
* **readme:** refresh Order A ladder, Gemini 4 Hydrogen results, and 12 v1.6.2 screenshots ([0115475](https://github.com/nishitpatel01/rag-arena/commit/0115475696579bde4064312da8847dc5d40efa73))

## [1.6.2] - 2026-11-18

Gemini 4 Hydrogen Replay Refresh (`183` official recordings), Multimodal & ADK Engine Quality Fixes, Single-Row Navigation, Responsive Workbench Fit, WCAG AA Contrast, Route Code-Splitting, and Cloud Run/Terraform Model Armor Parity (`$0/mo` standing idle).

### Added
- **Live Gemini 4 Hydrogen + Claude Opus 5.5 Benchmark Cohort (`scripts/record_live_benchmarks.py`, `data/recordings/`)**:
  - Added spend-bounded, resumable CLI recorder (`--max-spend-usd`, `--overwrite`, `assert_redacted`, `0644` permissions) and recorded all `48/48` standing golden runs (`8/8` across `managed_vertex`, `rag_engine`, `level1_naive`, `level2_hybrid`, `level3_multimodal`, and `level4_agentic` — zero timeouts on `q_sla_credit_calculation`, expanding total official replay recordings from `135` to **`183`**).
  - Enables full zero-credential replay and live-joined Overview TCO/fit scoring on `gemini-4-hydrogen__claude-opus-5-5` (`1 · Vertex Search` Battle `89.4`, `8 · Agentic ADK` Battle `88.8` / QA `0.961` at `21.6 s`, `2 · RAG Engine` Battle `87.8`, `4 · BQ Hybrid` Battle `85.7` at `11.5 s`).
- **Single-Row `"Architectures ▾"` Menu, Responsive Diagram Fit & Route Code-Splitting (`Shell.tsx`, `navMenuModel.ts`, `ArchitectureDiagram.tsx`, `diagramModel.ts`, `routes.tsx`)**:
  - Replaced inline 8-tab header wrap with an accessible `Architectures ▾` menu (`aria-haspopup="menu"`, active route label, pulsing ingest indicator, lock states) so the top navigation fits on a single row.
  - Added responsive viewBox scaling (`MIN_FIT_SCALE = 0.72`, `NODE_W = 168`) eliminating horizontal clipping on 7–8 column Composable Workbench pipelines at `1600×1000`, plus `React.lazy` + `<Suspense>` route splitting cutting the initial JS bundle `-50.0%` (`693.8 kB` → `346.7 kB`, `0` Rollup warnings).

### Fixed
- **Level 7 Multimodal Orphan-Heading Elimination & Level 8 ADK Negative-Lookup Guard (`multimodal.py`, `level3_multimodal.py`, `agent_runtime.py`)**:
  - Prevented `group_page_elements()` from flushing heading-only buffers before tables/subheadings and added `dedupe_heading_stubs()` pre-rerank filtering (`deduped_heading_stubs` reported in `retrieve`).
  - Added authoritative negative-lookup termination rule to `AGENT_INSTRUCTION` when `check_amendments` returns zero amendments, cutting Level 8 Argon engine spend on `q_soc_p1_response_matrix` `-70.2%` (`$0.1807` → `$0.0538`, QA `0.98`).
- **WCAG 2.1 AA Contrast, `/releases` Nested Inline Code, `/compare` Deduplication, `SafeMode` & Model Armor IAM Parity (`tokens.css`, `releasesModel.ts`, `DocMarkdown.tsx`, `LevelChecklist.tsx`, `ScopeSelector.tsx`, `ModelPicker.tsx`, `scripts/deploy_cloudrun.sh`, `infra/main.tf`)**:
  - Tuned `--ink-3` and light/dark semantic accents to strict `>= 4.5:1` WCAG AA across all surfaces, rendered nested inline `code` and safe links inside **bold** changelog items, deduplicated shared `disabledReason` banners on `/compare`, masked `upl_*` workspaces/PDF filenames in `SafeMode`, and auto-bound `roles/modelarmor.user` + `modelarmor.googleapis.com` when `MODEL_ARMOR_TEMPLATE_ID` is set (**510 backend tests** across `54` files; **231 frontend tests** across `28` suites).

## [1.6.1] - 2026-11-17

Enterprise Showcase & GCP Architecture Advisor: Interactive Use-Case Wizard & Monthly TCO Calculator, Google Cloud Model Armor Guardrails, Vertex AI GenAI Evaluation Service + Split Engine/Judge Cost, Industry Packs, Executive Brief Export, and Replay Speed Control (`$0/mo` standing idle).

### Added
- **Interactive Use-Case Architecture Wizard & Monthly GCP TCO Calculator (`backend/app/core/tco.py`, `backend/app/api/tco_api.py`, `frontend/src/features/wizard/`, `OverviewPage.tsx`)**:
  - Deterministic FCIS TCO and workload-fit advisor across Levels 1–8 (`GET /api/v1/tco/defaults`, `POST /api/v1/tco/estimate` with strict bounded validation) joining transparent published GCP pricing (`$0/mo` serverless idle vs dedicated AlloyDB / Vector Search 2.0 crossover) with real recorded cohort scores (zero hardcoded metrics).
  - 5 curated enterprise scenario presets, interactive query-volume and corpus-size log sliders, primary + runner-up recommendations, sortable 8-level TCO matrix, and 1-click deep links into `/levels/*`, `/compare`, and `/workbench`.
- **Google Cloud Model Armor Guardrails (`backend/app/core/model_armor.py`, `scripts/provision_model_armor.py`, `pipeline_spec.py`, `composable.py`, `WorkbenchPage.tsx`)**:
  - Real REST v1 client (`:sanitizeUserPrompt`, `:sanitizeModelResponse`) with bounded retries, PI/Jailbreak/RAI/SDP/Malicious URI screening, `$0/mo` idempotent template provisioning (`--dry-run` default), and `${MODEL_ARMOR_TEMPLATE_ID}` redaction.
  - Added `8. Safety Guardrail (Model Armor)` slot in Composable Workbench (`/workbench`), `guardrail_in` / `guardrail_out` stage nodes, 1-click prompt-injection test helper, and fail-loud `ModelArmorConfigurationError` when unconfigured in `live`/`record` mode.
- **Vertex AI GenAI Evaluation Service & Split Engine vs. Judge Economics (`backend/app/evaluation/vertex_eval.py`, `judges.py`, `scoring.py`, `leaderboard.py`, `routes.py`, `Scoreboard.tsx`, `LeaderboardTable.tsx`, `RunPills.tsx`)**:
  - Added judge-only `vertex-genai-eval` catalog entry calling Vertex AI `:evaluateInstances` (`GROUNDEDNESS`, `QUESTION_ANSWERING_QUALITY`, `FULFILLMENT`).
  - Split operational **Engine $** (scored in Battle Score efficiency) from external **Judge $** (unscored autorater overhead) and **Total $** across SSE `eval_complete`, `/leaderboard`, `/compare`, and level stage rails.
- **Industry Benchmark Packs, Executive Brief Export & Replay Speed Toggle (`frontend/src/features/packs/`, `frontend/src/features/export/`, `ReplaySpeedSelect.tsx`, `ComparePage.tsx`, `LeaderboardPage.tsx`, `LevelPage.tsx`)**:
  - 5 industry packs (`All`, `Federal Procurement`, `SLA & Financial`, `Security & SOC`, `Legal / CUAD`) distinguishing `Recorded (Replay + Live)` questions from `Live Template` prompts.
  - One-click Executive Decision Brief export (`.md`, formula-injection-safe `.csv`, `@media print` PDF) with full `SafeMode` (`?safe=1`) redaction on `/leaderboard` and `/compare`.
  - Per-request `replay_speed` (`1×` / `4×` / `Instant`, validated `1.0`..`100.0`, strictly honored ONLY when `DEMO_MODE=replay`) wired through `routes.py`, `battleTransport.ts`, `/compare`, and `/levels/*`.
- **Gemini 4 + Anthropic Claude Catalog, Synced App-Wide Model Selector & Model Pair Recommender (`pricing.py`, `models_catalog.py`, `model_recommender.py`, `tco.py`, `GlobalModelPopover.tsx`, `ArchitectureWizard.tsx`, `ModelPicker.tsx`)**:
  - Expanded Vertex AI catalog and `PRICE_TABLE` with live-verified `gemini-4-hydrogen` (`$1.50/$7.50`), `gemini-4-carbon` (`$2.00/$10.00`), `gemini-4-argon` (`$2.00/$10.00`), `claude-sonnet-4-6` (`$3.00/$15.00`), `claude-opus-5-5` (`$4.00/$20.00`), `claude-fable-5-2` (`$10/$50`, data-sharing gated), and `vertex-genai-eval` (`$0.75/$3.75`), marking `gemini-3.8-flash` as `legacy: true` and refreshing `data/model_verification.json` via `scripts/verify_models.py`.
  - Retired `gemini-3.8-flash` from Overview defaults (`DEFAULT_WORKLOAD.synthesis_model = "gemini-4-hydrogen"`, balanced preset `gemini-4-carbon`, multi-hop audit `gemini-4-argon`), expanded `TCO_SYNTHESIS_MODELS` to 6 models with Level 8 ADK Gemini-4 worker watch-outs, and labeled fallback cohorts honestly as `Reference cohort: gemini-4-argon + claude-opus-5-5`.
  - Added deterministic cross-family Model Pair Recommender (`GET /api/v1/models` `recommended_pairs`), app-wide `rag-arena:models-changed` selection sync across the header `GlobalModelPopover`, above-the-fold Overview Advisor Worker/Judge bar, and 1-click `<ModelPicker>` recommend helper.

### Fixed
- **Dockerfile Externalized Theme Script Parity (`Dockerfile`, `backend/tests/test_auth_and_headers.py`)**:
  - Stage 1 (`AS spa`) now copies `frontend/public ./public` and verifies `dist/theme-init.js` after `npm run build` so CSP-compliant `script-src 'self'` theme initialization is guaranteed inside container builds (**503 backend tests** across `53` files; **225 frontend tests** across `27` suites).

## [1.6.0] — 2026-11-16

P0 Security, Data Correctness, Concurrency Bounds & Terraform Parity (`$0/mo` standing idle).

### Added
- **Cryptographic IAP JWT Verification & Public Session Cookie Isolation (`backend/app/core/auth.py`, `config.py`, `main.py`, `uploads_api.py`, `routes.py`)**:
  - `ACCESS_MODE=iap` (fail-closed Cloud Run default when `K_SERVICE` is set in `live`/`record`) verifies `X-Goog-IAP-JWT-Assertion` ES256 signatures, `exp`/`iat`, `iss`, and `aud == IAP_JWT_AUDIENCE` via `google-auth`, ignoring unverified `X-Goog-Authenticated-User-Email` headers and redacting `${IAP_JWT_AUDIENCE}`.
  - `ACCESS_MODE=public` (local dev) isolates callers via an `HttpOnly; SameSite=Lax` session cookie (`__Host-rag_arena_sid` over HTTPS / `rag_arena_sid` over HTTP) instead of a shared `"anonymous"` owner.
  - Pure-ASGI security headers middleware (`Content-Security-Policy` with externalized `/theme-init.js` and `script-src 'self'`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`) plus `/api/v1/ingest/jobs` authentication, `LIVE_INGEST` gate, and `MAX_CONCURRENT_INGEST_JOBS = 2` cap.
- **Canonical Origin Allowlist, PDF Active-Content Hardening & >30-Page Customer Uploads (`uploads.py`, `uploads_api.py`, `parsers.py`, `layout_documentai.py`, `multimodal_job.py`)**:
  - Eliminates `Host:`-header Origin spoofing via `ALLOWED_UPLOAD_ORIGINS` (plus strict Cloud Run HTTPS hostname / localhost validation).
  - Expands PDF safety checks to reject `/SubmitForm`, `/ImportData`, `/GoToR`, `/GoToE`, `/RichMedia`, `/Movie`, `/Sound`, `/XFA`, `/AA`, non-destination `/OpenAction`, and `xref > 200,000` bombs while preserving standard `/URI` web hyperlinks; enforces pre-download `MAX_UPLOAD_FILE_BYTES` (`512 MiB`) to protect Cloud Run `/tmp` tmpfs.
  - Lifts silent `30`-page truncation on customer uploads (`upl_*`) up to `MAX_UPLOAD_PDF_PAGES` (`500` default) and aligns Level 3 `estimate_cost()` with `MAX_UPLOAD_VISION_PAGES` (`100` default).

### Fixed
- **Level 7/8 SQL Completeness, Honest `bbox_source` & Untrusted Prompt Delimiters (`alloydb_scann.py`, `vertex_vector_v2.py`, `synthesis.py`, `evaluator.py`)**:
  - Adds `doc_type`, `effective_date`, `amends_section`, and `bbox_json` to Level 7 AlloyDB hybrid `SELECT`, attributes full-page fallback boxes honestly as `"page"` across Levels 7 and 8, and wraps retrieved chunks / candidate answers in `<untrusted_retrieved_context>` / `<untrusted_candidate_answer>` breakout-escaped tags.
- **Bounded SSE Queues, Per-Subscriber Ingest Streams & Terraform Parity (`routes.py`, `jobs.py`, `gcp_clients.py`, `infra/main.tf`, `scripts/bootstrap_gcp.sh`, `scripts/deploy_cloudrun.sh`)**:
  - Bounds battle SSE queues (`maxsize=256`) with `finally` worker cancellation on client disconnect, isolates concurrent ingest SSE subscribers, locks `_VVS2_CLIENTS` while refreshing ADC tokens outside `_CLIENT_LOCK`, and brings `infra/main.tf` + `bootstrap_gcp.sh` to full `document_chunks_v3` / `ingest_runs` / `query_runs` / `upload_bucket` / `IAP_JWT_AUDIENCE` / 8-level replay parity (**475 backend tests** across `48` files; **198 frontend tests** across `22` suites).

## [1.5.0] — 2026-11-16

In-app Releases & Documentation tab (`/releases`) and deterministic `release-please` GitHub Actions automation.

### Added
- **In-App Releases & Documentation Tab (`/releases`, `frontend/src/features/releases/`, `Shell.tsx`)**:
  - Top-bar `Releases` navigation tab and clickable `v1.5.0` build badge in `Shell.tsx` opening a unified 3-subview documentation hub (`Release Timeline`, `System Architecture`, `Setup Guide`) synced with `?tab=timeline|architecture|setup_guide`.
  - **Release Timeline**: parses `CHANGELOG.md` (supporting both Keep-a-Changelog and `release-please` headings) with live repository telemetry chips (`8` architectures, `135` committed recordings), instant search, `running build` badge matching `/api/v1/health`, and mode-aware 1-click route deep-link pills (`/workbench`, `/compare`, `/leaderboard`, `/ingest`, `/levels/*`).
  - **Dependency-free AST Markdown Reader (`releasesModel.ts`, `DocMarkdown.tsx`)**: renders `docs/ARCHITECTURE.md` and `docs/SETUP_GUIDE.md` with sticky section Table of Contents, deduplicated GitHub-compatible heading anchors, GFM alignment tables, `CodeBlock` copyable snippets, safe link allowlisting (`#`, `/`, `http(s)://`, mapping repo doc links directly to in-app `/releases` tabs), and full `SafeMode` (`?safe=1`) identity masking using pure React JSX nodes.
- **Single-Source Manifest Endpoint & 5-Way Drift Gate (`backend/app/core/releases_docs.py`, `GET /api/v1/releases`, `scripts/build_docs_manifest.py`)**:
  - `GET /api/v1/releases` (`Cache-Control: no-store`) compiles `CHANGELOG.md`, `docs/ARCHITECTURE.md`, `docs/SETUP_GUIDE.md`, and `RecordingStore` counts dynamically with `mtime_ns` caching and strict repository path-boundary checks; `Dockerfile` + `.dockerignore`/`.gcloudignore` bundle only the 3 Markdown sources (`~75 KB`) while keeping screenshots excluded.
  - `scripts/build_docs_manifest.py --check` enforces 5-way version alignment across `CHANGELOG.md`, `backend/app/core/config.py`, `frontend/package.json`, `frontend/package-lock.json`, and `dev_q01.json`, plus zero-leak scanning against `publish.json` and `.env`.
- **Automated Release PR & CI Pipeline (`.github/workflows/release-docs.yml`, `release-please-config.json`, `.release-please-manifest.json`)**:
  - Every PR and push to `main` runs `build_docs_manifest.py --check`, the offline `pytest` suite, `npm test`, and `npm run build` (`contents: read`).
  - Pushes to `main` run `googleapis/release-please-action` (SHA-pinned, zero external secrets) to maintain an atomic Release PR updating `CHANGELOG.md` and all 4 versioned files, automatically creating the `v1.x.y` GitHub Release and tag on merge.
  - Offline test suites expanded to **454 backend tests** (`44` files) and **198 frontend tests** (`22` suites).

## [1.4.0] — 2026-11-04

Ephemeral Operational & High-QPS ScaNN Benchmarks: Level 7 (`alloydb_scann`) and Level 8 (`vertex_vector_v2`) recorded live and torn down to `$0/mo` standing idle cost.

### Added
- **Level 7 — AlloyDB for PostgreSQL + ScaNN Hybrid (`backend/app/engines/alloydb_scann.py`, `psycopg==3.3.8`)**:
  - Parameterized PostgreSQL hybrid search inside a transaction (`SET LOCAL scann.num_leaves_to_search = 8`, two-level tree-AH ScaNN cosine `<=>` leg + `ts_rank_cd` GIN full-text leg fused in-SQL via `k = 60` RRF) followed by Discovery Engine Ranking API reranking and selectable catalog LLM synthesis.
  - Runs identically against Cloud AlloyDB (loopback `alloydb-auth-proxy` on `127.0.0.1`) and zero-cloud local AlloyDB Omni Docker (`scripts/ephemeral_alloydb.sh --omni`).
- **Level 8 — Vertex AI Vector Search 2.0 (`backend/app/engines/vertex_vector_v2.py`, `backend/app/core/vvs2_client.py`)**:
  - Regional REST `v1beta` (`vectorsearch.googleapis.com`) `dataObjects:batchSearch` combining `3072-d` `denseScann` ANN (`gemini-embedding-2`) and deterministic CRC32 sublinear-TF sparse token vectors via server-side `combine.ranker.rrf`, followed by Discovery Engine Ranking API reranking and selectable catalog LLM synthesis.
- **Ephemeral Spin-Up → Record → Teardown Automation (`scripts/ephemeral_alloydb.sh`, `scripts/ephemeral_vvs2.sh`, `scripts/record_scann_benchmarks.py`)**:
  - Exports the existing `526` `3072-d` BigQuery `hybrid` chunks directly (`0` re-embedding API calls), trains ScaNN centroids strictly *after* bulk-loading vectors, records the `16` golden runs per engine (`gemini-4-argon` + `gemini-3.8-flash` judged by `claude-opus-5-5`) with optional `query_runs` persistence, and enforces `EXIT`-trapped teardown verified to `0` remaining `rag-arena-ephem-*` GCP resources (`$0/mo` idle).
- **Standing `$0/mo` Live Defaults, Workbench Integration & Redaction (`config.py`, `pipeline_spec.py`, `composable.py`, `redaction.py`, `frontend/`)**:
  - `DEMO_MODE=replay` enables all **8 levels** (`DEFAULT_ENABLED_LEVELS`, **135 committed recordings** total including **32 new Level 7–8 runs**); `DEMO_MODE=live|record` defaults to the **6 standing `$0/mo` levels** (`DEFAULT_LIVE_LEVELS`) unless `ENABLED_LEVELS` explicitly opts in.
  - Added `SCANN_INDEX_PROFILES` (`alloydb_scann`, `vertex_vector_v2`) to `PipelineSpec`, `ComposableRAGEngine`, Workbench, Compare (`EPHEMERAL_SCANN` badges), and `/ingest` CLI runbook notice; redacts `ALLOYDB_DSN`, `postgresql://` URIs, and `VVS2_COLLECTION` across recordings and safe mode.
  - Offline test suites expanded to **447 backend tests** (`43` files) and **194 frontend tests** (`21` suites).

## [1.3.0] — 2026-11-03

Composable RAG Workbench (`/workbench`) and Controlled Ablation Lab + Retrieval Diff Inspector (`/compare`).

### Added
- **Canonical `PipelineSpec` & Level 1–6 Presets (`backend/app/core/pipeline_spec.py`)**:
  - Typed `IndexProfile` (`corpus_store` × `tier_profile`) + 7 orthogonal `QuerySlots` (`query_prep`, `retriever`,
    `candidate_k`, `top_k`, `reranker`, `context_order`, `orchestration`, `agent_tools`) with deterministic SHA-256
    `pipeline_id` (`pipe_<12hex>`), explicit cross-slot validation (`validate_pipeline_spec`), dynamic
    `ArchitectureDescriptor` synthesis (`build_pipeline_descriptor`), and 1-click preset mappings for all 6 levels.
  - REST endpoints `GET /api/v1/pipelines/presets` and `POST /api/v1/pipelines/validate`.
- **Modular `ComposableRAGEngine` & `query_runs` provenance (`backend/app/engines/composable.py`, `api/routes.py`, `api/query_runs.py`)**:
  - Executes any valid `PipelineSpec` over existing indexed stores (`bigquery`, `vertex_ai_search`,
    `vertex_rag_engine`) at `$0/mo` new standing infrastructure cost — including BigQuery lexical-only `SEARCH()`,
    post-retrieval Discovery Engine `RankingClient.rank` on managed stores (`rag_engine` / `managed_vertex`),
    configurable amendment-first context ordering, and selectable ADK `LlmAgent` tool subsets.
  - `POST /api/v1/battle/stream` accepts custom `pipelines[]` (`slot_id`, `label`, `spec`, optional per-slot
    `synthesis_model`); `query_runs` persists `pipeline_id` and `pipeline_spec JSON`.
- **Interactive Composable Workbench (`/workbench`, `frontend/src/features/workbench/`)**:
  - 1-click Level 1–6 preset pills (`?preset=<levelId>`), 8 validated query-slot controls with ADK tool toggles,
    0-ms client-side live `ArchitectureDiagram` topology updates, and replay-mode preset lock (`Custom recipes require
    DEMO_MODE=live`).
- **Controlled 2–4 Recipe Ablation Lab & Retrieval Diff Inspector (`/compare`, `frontend/src/features/compare/`)**:
  - Dual-mode `/compare` (`Preset Ladder` vs `Ablation Lab` on an isolated `compare:ablation` session) with 4 scientific
    starter templates, live `varying: ...` delta indicator, and per-slot synthesis model override with a shared judge.
  - `RetrievalDiffPanel` + `retrievalDiffModel.ts` computing chunk rank deltas (`promoted`, `demoted`, `same`, `added`,
    `dropped`) against any baseline slot, auto-switching between exact `chunk_id` and cross-store `doc_name + page`
    alignment with clickable PDF source citations.
- Offline test suites expanded to **431 backend tests** (`40` files) and **190 frontend tests** (`20` suites).

## [1.2.0] — 2026-11-03

Level 6: Managed Vertex AI RAG Engine (`rag_engine`) across the benchmark corpus and private user PDF uploads.

### Added
- **Level 6 — Managed Vertex AI RAG Engine (`rag_engine`)**:
  - **Safe minimum-cost provisioning (`scripts/provision_rag_engine.py`)**: inspects regional `ragEngineConfig` before
    touching `RagManagedDb` — reuses active regional configurations untouched (`$0/mo` incremental compute on projects
    already running `RagManagedDb`), defaults fresh projects to `RagManagedDbConfig(serverless=Serverless())` via
    `aiplatform_v1beta1`, and never calls `unprovisioned` or modifies corpora outside `rag-arena-*`.
  - **Corpus & isolated private upload ingestion (`backend/app/ingestion/rag_engine_job.py`, `uploads_api.py`)**:
    idempotent GCS-backed `ImportRagFiles` (`512`-token chunks, `100`-token overlap, `text-embedding-005`) into
    `rag-arena-procurement` (`45` PDFs across bounded `<=25`-URI batches), plus zero-download per-workspace isolated
    `RagCorpus` (`rag-arena-upl_<12hex>`) deleted synchronously by `DELETE /api/v1/uploads/{id}` (raw numeric
    `rag_corpus_id` stripped from public HTTP manifests and redacted as `${RAG_ENGINE_CORPUS_ID}` in recordings).
  - **Two-mode live query engine (`backend/app/engines/rag_engine.py`, `backend/app/core/rag_engine_client.py`)**:
    - *Mode (a)*: regional `:retrieveContexts` (`$0/query` API fee, cosine distance + `chunk.pageSpan` PDF page
      citations, `ragFileIds` solicitation scoping) paired with any verified catalog synthesis LLM (`gemini-4-argon`,
      `gemini-3.8-flash`, etc.).
    - *Mode (b)*: pseudo-model `vertex-rag-engine-grounded` (Level 6 only) invoking Gemini `generate_content` with native
      `Tool(retrieval=Retrieval(vertex_rag_store=VertexRagStore(...)))` and extracting `grounding_metadata.grounding_chunks`
      into clickable PDF citations.
  - **Live diagram, Level 6 telemetry views, replay recordings, and Cloud Run parity**:
    `level6Payloads.ts` + `level6Views.tsx` (`RetrieveContexts` cosine-distance table, `RagCorpus` ingestion summary,
    `VertexRagStore` grounding mode), **24 new `DEMO_MODE=record` golden runs** (**103 committed recordings** total),
    and `roles/aiplatform.user` + `RAG_ENGINE_*` wiring in `scripts/deploy_cloudrun.sh` and `infra/main.tf`.
  - Offline test suites expanded to **415 backend tests** (`38` files) and **182 frontend tests** (`18` suites).

## [1.1.1] — 2026-11-03

Upload ingestion index-inspection fast-path (`~58 s` → `<3 s` on `build_indexes`).

### Changed
- **Upload workspace `build_indexes` fast-path** (`backend/app/ingestion/live_backends.py`, `backend/app/ingestion/jobs.py`):
  `upl_<12hex>` upload ingestion jobs inspect table-wide BigQuery `INFORMATION_SCHEMA` index states once via
  `snapshot_upload_indexes` (`poll_skipped_reason: "upload_scope_uses_table_indexes"`) instead of re-issuing table-level
  index DDL and waiting out the 60 s poll deadline when `document_chunks_v3` indexes already exist (bootstrapping async
  index DDL without polling only on an unindexed cold table). Benchmark corpus runs (`scope=None`) retain full DDL +
  bounded polling.

## [1.1.0] — 2026-11-02

Bring your own PDFs across all five RAG architectures, plus Leaderboard and Compare winner highlighting.

### Added
- **Private user PDF upload workspaces (`upl_<12hex>`)**:
  - Dedicated private Cloud Storage bucket (`UPLOAD_BUCKET`, `UPLOAD_BUCKET_MODE=auto|existing`, optional CMEK via
    `UPLOAD_BUCKET_KMS_KEY`) verified on startup for enforced public access prevention, uniform bucket-level access, and
    zero public IAM/ACL bindings; typed remedies on permission, org-policy, or name-conflict errors (never silent fallback).
  - Direct-to-GCS Origin-bound resumable upload sessions (`POST /api/v1/uploads/{id}/files` → browser `PUT` with `308`
    resume probe → `POST .../finalize`) bypassing Cloud Run's 32 MiB HTTP/1 request cap with no app-imposed file-size,
    page, or count limits; server-side PDF safety validation rejects non-PDFs, encrypted files, `/JS`/`/JavaScript`,
    `/Launch` actions, and `/EmbeddedFiles` while streaming in constant heap (`O(1)` `RssAnon`).
  - Workspace manifests stored in GCS with generation preconditions and owner bound to SHA-256 of the IAP identity
    (`404` for other principals); zero upload routes or UI controls in `DEMO_MODE=replay` or on the public replay service.
- **Scoped upload ingestion and querying across all five levels**:
  - Levels 1–2 (`naive`, `hybrid`) and Level 3 (`multimodal`) load and replace rows scoped to
    `(tier_profile, solicitation_id = upl_*)` while unscoped benchmark corpus queries and counts exclude `upl_*` rows.
  - Level 3 pre-flight cost estimate (`GET /api/v1/uploads/{id}/estimate`) and mandatory `confirm_cost_usd` guard
    before paid Document AI Layout Parser + Gemini vision runs; honest `UPLOADED_DOCUMENT` metadata without corpus
    filename assumptions.
  - Level 4 (`level4_agentic`) reuses the workspace's Level 3 `multimodal` rows and closure-pins `upload_scope` inside
    `search_procurement_corpus` and `check_amendments` so prompt-injected PDFs cannot redirect tool queries across scopes.
  - Level 5 (`managed_vertex`) provisions an isolated `rag-arena-uploads` data store and `rag-arena-uploads-search` app
    with filterable `workspace_id`/`doc_name` schema, zero-download `_vais_import.jsonl` (`dataSchema="document"`)
    imports, `workspace_id: ANY("upl_...")` search filtering, and synchronous VAIS + BigQuery + GCS deletion on
    `DELETE /api/v1/uploads/{id}`.
- **Reference-free evaluation, Leaderboard scopes, and winner highlighting**:
  - Reference-free QA relevance rubric (`reference_answer_used: false`, `no reference answer` badge) for upload and
    ad-hoc runs; `query_runs` tracks `solicitation_id` and `scope` (`golden` | `uploads`), and `DEMO_MODE=record`
    refuses to overwrite golden recordings with upload runs.
  - `GET /api/v1/leaderboard?scope=golden|uploads` segmented view plus persisted **"Highlight best by"** selector
    (`battle_score`, `qa_relevance`, `groundedness`, `context_precision`, `latency_ms`, `cost_usd`) highlighting winning
    and tied rows across `Leaderboard` and `Compare`.
- **Resilient live UI & catalog**:
  - App-wide `IngestSessionProvider` and `GET /api/v1/ingest/status` `active_jobs` keep live ingestion streams and
    `LevelIngestBanner` progress alive across route navigation and full browser reloads; clearly separated benchmark
    corpus (`loaded & ready to query`) vs private upload workspace sections on `/ingest`.
  - `<ModelPicker>` deduplicates pricing and model-availability reasons into a collapsed `"Pricing & availability"`
    disclosure (safe-mode masked); `gemini-4-carbon` (`Gemini 4 Carbon`) added to the model catalog.
  - Cloud Run cold-start hardening: GCP SDK client initialization and status checks run off the event loop under
    `_CLIENT_LOCK`, paired with bounded 30 s frontend bootstrap retry and single-flight `/ingest/status` coalescing.
  - Offline test suites expanded to **394 backend tests** (35 files) and **176 frontend tests** (17 suites), including
    IDOR/replay `404` matrices, prompt-injection/SQL scope isolation, and tracked-repo + `frontend/dist` leak scans.

## [1.0.0] — 2026-10-23

First public release (the internal "v3" rebuild: every stage measured, nothing simulated).

### Added
- **Five RAG architectures on one procurement corpus** (45 PDFs) and one 8-question golden set:
  Level 1 naive vector RAG (BigQuery `VECTOR_SEARCH`), Level 2 hybrid RRF + Vertex AI ranking API, Level 3 layout-aware
  multimodal RAG (Document AI Layout Parser + Gemini vision), Level 4 agentic RAG (Google ADK `LlmAgent` with
  retrieval, amendment, pricing and citation-check tools), Level 5 managed Vertex AI Search (managed summary or a
  selectable model over extractive segments).
- **Live, measured telemetry**: engine-emitted stage events over SSE with the real SQL / API requests, durations,
  tokens and cost; live architecture diagrams; resizable panes; screen-share safe mode.
- **Evaluation**: per-run synthesis and judge model picker (judge ≠ synthesis enforced server-side), Battle Score
  (40% QA relevance, 35% groundedness, 15% context precision, 10% efficiency vs 60 s / $0.20).
- **Compare** page (selected levels run concurrently on one question) and **Leaderboard** (BigQuery `query_runs` or
  recordings, one table per model pair, provenance per row); results persist across page navigation.
- **Zero-credential replay** of 79 recorded real runs.
- **Configurable Cloud Run deployment** (region, IAP or public access, optional public replay-only copy,
  least-privilege service accounts) with teardown script and Terraform parity.
- Offline test suites: 278 backend tests, 156 frontend tests.

### Known limitations
- Level 4 with Gemini 3.8 Flash has no recording for `q_sla_credit_calculation` (6/6 live attempts timed out).
- Ingestion jobs longer than 15 minutes must run locally (Cloud Run request timeout).
- `docker compose` (replay) is updated for the v1 image but not yet exercised in CI.
