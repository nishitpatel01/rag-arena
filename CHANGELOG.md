# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

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
