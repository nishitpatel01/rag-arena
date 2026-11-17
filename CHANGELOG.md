# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [1.6.1](https://github.com/nishitpatel01/rag-arena/compare/v1.6.0...v1.6.1) (2026-11-17)


### Added

* **backend:** v3 StageEmitter contracts, timer-free SSE forwarder, record/replay, ingestion jobs ([51be8f5](https://github.com/nishitpatel01/rag-arena/commit/51be8f5ed685a99c2c230617e8ea718c1aa1a1fb))
* **compare:** real /compare page — one question, selected levels run concurrently, stacked rails, side-by-side answers, server-computed Battle Score scoreboard ([d87eadb](https://github.com/nishitpatel01/rag-arena/commit/d87eadb0443a195d56598b73aec583c31d103d15))
* **core:** add canonical PipelineSpec, validator, presets, and dynamic descriptor ([4333e0f](https://github.com/nishitpatel01/rag-arena/commit/4333e0f82ca7ea5e1be5315d7306ea78c97f55ea))
* **deploy:** configurable Cloud Run deployment (region, IAP|public, optional public replay copy, LIVE_INGEST), single SPA+API image, least-privilege SAs, teardown, Terraform parity, v3 compose ([6f742a4](https://github.com/nishitpatel01/rag-arena/commit/6f742a4b73a9a64cccf6b41a54deda3619842386))
* **docai:** idempotent rag-arena-layout-parser provisioning (REST, env-only ids) in script, bootstrap and Terraform ([43ba563](https://github.com/nishitpatel01/rag-arena/commit/43ba563e7daae71b1c42dff5ae7f11ffc442fe06))
* **engines:** implement ComposableRAGEngine, pipeline APIs, and query_runs provenance ([9a02632](https://github.com/nishitpatel01/rag-arena/commit/9a026321946aa2a4fb3108da6386b70829608217))
* **engines:** implement Level 6 Vertex AI RAG Engine and grounded mode ([d42509a](https://github.com/nishitpatel01/rag-arena/commit/d42509a265286d3a47011bc33a20e0232c52f3df))
* **eval:** Claude Opus 5.5 as LLM-as-a-judge via Vertex AI (distinct from synthesis model) ([68b3a56](https://github.com/nishitpatel01/rag-arena/commit/68b3a561c9cfbf57176fcd5d4a6ab07f2e408d81))
* **frontend:** BigQuery index-usage and rerank billing lines; diagram fits six nodes; refresh Level 2 screenshots ([349f90c](https://github.com/nishitpatel01/rag-arena/commit/349f90ca08f5b5c16609140a67028a5f394249f2))
* **frontend:** full diagram transcription from retrieved chunks; Level 3 README screenshot ([5a3058a](https://github.com/nishitpatel01/rag-arena/commit/5a3058ade2aa1b9c44fd78e33a44c9ef6e2c7e5a))
* **frontend:** Level 2 views — acronym expansion diff, hybrid RRF candidates, rerank before/after, Level 1 compare teaser ([d1d530b](https://github.com/nishitpatel01/rag-arena/commit/d1d530b4245cb9fe2e3287175b7a597d18833e93))
* **frontend:** Level 3 views — element/layout/bbox-source badges, authority headers, element mix, diagram transcription drawer, parse summary ([9c44716](https://github.com/nishitpatel01/rag-arena/commit/9c447162910327603b46292f7628cdcec490f155))
* **frontend:** Level 4 live agent timeline — model/tool rows in arrival order, pricing expressions, citation-repair diff, tool nodes light up while calls are open ([500749c](https://github.com/nishitpatel01/rag-arena/commit/500749c36bf78b8e0faa3bcb7af33791dbb09f55))
* **frontend:** Level 5 Vertex AI Search views (results + extractive segments, summary vs model modes, managed ingest), 21-level5.png ([1e7f8a3](https://github.com/nishitpatel01/rag-arena/commit/1e7f8a365a03faf8bb9fad4550179e8e7f3d7dae))
* **frontend:** level pages show the newest run that included them (Compare or own), with question/models, banner, and Clear ([dc098ed](https://github.com/nishitpatel01/rag-arena/commit/dc098edad6944e77224a8f5afabe3fa245a62407))
* **frontend:** live Level 1 polish — verbatim index states, ingest run provenance table, markdown answers ([69ed3d5](https://github.com/nishitpatel01/rag-arena/commit/69ed3d56638fc0f159b79f46c8df6292a742cfe8))
* **frontend:** per-page run sessions survive navigation (runs keep streaming), Reset / Stop & reset on level and Compare pages ([049d7a6](https://github.com/nishitpatel01/rag-arena/commit/049d7a6c359eca38f3566a3226c2a3f0773ec774))
* **frontend:** per-run synthesis and judge model picker ([692f574](https://github.com/nishitpatel01/rag-arena/commit/692f5742b73b2975ef96cea74875aa351d86e86d))
* **frontend:** polish Level 4 diagram/timeline for real recordings and add 20-level4.png ([7643511](https://github.com/nishitpatel01/rag-arena/commit/7643511df78cf71e356fb481d57def3a1b4fe8d3))
* **frontend:** resizable Document viewer and Node details panes (drag/keyboard, persisted, wider defaults) ([d5f0168](https://github.com/nishitpatel01/rag-arena/commit/d5f0168855cbe188576071f6aed532e35b2fa968))
* **frontend:** resizable Question pane; side panes share space so the middle column keeps &gt;= 480 px ([1d17618](https://github.com/nishitpatel01/rag-arena/commit/1d17618840f648af415a17da718591b2be1a25c9))
* **frontend:** screen-share safe mode (mask project id, bucket, project number); fix per-document ingest progress rows ([32c2074](https://github.com/nishitpatel01/rag-arena/commit/32c2074812e74baa03e8eccb9e40456d0108f295))
* **frontend:** show judge rationale, judge tokens/cost and run cost (engine + judge) ([e8b4369](https://github.com/nishitpatel01/rag-arena/commit/e8b4369c2f7655e342d46fddc375fb93bd42dd39))
* **frontend:** v3 full-width shell, light/dark/system theme, stage rail and live architecture diagram ([8478425](https://github.com/nishitpatel01/rag-arena/commit/847842582926deaee3c32b49b65c7cc16a0666f7))
* **ingest:** add Vertex AI RAG Engine corpus and isolated upload ingestion ([cd36717](https://github.com/nishitpatel01/rag-arena/commit/cd36717b1fe56a01eb852978b195ae8ebfb9bf09))
* **ingest:** Level 3 multimodal ingestion — Document AI Layout Parser v1.5 elements, Gemini vision diagram transcription, element-aware chunks, multimodal embeddings ([d1b694b](https://github.com/nishitpatel01/rag-arena/commit/d1b694ba28b0393334535eae5c63e62a0ffa33fe))
* **ingest:** live Vertex embedder and BigQuery loader for ingestion jobs ([be2a845](https://github.com/nishitpatel01/rag-arena/commit/be2a8457da8dbb4c75bd7b0dc333ecd1e1434ad8))
* **leaderboard:** /leaderboard from query_runs (live) or recordings (replay), per-pair tables with provenance; delete hand-typed precomputed_runs.json ([5678375](https://github.com/nishitpatel01/rag-arena/commit/56783754b47abfb37fc826600222cd877a87d13a))
* **level1:** query embedding format, query_runs provenance per live run ([1adb425](https://github.com/nishitpatel01/rag-arena/commit/1adb4253142b8ddba205c0b2423e2d67e5b5b8c8))
* **level2:** Level 2 hybrid engine with measured query_prep, embed, RRF retrieve, rerank, synthesize stages ([2b80d43](https://github.com/nishitpatel01/rag-arena/commit/2b80d4346d62aca9166ca82f0e334b6f027e74cd))
* **level3:** Level 3 engine — element intent, multimodal RRF retrieve, rerank, authority-header synthesis (latest amendment first) ([7c80822](https://github.com/nishitpatel01/rag-arena/commit/7c80822f9e755a4752f428eb8e4bfa261fda00cc))
* **level4:** agent tools on real data — corpus search (Level 3 retrieve+rerank), BigQuery amendment lookup, AST-safe pricing calculator, citation verifier ([2e651b9](https://github.com/nishitpatel01/rag-arena/commit/2e651b983b7688931e756b164bec9d73d7fd752d))
* **level4:** real Google ADK agent (LlmAgent + InMemoryRunner) with live agent_turn telemetry, bounded turns/tool calls/timeouts, one citation-repair turn ([ba6cc60](https://github.com/nishitpatel01/rag-arena/commit/ba6cc609921b97dae0efd720d07e4b097ad79d49))
* **level5:** Managed Vertex AI Search — provisioning, GCS sync + managed import job, search/summary engine (two synthesis modes), 16 recordings; battle route builds only requested engines ([5994be6](https://github.com/nishitpatel01/rag-arena/commit/5994be65e60195ba9ff31b137418bdc504c18dc0))
* **models:** verified model catalog with per-run synthesis and judge selection ([b903440](https://github.com/nishitpatel01/rag-arena/commit/b903440cd836abcce4b3f9cfc7597c79c0df4da4))
* **rag-engine:** add safe RagManagedDb provisioning and Level 6 config ([053c20a](https://github.com/nishitpatel01/rag-arena/commit/053c20a19fead6391022a38ecccce32fcc8bb879))
* **recording:** redact project, bucket, credential and home paths before writing recordings ([a0a2e84](https://github.com/nishitpatel01/rag-arena/commit/a0a2e84af599b2ca132f01a543173a8b20fb6819))
* **releases:** add /api/v1/releases manifest compiler, drift CLI, and container packaging ([383a9d5](https://github.com/nishitpatel01/rag-arena/commit/383a9d5dbab2ecce78739d0e997ae59190a64aa4))
* **rerank:** Discovery Engine Ranking API REST client with bounds, pricing per rank count ([c3745cc](https://github.com/nishitpatel01/rag-arena/commit/c3745cc98e82f22fe41a2bfb8bd103f09c51fa28))
* **retrieve:** hybrid VECTOR_SEARCH + SEARCH() RRF SQL with per-row ranks; CUAD docs labelled COMMERCIAL_AGREEMENT ([d5a5b7c](https://github.com/nishitpatel01/rag-arena/commit/d5a5b7c32dfef74c89083231b34cbd8cef4d785a))
* **scann:** promote Level 7 AlloyDB ScaNN & Level 8 Vector Search 2.0 engines, UI, and ephemeral scripts ([db9f869](https://github.com/nishitpatel01/rag-arena/commit/db9f8694415fc7c9960b6431baa8b92db6fe761e))
* **v1.2:** add Level 6 UI, Cloud Run parity, 24 recordings, and v1.2.0 docs ([6d1bfd1](https://github.com/nishitpatel01/rag-arena/commit/6d1bfd19d0019c53f9f691a1c1d93d04b17ff9f4))
* **v1.3:** add Controlled Ablation Lab, Retrieval Diff Inspector, and v1.3.0 docs ([64776d1](https://github.com/nishitpatel01/rag-arena/commit/64776d138c4e33e42ad89fbf23cdd56e9eb4c7dd))
* **v1.4:** add 32 live ScaNN golden recordings and v1.4.0 release documentation ([62b0d6f](https://github.com/nishitpatel01/rag-arena/commit/62b0d6fd728a974f96e07890a7ee56da41d94da0))
* **v1.5:** add in-app /releases documentation hub and release-please CI workflow ([37db489](https://github.com/nishitpatel01/rag-arena/commit/37db4895c1c5794014642bfeef2812b7d93aac7b))
* **viewer:** citation drawer with real PDF page render and text-match highlights ([52a5697](https://github.com/nishitpatel01/rag-arena/commit/52a5697d3d0a6db4eb301fab00d76ba0d39543cd))
* **workbench:** add interactive /workbench UI synced with live ArchitectureDiagram ([fae8363](https://github.com/nishitpatel01/rag-arena/commit/fae8363a3f330522856b7cf05d2c1dfc5a2f68c9))


### Changed

* **ingest:** skip 60s index poll on upload workspace ingestion (v1.1.1) ([8e4a188](https://github.com/nishitpatel01/rag-arena/commit/8e4a18873722f0a8c739d191d28f9be35d883287))
* **ingest:** skip 60s index poll on upload workspace ingestion (v1.1.1) ([b297158](https://github.com/nishitpatel01/rag-arena/commit/b2971587a6244e887b830113337a6ce83c620cca))


### Fixed

* **build:** frontend lockfile resolves from the public npm registry (was an internal proxy host); publish.json forbids internal proxy hostnames ([d1a873e](https://github.com/nishitpatel01/rag-arena/commit/d1a873eb1dda7d87f797d2787f9969c5f69a4e60))
* **corpus:** draw diagram titles/labels in dark ink so rendered diagrams are readable ([a3298ef](https://github.com/nishitpatel01/rag-arena/commit/a3298ef39502b8e0441cecad817e04efbabc06fe))
* **frontend:** fail loud when a run or ingest stream ends without a terminal event ([5f5e665](https://github.com/nishitpatel01/rag-arena/commit/5f5e6657feea173d8ce200aa0a68cdf955665b0e))
* honest local-corpus ingest node, env-driven CORS, v3 table in env template; Phase 1 D5/D7 evidence ([1ad0843](https://github.com/nishitpatel01/rag-arena/commit/1ad084359f07913ebd89ffc3cb139fd492b639d1))
* **ingest:** accurate non-live ingest status message (no stale 'Phase 1' wording) ([4ad7e8b](https://github.com/nishitpatel01/rag-arena/commit/4ad7e8b66bb8907d0820d8f827a150d6ef9cf645))
* **ingest:** require PyMuPDF in live mode, fail on empty docs, extract unclipped text ([0300986](https://github.com/nishitpatel01/rag-arena/commit/0300986b05204e617e68373e9717c3e2d2b5af23))
* **leaderboard:** never aggregate synthetic dev fixture recordings ([9dcc826](https://github.com/nishitpatel01/rag-arena/commit/9dcc82605b9aa8899cff529505f30b331d5ec19c))
* **level4,deploy:** thinking config via ADK BuiltInPlanner; image installs a public-PyPI lock (google-adk 2.12.0) instead of unpinned ranges that resolved to adk 1.14.1 ([aa4d20a](https://github.com/nishitpatel01/rag-arena/commit/aa4d20ae93d669168ecfde91e6aa9e74d29efffa))
* **replay:** keep Level 3 citation layout_source/bbox_source when decoding recordings ([3735c1c](https://github.com/nishitpatel01/rag-arena/commit/3735c1c7d7261928fcf4befb194c96151c10c11d))
* **scoring:** efficiency references 60 s / $0.20 per run so speed and cost separate real runs (owner decision) ([108b98d](https://github.com/nishitpatel01/rag-arena/commit/108b98d5f2253f558203bc851404012ae4dfcc0a))


### Documentation

* add Level 4 (Google ADK agentic RAG) architecture, results, and D1-D6 evidence ([9099fa2](https://github.com/nishitpatel01/rag-arena/commit/9099fa20c739c640ec6ba37ab53c168000cf5370))
* Cloud Run deployment (configurable), Phase 6 progress ([7b8aae7](https://github.com/nishitpatel01/rag-arena/commit/7b8aae7c519b9e0416bac3df0de3eef0b8feb6f4))
* Level 2 live — architecture, walkthrough with screenshots, Level 1 vs Level 2 results, Ranking API pricing ([d946e32](https://github.com/nishitpatel01/rag-arena/commit/d946e321c308b36ec27611d84355d4351bf12eb2))
* Level 3 live — compact README (one screenshot per level, averages table), detail moved to ARCHITECTURE/SETUP ([8efa35a](https://github.com/nishitpatel01/rag-arena/commit/8efa35aa14af1e4a894ef157ec8aa1aa9d10a45c))
* Level 5 (Managed Vertex AI Search) architecture, results and D1-D6 evidence ([5d09a95](https://github.com/nishitpatel01/rag-arena/commit/5d09a955114b46f776051328292cf8f5b466cd56))
* **phase1:** D1/D2/D3/D5/D6 evidence; golden set is 8 questions ([d680267](https://github.com/nishitpatel01/rag-arena/commit/d680267fef3abb662a0f55584f747d54905e9c9c))
* **phase1:** D4 UI evidence ([d790d4f](https://github.com/nishitpatel01/rag-arena/commit/d790d4f62f151f81405819a38a59ca4bbbea6f52))
* **phase1:** record owner feedback items (judge model, viewer, model picker) ([423acd4](https://github.com/nishitpatel01/rag-arena/commit/423acd4aed766d1c9b2d9545be34cc2ab1796887))
* **phase2:** D1-D6 evidence for Level 2 ([7bcf3fc](https://github.com/nishitpatel01/rag-arena/commit/7bcf3fcbc4a3da031de4542c413882f38d7aa55e))
* **phase3:** D1-D6 evidence for Level 3 ([b653bcb](https://github.com/nishitpatel01/rag-arena/commit/b653bcb81d13a04c27da0ef993e7649e0a6e972f))
* README Compare/Leaderboard + compact layout; Phase 6 decisions (history A, parked engines kept) ([f85b850](https://github.com/nishitpatel01/rag-arena/commit/f85b850335e7a3a54bf36906a2f3c55a654e1b31))
* README opens with problem + solution, GCP products per level, q… ([071c182](https://github.com/nishitpatel01/rag-arena/commit/071c1829d21ef214d423625157079e5d1e714cf4))
* README opens with problem + solution, GCP products per level, quality & tests, clean layout tree, MIT LICENSE; principles table moved to ARCHITECTURE §0 ([7219188](https://github.com/nishitpatel01/rag-arena/commit/7219188a89c97b0a96f82bea891fbebf75f3a48f))
* README screenshots for Compare and Leaderboard ([b06a189](https://github.com/nishitpatel01/rag-arena/commit/b06a18957b128b214ed8a0cc0838841f3dcb4dcb))
* README screenshots for Compare and Leaderboard ([7c32f58](https://github.com/nishitpatel01/rag-arena/commit/7c32f58e43051646a0404d8fbbec5c4a3ffd6606))
* **readme:** one screenshot per architecture; condense stage walkthrough into a table; drop unused screenshots ([fef80a9](https://github.com/nishitpatel01/rag-arena/commit/fef80a9bfb6630705b1ae2c3a70d0d8ebc184bed))
* **screenshots:** 14 live UI screenshots (safe mode, guard-verified) for each stage, architecture, viewer, ingest, leaderboard ([0deb6cb](https://github.com/nishitpatel01/rag-arena/commit/0deb6cbaf825748de9e1aa36302f2bd1d1ec7c80))
* **screenshots:** Level 2 retrieve candidates, rerank before/after, query prep, architecture (replay, guard-verified) ([25e8553](https://github.com/nishitpatel01/rag-arena/commit/25e85535aecf88d9a4e00ee6777e451c2bbeca52))
* **task:** Cloud Run gate passed (owner live run + ingestion on deployed UI) ([0758991](https://github.com/nishitpatel01/rag-arena/commit/07589912fad7c12310ccd1daf13710f8d2656fd3))
* **task:** Phase 6 compare, UI session and leaderboard evidence ([2decd23](https://github.com/nishitpatel01/rag-arena/commit/2decd23a3aeb0333df3d245d45d1c0695bf5b53e))
* v3 README with architecture, stage-by-stage walkthrough, model catalog, Level 1 results; refresh architecture and setup guides ([d775bd0](https://github.com/nishitpatel01/rag-arena/commit/d775bd0600bf50477538b091374f5736607c6692))
* **v3:** Cloud Run deployment plan (IAP, least-privilege SA, env-only config) in Phase 6 ([b63953c](https://github.com/nishitpatel01/rag-arena/commit/b63953c951a80c99688cffcf9dff2894f7d4efa2))
* **v3:** spec, task tracker, v2 authenticity audit, env-only configuration ([f72d3b1](https://github.com/nishitpatel01/rag-arena/commit/f72d3b158a14b3d2f4e45bc40a7bd560427d6f66))

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
