# The RAG Arena: Setup & Deployment Guide

This guide walks through running **The RAG Arena** in two modes:
1. **Zero-Credential Replay Mode (`DEMO_MODE=replay`)**: Run immediately on any workstation using precomputed benchmark traces and golden procurement PDFs without requiring a Google Cloud billing account.
2. **Live Google Cloud Mode (`DEMO_MODE=live`)**: Provision BigQuery Vector + Search indexes, Vertex AI Gemini models, Discovery Engine Ranking API, and Google ADK live execution.

---

## 1. Prerequisites

- **Python:** `3.11+`
- **Node.js:** `20+` (for local frontend development) or **Docker & Docker Compose**
- **Google Cloud SDK (`gcloud` & `bq`):** Required only for `DEMO_MODE=live`
- **Terraform (`>= 1.5.0`):** Optional, for declarative infrastructure provisioning

---

## 2. Mode A: Zero-Credential Local Quickstart (`DEMO_MODE=replay`)

### Option 1: Docker Compose (Single Command)

```bash
docker compose up --build
```

- **Frontend Arena UI:** `http://localhost:5173`
- **Backend API & Health Check:** `http://localhost:8000/api/v1/health`

### Option 2: Native Workstation Execution

```bash
# 1. Generate the 18-document procurement PDF corpus (if not already present)
./scripts/download_procurement_corpus.sh

# 2. Start the Backend Server (Port 8000)
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DEMO_MODE="replay"
uvicorn app.main:app --reload --port 8000
```

In a second terminal:

```bash
# 3. Start the Frontend Vite Dev Server (Port 5173)
cd frontend
npm install
npm run dev
```

---

## 3. Mode B: Live Google Cloud Deployment (`DEMO_MODE=live`)

### Step 1: Authenticate with Google Cloud

```bash
gcloud auth login
gcloud auth application-default login
export GCP_PROJECT_ID="your-gcp-project-id"
export GCP_LOCATION="us-central1"
```

### Step 2: Provision BigQuery Dataset, Table & Search Indexes

Run the automated bootstrap script:

```bash
./scripts/bootstrap_gcp.sh
```

Or provision declaratively with Terraform:

```bash
cd infra
terraform init
terraform apply -var="project_id=${GCP_PROJECT_ID}" -var="region=${GCP_LOCATION}"
```

### Step 3: Build & Index the Procurement Corpus

```bash
# Generate the multi-document RFP solicitation packets + CUAD agreements
./scripts/download_procurement_corpus.sh
```

### Step 4: Launch the Backend in Live Mode

```bash
export DEMO_MODE="live"
export GCP_PROJECT_ID="your-gcp-project-id"
export GCP_LOCATION="us-central1"
export BQ_DATASET="rag_arena"
export BQ_TABLE="document_chunks"

uvicorn app.main:app --host 0.0.0.0 --port 8000 --app-dir backend
```

---

## 4. Environment Variable Reference

| Variable | Default | Description |
| :--- | :--- | :--- |
| `DEMO_MODE` | `replay` | `replay` for deterministic precomputed benchmark traces; `live` for real-time Google Cloud execution. |
| `GCP_PROJECT_ID` | *(none — required for `live`/`record`)* | Target Google Cloud Project ID, set only in the git-ignored `.env`. |
| `GCP_LOCATION` | `us-central1` | Region for Vertex AI and BigQuery resources. |
| `BQ_DATASET` | `rag_arena` | BigQuery dataset storing indexed procurement chunks. |
| `BQ_TABLE` | `document_chunks` | BigQuery table containing `naive`, `hybrid`, and `multimodal` tier profiles. |
| `VVS2_COLLECTION` | `procurement-chunks-v2` | Vertex AI Vector Search 2.0 Collection ID storing DataObjects + dense/sparse vectors. |
| `ALLOYDB_TABLE` | `document_chunks` | AlloyDB for PostgreSQL table indexed with `alloydb_scann` and `ai.hybrid_search()`. |
| `ALLOYDB_DSN` | *(empty)* | PostgreSQL connection string (`postgresql://...`) for live AlloyDB ScaNN queries. |
| `GEMINI_FAST_MODEL` | `gemini-4-argon` | Configurable generation & LLM-as-a-Judge model identifier. |
| `GEMINI_PRO_MODEL` | `gemini-4-argon` | Configurable complex reasoning model for Level 4 Google ADK agent (alias: `GEMINI_REASONING_MODEL`). |
| `GEMINI_THINKING_LEVEL` | `high` | Thinking depth (`low`, `medium`, `high`) passed via `thinking_config` to `gemini-4-argon`. |
| `TEXT_EMBEDDING_MODEL` | `gemini-embedding-2` | Latest GA unified embedding model used in text mode for Level 1 & Level 2. |
| `MULTIMODAL_EMBEDDING_MODEL` | `gemini-embedding-2` | Latest GA unified embedding model used in multimodal mode for Level 3 & Level 4. |
| `RERANKER_MODEL` | `semantic-ranker-default@latest` | Discovery Engine cross-encoder reranking model. |
| `QUERY_TIMEOUT_SECONDS` | `30` | Per-engine execution timeout bound (`1–120` seconds). |

---

## 5. Running the Verification Suite

```bash
.venv/bin/pytest backend/tests/ -v
```
