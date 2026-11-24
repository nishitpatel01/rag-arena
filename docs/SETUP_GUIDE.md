# The RAG Arena — Setup Guide

Two ways to run it:

1. **Zero-credential replay** (`DEMO_MODE=replay`, the default): plays the committed recordings of real Level 1–10
   runs (`199` baseline recordings + `10` isolated ACL refusal demos across 7 standing architectures + 3 ephemeral DB/ANN benchmarks). No Google Cloud account, no
   credentials, no network calls to GCP.
2. **Live on your own GCP project** (`DEMO_MODE=live` or `record`): real Vertex AI (including Vertex AI RAG Engine),
   BigQuery (including BigQuery Property Graph ISO GQL), Document AI, Cloud Storage, and Vertex AI Search calls, benchmark and private-PDF ingestion from the UI,
   and (in `record`) new golden-set recordings.

**All ten levels** are enabled by default in `DEMO_MODE=replay` (`DEFAULT_ENABLED_LEVELS`), while `DEMO_MODE=live` /
`record` defaults to the **seven standing \$0/mo-idle levels** (`DEFAULT_LIVE_LEVELS=managed_vertex,rag_engine,level1_naive,level2_hybrid,level3_multimodal,bigquery_graphrag,level4_agentic`)
so standing deployments never require an active Cloud SQL, AlloyDB, or Vector Search 2.0 cluster. Override
`ENABLED_LEVELS` in `.env` or use the one-command ephemeral scripts (§3.7) to benchmark Levels 5–7 live. The
**Composable RAG Workbench** (`/workbench` with ADK unlocked across all 9 stores and dense-only ablation), private user PDF uploads (`v1.1`–`v1.2`), `/compare` (Preset Ladder + 2–4
recipe Controlled Ablation Lab with Retrieval Diff Inspector), `/leaderboard` (with winner highlighting), and the
configurable Cloud Run deployment (including restricted-org VPC-SC & pre-provisioned SA toggles) are all live. Level 1 needs `VAIS_*` keys, Level 2 needs `RAG_ENGINE_CORPUS_ID`, and Level 9 needs `scripts/provision_bq_graph.py --apply` (`$0/mo` standing) in live mode.

---

## 1. Prerequisites

| Tool | Version | Needed for |
| :-- | :-- | :-- |
| Python | 3.11+ (the container image uses 3.12) | backend |
| Node.js + npm | 20+ | frontend |
| Google Cloud CLI (`gcloud`, `bq`) | current | live mode & Cloud Run deploy |
| Terraform | ≥ 1.5 | optional (`infra/main.tf`) |

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
(cd frontend && npm install)
```

---

## 2. Replay mode (no credentials)

```bash
# terminal 1 — API on http://localhost:8000
DEMO_MODE=replay .venv/bin/python -m uvicorn app.main:app --app-dir backend --port 8000

# terminal 2 — UI on http://localhost:5173 (Vite proxies /api → :8000)
cd frontend && npm run dev
```

- Open **Level 1**, choose one of the 8 golden questions, and a recorded model pair: synthesis
  `gemini-4-argon` or `gemini-3.8-flash`, judge `claude-opus-5-5` (or `vertex-ai-search-summary` on Level 5,
  `vertex-rag-engine-grounded` on Level 6).
  `GET /api/v1/models` lists the playable pairs under `recorded_pairs`.
- Stage events replay with their recorded relative timing; speed it up with `REPLAY_SPEED=4.0` (range `(0, 100]`).
- An ad-hoc question returns `engine_error: No recording for this question in replay mode` — replay never
  improvises an answer, and all `/api/v1/uploads/*` routes return `404`.
- Single-container replay via Docker: `docker compose up --build` builds the root multi-stage `Dockerfile` (SPA +
  Uvicorn API) and serves replay mode on `http://localhost:8080`.
- Smoke test without the UI:
  ```bash
  curl -sN -X POST localhost:8000/api/v1/battle/stream -H 'content-type: application/json' \
    -d '{"query":"replay","question_id":"q_exact_clin_lookup","engines":["level1_naive"],
         "synthesis_model":"gemini-3.8-flash","judge_model":"claude-opus-5-5"}'
  ```
  Expected events: `run_started`, five `stage_started` / `stage_completed` pairs, `answer_complete`,
  `eval_complete`, `battle_complete`.

---

## 3. Live mode on your own GCP project

### 3.1 Configure `.env`

```bash
cp .env.example .env    # .env is git-ignored; never commit real identifiers
```

| Key | Example | Notes |
| :-- | :-- | :-- |
| `DEMO_MODE` | `live` | `record` = `live` + writes golden runs to `data/recordings/**` |
| `GCP_PROJECT_ID` | `your-gcp-project-id` | required in `live` / `record` |
| `GCP_LOCATION` | `us-central1` | BigQuery and upload bucket location |
| `VERTEX_GENAI_LOCATION` | `global` | google-genai client location (embeddings, Gemini) |
| `GCS_CORPUS_BUCKET` | `your-gcp-project-id-rag-arena-corpus` | defaults to `<project>-rag-arena-corpus` |
| `UPLOAD_BUCKET_MODE` | `auto` | `auto` creates/verifies the private upload bucket; `existing` never creates |
| `UPLOAD_BUCKET` | `your-gcp-project-id-rag-arena-uploads` | defaults to `<project>-rag-arena-uploads`; must differ from `GCS_CORPUS_BUCKET` |
| `UPLOAD_BUCKET_KMS_KEY` | empty | optional CMEK resource name when creating `UPLOAD_BUCKET` |
| `ACCESS_MODE` / `IAP_JWT_AUDIENCE` / `LIVE_INGEST` | `public` locally (`iap` on Cloud Run) / `/projects/<NUM>/locations/<REGION>/services/<SERVICE>` / `true` | ES256 `X-Goog-IAP-JWT-Assertion` verification in `iap` mode; `HttpOnly` session cookie in `public` mode |
| `ALLOWED_UPLOAD_ORIGINS` / `MAX_UPLOAD_FILE_BYTES` / `MAX_UPLOAD_PDF_PAGES` / `MAX_UPLOAD_VISION_PAGES` | empty / `536870912` (`512 MiB`) / `500` / `100` | canonical upload `Origin` allowlist and configurable Cloud Run `/tmp` / PDF / Vision safety bounds |
| `BQ_DATASET` | `rag_arena` | |
| `BQ_TABLE` | `document_chunks_v3` | live ingestion refuses `document_chunks` (the frozen v2 table) |
| `GEMINI_FAST_MODEL` / `GEMINI_THINKING_LEVEL` | `gemini-4-argon` / `high` | default synthesis model and Gemini thinking level |
| `JUDGE_PROVIDER` / `JUDGE_MODEL` / `JUDGE_LOCATION` | `anthropic_vertex` / `claude-opus-5-5` / `global` | default judge; must differ from the synthesis model |
| `TEXT_EMBEDDING_MODEL` | `gemini-embedding-2` | query and document embeddings |
| `QUERY_TIMEOUT_SECONDS` / `INGEST_JOB_TIMEOUT_SECONDS` | `120` / `1800` | explicit bounds (`120` recommended for Level 4 multi-turn ADK traces) |
| `AGENT_MAX_TURNS` / `AGENT_MAX_TOOL_CALLS_PER_TURN` / `AGENT_TURN_TIMEOUT_SECONDS` / `AGENT_THINKING_LEVEL` | `8` / `8` / `90` / `low` | Level 4 ADK runner bounds and Gemini thinking level |
| `CLOUDSDK_CONFIG` / `GOOGLE_APPLICATION_CREDENTIALS` | empty | optional isolated gcloud profile / ADC file |
| `MODEL_LOCATION_<ID>` | `MODEL_LOCATION_CLAUDE_OPUS_5_5=global` | optional per-model endpoint override |
| `DOCAI_LOCATION` / `DOCAI_PROCESSOR_ID` / `DOCAI_PROCESSOR_VERSION` | `us` / short id / `pretrained-layout-parser-v1.5-2025-08-25` | Level 3 Layout Parser processor printed by `scripts/provision_documentai.py` |
| `DOCAI_TIMEOUT_SECONDS` / `VISION_MODEL` | `120` / `gemini-4-argon` | Layout Parser request bound; Gemini model that transcribes diagram pages |
| `VAIS_LOCATION` / `VAIS_DATA_STORE_ID` / `VAIS_ENGINE_ID` | `global` / `rag-arena-procurement` / `rag-arena-procurement-search` | Level 5 corpus Vertex AI Search short ids printed by `scripts/provision_vertex_search.py` |
| `VAIS_UPLOADS_DATA_STORE_ID` / `VAIS_UPLOADS_ENGINE_ID` | `rag-arena-uploads` / `rag-arena-uploads-search` | Level 5 private uploads data store + search app short ids (must differ from corpus ids) |
| `RAG_ENGINE_LOCATION` / `RAG_ENGINE_CORPUS_ID` | `us-central1` / short corpus id | Level 6 regional Vertex AI RAG Engine location (never `global`) + corpus id printed by `scripts/provision_rag_engine.py` |
| `RAG_ENGINE_EMBEDDING_MODEL` / `RAG_ENGINE_TOP_K` / `RAG_ENGINE_CHUNK_SIZE` / `RAG_ENGINE_CHUNK_OVERLAP` / `RAG_ENGINE_TIMEOUT_SECONDS` | `text-embedding-005` / `10` / `512` / `100` / `30` | Level 6 `RagCorpus` publisher embedding model, `:retrieveContexts` top-k, chunking spec, and HTTP bound |
| `CLOUDSQL_DSN` / `CLOUDSQL_TABLE` / `CLOUDSQL_CANDIDATE_K` / `CLOUDSQL_TIMEOUT_SECONDS` | empty / `document_chunks` / `25` / `30` | Level 5 ephemeral Cloud SQL PostgreSQL 16 + `pgvector 0.8.5` (`halfvec(3072)` HNSW) loopback URI (`127.0.0.1` via `cloud-sql-proxy`) |
| `ALLOYDB_DSN` / `ALLOYDB_TABLE` / `ALLOYDB_CANDIDATE_K` / `ALLOYDB_TIMEOUT_SECONDS` | empty / `procurement_chunks_scann` / `25` / `30` | Level 6 ephemeral AlloyDB for PostgreSQL + ScaNN connection URI (loopback `127.0.0.1` via `alloydb-auth-proxy` or `--omni`), table identifier, candidate top-k, and timeout |
| `VVS2_LOCATION` / `VVS2_COLLECTION` / `VVS2_INDEX_ID` / `VVS2_TOP_K` / `VVS2_TIMEOUT_SECONDS` | `us-central1` / empty / `dense-scann` / `25` / `30` | Level 7 ephemeral Vertex AI Vector Search 2.0 (`vectorsearch.googleapis.com/v1beta`) region, short RFC1035 collection id, `denseScann` index id, candidate top-k, and HTTP bound |
| `BQ_GRAPH_NAME` | `procurement_graph` | Level 9 standing `$0/mo` BigQuery Property Graph provisioned by `scripts/provision_bq_graph.py --apply` (ISO GQL `GRAPH_TABLE`) |

### 3.2 Authenticate

```bash
gcloud auth login
gcloud auth application-default login
```

The backend uses Application Default Credentials only — no key files are required or committed.

IAM for the principal running the backend locally: `roles/aiplatform.user`, `roles/bigquery.jobUser`,
`roles/bigquery.dataEditor` on the `rag_arena` dataset, `roles/documentai.apiUser`, `roles/discoveryengine.editor`,
and `roles/storage.admin` (to auto-create and privacy-check `UPLOAD_BUCKET`, or `roles/storage.objectUser` +
`roles/storage.legacyBucketReader` on a pre-created private `UPLOAD_BUCKET` with `UPLOAD_BUCKET_MODE=existing`).

### 3.3 Bootstrap the project

`scripts/bootstrap_gcp.sh` reads **environment variables** (it does not parse `.env`). Run it with
`BQ_TABLE=document_chunks` so it does not pre-create `document_chunks_v3` with the legacy v2 schema — the Level 1
ingestion job creates `document_chunks_v3` with the v3 schema automatically:

```bash
set -a; source .env; set +a
BQ_TABLE=document_chunks ./scripts/bootstrap_gcp.sh
```

What it does:
- enables `aiplatform`, `bigquery`, `storage`, `discoveryengine`, `documentai`, `alloydb` APIs;
- creates `gs://$GCS_CORPUS_BUCKET` with uniform bucket-level access and enforced public access prevention, and uploads
  `data/sample_docs/` to `sample_docs/`;
- creates the `rag_arena` BigQuery dataset;
- ensures the Level 3 Document AI processor `rag-arena-layout-parser` (`LAYOUT_PARSER_PROCESSOR`) via
  `scripts/provision_documentai.py` (idempotent; prints the short processor id for `DOCAI_PROCESSOR_ID` in `.env`).

### 3.4 Verify model access

```bash
PYTHONPATH=backend .venv/bin/python scripts/verify_models.py
```

One bounded live call per LLM catalog model (60 s timeout, no retries) → `data/model_verification.json`
(model ids, `ok` flag, HTTP status, redacted message, timestamp). Models without an `ok` entry stay disabled in
`<ModelPicker>` with their exact reason shown inside the `"Pricing & availability"` disclosure. Re-run this script on
your own project (for example when `gemini-4-carbon` is enabled on your project or after granting quota).

Claude models are Anthropic partner models on Vertex AI (`global` endpoint, `:rawPredict`). **Claude Fable 5.1**
additionally requires Anthropic data sharing to be enabled on your project:

```bash
curl -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  "https://aiplatform.googleapis.com/v1beta1/projects/your-gcp-project-id/locations/global/publishers/anthropic/models/claude-fable-5-1:setPublisherModelConfig" \
  -d '{"publisherModelConfig":{"dataSharingEnabledProvider":"anthropic"}}'
```

Then re-run `verify_models.py`.

### 3.5 Run, ingest, query (benchmark corpus)

```bash
.venv/bin/python -m uvicorn app.main:app --app-dir backend --port 8000   # DEMO_MODE comes from .env
cd frontend && npm run dev
```

1. Open `http://localhost:5173/ingest`. Under **Pre-loaded benchmark corpus (45 procurement PDFs)**, run **Level 1**
   (`discover_docs → parse → chunk → embed → load_bigquery → build_indexes → verify_counts`; 526 pages → 5,067
   `naive` chunks × 3072-d).
2. Run **Level 2** (`hybrid` tier: 526 contextual-lineage chunks + vector/search indexes). Its `rerank` stage calls
   Discovery Engine Ranking API (`semantic-ranker-default@latest`, no fallback ranker).
3. With `DOCAI_*` set, run **Level 3** (`multimodal` tier: Layout Parser online in ≤ 15-page splits at $10 / 1,000
   pages + Gemini vision on diagram pages, no PyMuPDF fallback).
4. **Level 4 (`level4_agentic`)** reuses the Level 3 `multimodal` tier (no separate ingestion job) and requires a
   verified Gemini synthesis model (`gemini-4-argon`, `gemini-4-carbon`, or `gemini-3.8-flash`).
5. **Level 5 (`managed_vertex`)**: run `PYTHONPATH=backend .venv/bin/python scripts/provision_vertex_search.py` once
   (idempotently creates both the corpus store/app `rag-arena-procurement*` and the filterable uploads store/app
   `rag-arena-uploads*`), set the `VAIS_*` keys in `.env`, and run Level 5 ingestion from `/ingest`.
6. **Level 6 (`rag_engine`)**: run `PYTHONPATH=backend .venv/bin/python scripts/provision_rag_engine.py` once
   (reuses any active regional `ragEngineConfig` untouched at \$0 incremental Spanner compute, defaults uninitialized
   projects to `RagManagedDb` **Serverless** mode, and never sets `unprovisioned`), set `RAG_ENGINE_LOCATION` and
   `RAG_ENGINE_CORPUS_ID` in `.env`, and run Level 6 ingestion from `/ingest` (imports the 45 GCS PDFs in batches of
   `<=25` URIs with `chunk_size=512, overlap=100`).
7. To capture new golden-set recordings, run with `DEMO_MODE=record`; files land in
   `data/recordings/<engine>/<synthesis>__<judge>/<question_id>.json` with identity values redacted (`upl_*` upload
   runs never overwrite golden recordings).

### 3.6 Uploading your own PDFs (v1.1–v1.2)

1. Open `/ingest` in `DEMO_MODE=live` or `record`. Verify the **Upload bucket** card reports `private ✓ · ok`, or click
   **Create / verify bucket** (if permissions or org policies block creation, the card shows the exact missing
   permission/constraint and a copy-paste `gcloud` command).
2. Click **New workspace** (`upl_<12hex>`) and drop or select one or more PDFs. The browser uploads straight to Cloud
   Storage via Origin-bound resumable session URIs (`256 KiB` chunks with automatic `308` resume, bypassing Cloud Run's
   32 MiB HTTP/1 body cap, with no app-imposed size, page, or file-count limits). `/finalize` validates each file in
   constant memory (`O(1)` heap) and rejects non-PDFs, encrypted PDFs, JavaScript, `/Launch` actions, and embedded files.
3. Click **Ingest** for **Level 1**, **Level 2**, **Level 3** (displays a pre-flight Document AI + Gemini vision max
   cost estimate requiring confirmation before any paid call starts; Levels 1–3 inspect existing BigQuery indexes
   without waiting out the 60 s DDL poll), **Level 5** (zero-download JSONL import into `rag-arena-uploads`), and/or
   **Level 6** (provisions an isolated per-workspace `rag-arena-upl_<12hex>` `RagCorpus` and imports GCS URIs with zero
   local PDF downloads). **Level 4** unlocks automatically once Level 3 is ingested for that workspace. Live ingestion
   progress stays visible across route changes and full page reloads.
4. On any Level page or **`/compare`**, select your `upl_<12hex>` workspace in the **Scope** dropdown and ask any
   question. Upload runs use reference-free QA relevance judging (`no reference answer` badge) and are tracked under
   **`Scope: Uploads`** on **`/leaderboard`**, with **Highlight best by** available on both Compare and Leaderboard.
5. Uploads persist until you click **Delete workspace**, which synchronously deletes the workspace's BigQuery rows,
   Vertex AI Search documents, isolated Vertex AI RAG Engine `RagCorpus`, and Cloud Storage objects while leaving the
   45-PDF benchmark corpus and any neighbor `RagCorpus` resources untouched.

### 3.7 Standing BigQuery Property Graph (Level 9) & Ephemeral DB/ANN Benchmarks (Levels 5–7 — \$0/mo standing idle)

- **Level 9 (`bigquery_graphrag`, standing \$0/mo)**: derive the 5 lightweight node/edge tables (`<50 KiB`) and `CREATE OR REPLACE PROPERTY GRAPH procurement_graph` from `document_chunks_v3` (`tier_profile='multimodal'`):
  ```bash
  PYTHONPATH=backend .venv/bin/python scripts/provision_bq_graph.py --apply
  ```
- **Levels 5 (`cloudsql_pgvector`), 6 (`alloydb_scann`), and 7 (`vertex_vector_v2`)** play out of the box in `DEMO_MODE=replay` from committed golden recordings. To re-run or re-record them live without leaving standing database or dedicated ANN replica charges:

```bash
# Level 5 — Cloud SQL PostgreSQL 16 + pgvector 0.8.5 (halfvec 3072-d HNSW + GIN FTS + Postgres RLS) → record → teardown
./scripts/ephemeral_cloudsql.sh --full --synthesis-models gemini-4-neon --judge-model claude-opus-5-5 --yes

# Level 6a — Cloud AlloyDB ZONAL 2-vCPU + loopback alloydb-auth-proxy → export 526 BQ hybrid vectors → ScaNN + GIN → record → teardown
./scripts/ephemeral_alloydb.sh --full --write-query-runs --yes

# Level 6b — Local AlloyDB Omni Docker (127.0.0.1 loopback, $0 cloud DB compute) → ScaNN + GIN → record → container rm
./scripts/ephemeral_alloydb.sh --omni --full --yes

# Level 7 — Vertex AI Vector Search 2.0 Collection + dense-scann index → 526 DataObjects → record → index & collection delete
./scripts/ephemeral_vvs2.sh --full --write-query-runs --yes
```

All three scripts export the existing `3072-d` BigQuery `hybrid` chunks (`0` re-embedding API calls), build vector indexes strictly *after* bulk-loading all rows, write redacted recordings via `scripts/record_scann_benchmarks.py`, and install a bash `trap ... EXIT` that tears down all `rag-arena-ephem-*` resources and verifies `0` remain.

---

## 4. Tests

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests -q   # 525 passed across 56 files (offline; no GCP calls)
cd frontend && npm test && npm run build                # 241 passed across 30 suites + tsc/vite build
```

`backend/tests/test_publish_manifest.py` scans `README.md`, `docs/ARCHITECTURE.md`, `docs/SETUP_GUIDE.md`,
`.env.example`, all tracked repository files, and `frontend/dist` for internal terms, `.env` identity values, and
concrete Cloud Run URLs.

---

## 5. Troubleshooting

| Symptom | Cause / fix |
| :-- | :-- |
| Startup error `GCP_PROJECT_ID must be set … when DEMO_MODE=live\|record` | Set it in `.env` or the environment, or use `DEMO_MODE=replay`. |
| Ingestion fails with `BQ_TABLE=document_chunks is the frozen v2 table` | Set `BQ_TABLE=document_chunks_v3` (or another v3 table name). |
| Upload bucket card shows `permission_denied` / `org_policy` / `unsafe_bucket` | Run the printed `gcloud` command (or `scripts/deploy_cloudrun.sh --upload-bucket-only`) as an admin; buckets must have enforced PAP, UBLA on, and no `allUsers` / `allAuthenticatedUsers` bindings. |
| Level 3 upload ingest returns HTTP 409 `needs_confirmation` | Review the Document AI + Gemini vision max-cost estimate and confirm it in the UI (or pass `confirm_cost_usd >= max_usd`). |
| Model shows under `"Pricing & availability"` as unavailable | Its entry in `data/model_verification.json` failed (e.g. `404` model access or `429` base-model quota 0). Fix access/quota and re-run `scripts/verify_models.py`. |
| HTTP 400 `judge_model must differ from synthesis_model` | Pick two different models; a model never grades its own answers. |
| Vector index `ACTIVE (coverage 0%)` or `mm_vec_idx` `NOT CREATED` | BigQuery builds IVF above 5,000 rows and allows one vector index per table; `VECTOR_SEARCH` falls back to exact brute-force scan automatically. |
| Search index `TEMPORARILY DISABLED` or `UNUSED` | BigQuery disables search indexes below 10 GB and reports `UNUSED` when combined with `VECTOR_SEARCH` in one query; `SEARCH()` still executes accurately. |
| Level 6 error `rag_engine needs RAG_ENGINE_CORPUS_ID` or `RAG_ENGINE_LOCATION` rejected | Run `scripts/provision_rag_engine.py`, copy `RAG_ENGINE_CORPUS_ID` into `.env`, and use a regional location such as `us-central1` (`global` is not supported by Vertex AI RAG Engine). |
| Levels 7–8 locked in `live` mode or fail with `ALLOYDB_DSN` / `VVS2_COLLECTION` unset | `DEMO_MODE=live` defaults to `DEFAULT_LIVE_LEVELS` (Levels 1–6) at \$0/mo idle; use `DEMO_MODE=replay` for recorded runs, or bring up an ephemeral store (`scripts/ephemeral_alloydb.sh --up` / `scripts/ephemeral_vvs2.sh --up`) and append `alloydb_scann` / `vertex_vector_v2` to `ENABLED_LEVELS`. |

---

## 6. Cloud Run

Prerequisites: the project is bootstrapped (BigQuery dataset, corpus bucket, Document AI processor, Vertex AI Search
corpus + uploads stores, and Vertex AI RAG Engine `RagCorpus`) and your deployer identity can enable APIs, create
buckets/service accounts, grant IAM, run Cloud Build, and deploy Cloud Run.

1. `cp deploy/cloudrun.env.example deploy/cloudrun.env` (git-ignored) and set `GCP_PROJECT_ID`, `GCS_CORPUS_BUCKET`,
   `UPLOAD_BUCKET` (optional; defaults to `<project>-rag-arena-uploads`), `UPLOAD_BUCKET_MODE` (`auto` | `existing`),
   `DOCAI_PROCESSOR_ID`, `VAIS_*` (including `VAIS_UPLOADS_*`), `RAG_ENGINE_LOCATION`, `RAG_ENGINE_CORPUS_ID`,
   `REGION`, `ACCESS_MODE` (`iap` | `public`), `IAP_MEMBERS`, `DEPLOY_REPLAY`, and `LIVE_INGEST`.
2. `scripts/deploy_cloudrun.sh --dry-run`, then `scripts/deploy_cloudrun.sh --yes` (or `--upload-bucket-only` to
   provision/verify just the private upload bucket and its IAM). It leak-scans the Cloud Build upload set, creates or
   verifies the private upload bucket (PAP enforced, UBLA on, no auto-delete lifecycle), grants the live runtime SA
   least-privilege roles (`aiplatform.user` covering Vertex AI models + RAG Engine, `bigquery.jobUser`, dataset `WRITER`,
   `documentai.apiUser`, `discoveryengine.editor|viewer`, `modelarmor.user` when `MODEL_ARMOR_TEMPLATE_ID` is set, corpus
   bucket access, and `storage.objectUser` + `storage.legacyBucketReader` on `UPLOAD_BUCKET`, plus `storage.objectViewer`
   on `UPLOAD_BUCKET` for the Vertex AI Search service agent while Google's project-level `aiplatform.ragServiceAgent`
   reads corpus/upload buckets directly), leaves the replay SA with zero roles, deploys the services, and prints their URLs.
3. IAP: open the live URL and sign in with an allowlisted account; each signed-in user sees only their own `upl_*`
   workspaces (keyed by SHA-256 of the IAP email), while the public replay service exposes zero upload routes (`404`)
   or identity fields.

Notes: requests time out after `TIMEOUT` (default 900 s; `INGEST_JOB_TIMEOUT_SECONDS=840`). Cold-start SDK
initialization runs off the Uvicorn event loop and the UI bootstrap retries for up to 30 s so `/api/v1/health` and
`/api/v1/levels` recover cleanly after scale-to-zero (`min-instances=0`). For **restricted enterprise orgs (VPC-SC,
pre-provisioned service accounts, CMEK, or no public invokers)**, configure `CREATE_SERVICE_ACCOUNTS=false` +
`EXISTING_LIVE_SA_EMAIL` / `EXISTING_REPLAY_SA_EMAIL`, Direct VPC Egress (`VPC_NETWORK`, `VPC_SUBNETWORK`, `VPC_EGRESS`),
`REPLAY_PUBLIC_INVOKER=false`, `CORPUS_BUCKET_KMS_KEY`, `BIGQUERY_KMS_KEY`, and `CLOUD_BUILD_WORKER_POOL` in
`deploy/cloudrun.env` or `infra/main.tf`. `scripts/teardown_cloudrun.sh --yes` removes
the Cloud Run services, service accounts, and bucket IAM bindings created by deploy while keeping your buckets and
BigQuery dataset intact.

---

## 7. Release History, Document ACL Backfill & Automated Documentation (`v1.5+`)

- **BigQuery Document ACL backfill (`v1.6.3`, zero re-ingest)**: existing deployments upgrade `document_chunks_v3` in place with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS allowed_groups ARRAY<STRING>` followed by `app.ingestion.bq_indexer.build_acl_backfill_sql(...)` (and optional `build_row_access_policy_ddl(...)` for DB-enforced BigQuery `ROW ACCESS POLICY`).
- **In-app `/releases` tab**: open `/releases` (or click the version badge in the top bar) to browse the parsed
  `CHANGELOG.md` timeline, `docs/ARCHITECTURE.md`, and `docs/SETUP_GUIDE.md` directly inside the app (`GET /api/v1/releases`).
- **Pre-build & CI check**: run `PYTHONPATH=backend .venv/bin/python scripts/build_docs_manifest.py --check` to verify
  6-way version alignment (`CHANGELOG.md`, `backend/app/core/config.py`, `frontend/package.json`,
  `frontend/package-lock.json`, `.release-please-manifest.json`, and `dev_q01.json`) and zero-leak hygiene.
- **GitHub Actions (`release-please`)**: `.github/workflows/release-docs.yml` runs the offline suites and manifest check
  on every PR/push to `main` and maintains an automated Release PR (`release-please-config.json`, zero external secrets)
  from Conventional Commits (`feat:`, `fix:`, `perf:`, `docs:`), creating the `v1.x.y` tag and GitHub Release on merge.
