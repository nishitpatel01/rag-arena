# The RAG Arena — Setup Guide

Two ways to run it:

1. **Zero-credential replay** (`DEMO_MODE=replay`, the default): plays the committed recordings of real Level 1–6
   runs. No Google Cloud account, no credentials, no network calls to GCP.
2. **Live on your own GCP project** (`DEMO_MODE=live` or `record`): real Vertex AI (including Vertex AI RAG Engine),
   BigQuery, Document AI, Cloud Storage, and Vertex AI Search calls, benchmark and private-PDF ingestion from the UI,
   and (in `record`) new golden-set recordings.

**All six levels**, private user PDF uploads (`v1.1`–`v1.2`), `/compare`, `/leaderboard` (with winner highlighting), and
the configurable Cloud Run deployment are live and enabled by default
(`ENABLED_LEVELS=level1_naive,level2_hybrid,level3_multimodal,level4_agentic,managed_vertex,rag_engine`; override in
`.env` or the environment). Level 5 needs the `VAIS_*` keys and Level 6 needs `RAG_ENGINE_CORPUS_ID` in live mode.

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

---

## 4. Tests

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests -q   # 415 passed (offline; no GCP calls)
cd frontend && npm test && npm run build                # 182 passed across 18 suites + tsc/vite build
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
   `documentai.apiUser`, `discoveryengine.editor|viewer`, corpus bucket access, and `storage.objectUser` +
   `storage.legacyBucketReader` on `UPLOAD_BUCKET`, plus `storage.objectViewer` on `UPLOAD_BUCKET` for the Vertex AI
   Search service agent while Google's project-level `aiplatform.ragServiceAgent` reads corpus/upload buckets directly),
   leaves the replay SA with zero roles, deploys the services, and prints their URLs.
3. IAP: open the live URL and sign in with an allowlisted account; each signed-in user sees only their own `upl_*`
   workspaces (keyed by SHA-256 of the IAP email), while the public replay service exposes zero upload routes (`404`)
   or identity fields.

Notes: requests time out after `TIMEOUT` (default 900 s; `INGEST_JOB_TIMEOUT_SECONDS=840`). Cold-start SDK
initialization runs off the Uvicorn event loop and the UI bootstrap retries for up to 30 s so `/api/v1/health` and
`/api/v1/levels` recover cleanly after scale-to-zero (`min-instances=0`). `scripts/teardown_cloudrun.sh --yes` removes
the Cloud Run services, service accounts, and bucket IAM bindings created by deploy while keeping your buckets and
BigQuery dataset intact.
