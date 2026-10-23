# The RAG Arena v3 — Setup Guide

Two ways to run it:

1. **Zero-credential replay** (`DEMO_MODE=replay`, the default): plays the committed recordings of real Level 1–4
   runs. No Google Cloud account, no credentials, no network calls to GCP.
2. **Live on your own GCP project** (`DEMO_MODE=live` or `record`): real Vertex AI and BigQuery calls,
   ingestion from the UI, and (in `record`) new recordings.

**Levels 1–4** are live and enabled by default (`ENABLED_LEVELS=level1_naive,level2_hybrid,level3_multimodal,level4_agentic`; override in `.env` or the environment). Level 5, the Compare page and the Cloud
Run deployment are planned; see the [README roadmap](../README.md#roadmap).

---

## 1. Prerequisites

| Tool | Version | Needed for |
| :-- | :-- | :-- |
| Python | 3.11+ (the backend image uses 3.12) | backend |
| Node.js + npm | 20+ | frontend |
| Google Cloud CLI (`gcloud`, `bq`) | current | live mode only |
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
  `gemini-4-argon` or `gemini-3.8-flash`, judge `claude-opus-5-5`. `GET /api/v1/models` lists the playable
  pairs under `recorded_pairs`.
- Stage events replay with their recorded relative timing; speed it up with `REPLAY_SPEED=4.0` (range
  `(0, 100]`).
- An ad-hoc question returns `engine_error: No recording for this question in replay mode` — replay never
  improvises an answer.
- Smoke test without the UI:
  ```bash
  curl -sN -X POST localhost:8000/api/v1/battle/stream -H 'content-type: application/json' \
    -d '{"query":"replay","question_id":"q_exact_clin_lookup","engines":["level1_naive"],
         "synthesis_model":"gemini-3.8-flash","judge_model":"claude-opus-5-5"}'
  ```
  Expected events: `run_started`, five `stage_started` / `stage_completed` pairs, `answer_complete`,
  `eval_complete`, `battle_complete`.

> [!NOTE]
> `docker-compose.yml` and `backend/Dockerfile` predate v3. The compose frontend container proxies `/api` to
> its own `localhost:8000` and the backend image does not serve the built UI, so compose is not a supported
> path until Phase 6. Use the two commands above.

---

## 3. Live mode on your own GCP project

### 3.1 Configure `.env`

```bash
cp .env.example .env    # .env is git-ignored; never commit real identifiers
```

| Key | Example | Notes |
| :-- | :-- | :-- |
| `DEMO_MODE` | `live` | `record` = `live` + writes `data/recordings/**` |
| `GCP_PROJECT_ID` | `your-gcp-project-id` | required in `live` / `record` |
| `GCP_LOCATION` | `us-central1` | BigQuery client location |
| `VERTEX_GENAI_LOCATION` | `global` | google-genai client location (embeddings, Gemini) |
| `GCS_CORPUS_BUCKET` | `your-gcp-project-id-rag-arena-corpus` | defaults to `<project>-rag-arena-corpus` |
| `BQ_DATASET` | `rag_arena` | |
| `BQ_TABLE` | `document_chunks_v3` | live ingestion refuses `document_chunks` (the frozen v2 table) |
| `GEMINI_FAST_MODEL` | `gemini-4-argon` | default synthesis model |
| `GEMINI_THINKING_LEVEL` | `high` | Gemini synthesis thinking level |
| `JUDGE_PROVIDER` / `JUDGE_MODEL` / `JUDGE_LOCATION` | `anthropic_vertex` / `claude-opus-5-5` / `global` | default judge; must differ from the synthesis models |
| `TEXT_EMBEDDING_MODEL` | `gemini-embedding-2` | query and document embeddings |
| `QUERY_TIMEOUT_SECONDS` / `INGEST_JOB_TIMEOUT_SECONDS` | `120` / `1800` | explicit bounds (`120` recommended when running or replaying Level 4 multi-turn ADK traces) |
| `AGENT_MAX_TURNS` / `AGENT_MAX_TOOL_CALLS_PER_TURN` / `AGENT_TURN_TIMEOUT_SECONDS` / `AGENT_THINKING_LEVEL` | `8` / `8` / `90` / `low` | Level 4 ADK runner bounds and Gemini thinking level |
| `CLOUDSDK_CONFIG` / `GOOGLE_APPLICATION_CREDENTIALS` | empty | optional isolated gcloud profile / ADC file |
| `MODEL_LOCATION_<ID>` | `MODEL_LOCATION_CLAUDE_OPUS_5_5=global` | optional per-model endpoint override |
| `DOCAI_LOCATION` / `DOCAI_PROCESSOR_ID` | `us` / short id printed by `scripts/provision_documentai.py` | Level 3 Layout Parser processor (never the full resource name) |
| `DOCAI_PROCESSOR_VERSION` | `pretrained-layout-parser-v1.5-2025-08-25` | release candidate; returns table structure but no bounding boxes (empty = processor default) |
| `DOCAI_TIMEOUT_SECONDS` / `VISION_MODEL` | `120` / `gemini-4-argon` | Layout Parser request bound; Gemini model that transcribes diagram pages |

### 3.2 Authenticate

```bash
gcloud auth login
gcloud auth application-default login
```

The backend uses Application Default Credentials only — no key files are required or committed.

IAM for the principal running the backend: `roles/aiplatform.user`, `roles/bigquery.jobUser`, and
`roles/bigquery.dataEditor` on the `rag_arena` dataset. Running the bootstrap script additionally requires
permission to enable services and create a bucket and a dataset.

### 3.3 Bootstrap the project

`scripts/bootstrap_gcp.sh` reads **environment variables** (it does not parse `.env`). It also creates a
v2-schema chunks table named by `BQ_TABLE`; run it with the v2 name so it does not pre-create your v3 table
with the old schema — the Level 1 ingestion job creates the v3 table itself.

```bash
set -a; source .env; set +a
BQ_TABLE=document_chunks ./scripts/bootstrap_gcp.sh
```

What it does:
- enables `aiplatform`, `bigquery`, `storage`, `discoveryengine`, `documentai`, `alloydb` APIs (Level 1 uses
  the first three; the rest are for planned levels and parked engines);
- creates `gs://$GCS_CORPUS_BUCKET` with uniform bucket-level access and public access prevention, and uploads
  `data/sample_docs/` to `sample_docs/` (Level 1 ingestion reads the local copy; the bucket is for later levels);
- creates the `rag_arena` dataset;
- ensures the Level 3 Document AI processor `rag-arena-layout-parser` (`LAYOUT_PARSER_PROCESSOR`) by running
  `scripts/provision_documentai.py`, which is idempotent (creates it only when absent, never touches other
  processors) and prints only the short processor id — put it in `.env` as `DOCAI_PROCESSOR_ID`. It can also be
  run on its own: `PYTHONPATH=backend .venv/bin/python scripts/provision_documentai.py`.

### 3.4 Verify model access

```bash
PYTHONPATH=backend .venv/bin/python scripts/verify_models.py
```

One bounded live call per catalog model (60 s timeout, no retries) → `data/model_verification.json`
(model ids, ok flag, HTTP status, redacted message, timestamp). The backend marks any model without an `ok`
entry as unavailable and shows the recorded error in the model picker. The committed file reflects the
author's project — re-run it for yours.

Claude models are Anthropic partner models on Vertex AI, called on the `global` endpoint with `:rawPredict`.
**Claude Fable 5.1** additionally requires Anthropic data sharing to be enabled on your project. If you and
your organisation agree to that, enable it once:

```bash
curl -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  "https://aiplatform.googleapis.com/v1beta1/projects/your-gcp-project-id/locations/global/publishers/anthropic/models/claude-fable-5-1:setPublisherModelConfig" \
  -d '{"publisherModelConfig":{"dataSharingEnabledProvider":"anthropic"}}'
```

Then re-run `verify_models.py`.

### 3.5 Run, ingest, query

```bash
.venv/bin/python -m uvicorn app.main:app --app-dir backend --port 8000   # DEMO_MODE comes from .env
cd frontend && npm run dev
```

1. Open `http://localhost:5173/ingest` and run the **Level 1** ingestion job. The stream shows
   `discover_docs → parse → chunk → embed → load_bigquery → build_indexes → verify_counts` with per-document and
   embedding progress. On the reference run: 526 pages → 5,067 chunks × 3072-d, ≈5.6 minutes end to end (most
   of it in `embed`), `counts_by_tier {"naive": 5067}`.
2. Check the status card: live counts by tier, index states as BigQuery reports them, and the `ingest_runs`
   row. The Level 1 Run button stays disabled while the `naive` tier has 0 rows.
3. Open Level 1, pick a synthesis and a judge model (different models), ask a golden or custom question.
   Each run writes a `query_runs` row.
4. **Level 2**: run the Level 2 ingestion job from `/ingest`
   (`hybrid` tier: one chunk per page with a contextual-lineage header, plus vector and search indexes), then
   open Level 2. Its `rerank` stage calls the Discovery Engine Ranking API (`discoveryengine.googleapis.com`,
   enabled by the bootstrap script; `RERANKER_MODEL`, `RANKING_TIMEOUT_SECONDS`, `HYBRID_CANDIDATE_K` = 50,
   `RERANK_TOP_N` = 5). A Ranking API error fails the stage — there is no fallback ranker.
5. **Level 3**: with `DOCAI_*` set, run the Level 3 ingestion job
   (`multimodal` tier). `parse` calls the Layout Parser online (PDFs split into ≤ 15-page requests; $10 per
   1,000 pages) and Gemini vision on diagram pages; pages whose transcription is unusable are listed in
   `vision_failed_pages`. There is no PyMuPDF text fallback — a missing or failing processor fails `parse`.
6. **Level 4 (`level4_agentic`)**: reuses the Level 3 `multimodal` tier (no separate ingestion job; its Run button
   checks the `multimodal` tier count). Requires `google-adk` (included in `backend/requirements.txt`) and a Gemini
   synthesis model (`gemini-4-argon` or `gemini-3.8-flash`); selecting a Claude model as the Level 4 agent model
   returns HTTP 400 because `google-adk`'s Claude adapter requires the undeclared `anthropic` SDK.
7. To capture new recordings, restart with `DEMO_MODE=record` and run the golden questions; files land in
   `data/recordings/<engine>/<synthesis>__<judge>/<question_id>.json` with identity values redacted.

---

## 4. Tests

```bash
PYTHONPATH=backend .venv/bin/pytest backend/tests -q   # offline; no GCP calls
cd frontend && npm test && npm run build
```

`backend/tests/test_publish_manifest.py` also scans `README.md`, `docs/ARCHITECTURE.md`, `docs/SETUP_GUIDE.md`
and `.env.example` for internal terms and for the identity values in your local `.env`.

---

## 5. Troubleshooting

| Symptom | Cause / fix |
| :-- | :-- |
| Startup error `GCP_PROJECT_ID must be set … when DEMO_MODE=live\|record` | Set it in `.env` or the environment, or use `DEMO_MODE=replay`. |
| Ingestion fails with `BQ_TABLE=document_chunks is the frozen v2 table` | Set `BQ_TABLE=document_chunks_v3` (or another new table name). |
| `Live ingestion needs DEMO_MODE=live\|record` in the ingest status | You are in replay; ingestion makes no GCP calls there. |
| Model shows as unavailable in the picker | Its entry in `data/model_verification.json` is missing or failed; the reason is shown verbatim. Re-run `verify_models.py`. |
| HTTP 400 `judge_model must differ from synthesis_model` | Pick two different models; a model never grades its own answers. |
| Vector index `ACTIVE (coverage 0%)` | BigQuery is still building the IVF index; `VECTOR_SEARCH` brute-forces meanwhile (≈130 MB scanned per query on the 5,067-row tier). |
| Search index `TEMPORARILY DISABLED` | BigQuery reports this for tables below 10 GB; Level 1 does not use the search index. |
| Level 2 `retrieve` shows search index `UNUSED` | BigQuery reports "The search index cannot be used when a vector search index is used." for the combined VECTOR_SEARCH + SEARCH() statement; the lexical leg still runs without the index. |
| Recorded `retrieve` shows 0 bytes processed | BigQuery result-cache hit (`cache_hit: true`); ask a question not run recently to see a real scan. |
| `/api/v1/ingest/status` is slow on first load | It runs live BigQuery `COUNT(*)` and `INFORMATION_SCHEMA` queries. |
