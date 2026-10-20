#!/usr/bin/env bash
# Bootstrap Google Cloud APIs, BigQuery dataset/table, and search indexes for The RAG Arena.
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-}"
LOCATION="${GCP_LOCATION:-us-central1}"
GCS_CORPUS_BUCKET="${GCS_CORPUS_BUCKET:-${PROJECT_ID}-rag-arena-corpus}"
BQ_DATASET="${BQ_DATASET:-rag_arena}"
BQ_TABLE="${BQ_TABLE:-document_chunks}"
CORPUS_DIR="${CORPUS_DIR:-data/sample_docs}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: GCP_PROJECT_ID environment variable must be set before running bootstrap_gcp.sh" >&2
  exit 1
fi

if [[ -d "${HOME}/Downloads/google-cloud-cli-linux-x86_64/google-cloud-sdk/bin" ]]; then
  export PATH="${HOME}/Downloads/google-cloud-cli-linux-x86_64/google-cloud-sdk/bin:${PATH}"
fi
# Use an isolated gcloud profile by exporting CLOUDSDK_CONFIG in your environment / .env; never hardcoded here.
if [[ -n "${CLOUDSDK_CONFIG:-}" ]]; then
  echo "==> Using gcloud configuration directory: ${CLOUDSDK_CONFIG}"
fi
export CLOUDSDK_METRICS_ENVIRONMENT="${CLOUDSDK_METRICS_ENVIRONMENT:+$CLOUDSDK_METRICS_ENVIRONMENT }datacloud.ai-agent"

echo "==> Configuring Google Cloud project: ${PROJECT_ID} (${LOCATION})"
gcloud config set project "${PROJECT_ID}" --quiet

REQUIRED_APIS=(
  "aiplatform.googleapis.com"
  "bigquery.googleapis.com"
  "discoveryengine.googleapis.com"
  "documentai.googleapis.com"
  "storage.googleapis.com"
  "alloydb.googleapis.com"
)

echo "==> Enabling required Google Cloud APIs..."
for api in "${REQUIRED_APIS[@]}"; do
  gcloud services enable "${api}" --project="${PROJECT_ID}" --quiet
done

echo "==> Provisioning secure GCS corpus bucket gs://${GCS_CORPUS_BUCKET} (if not exists)..."
if ! gcloud storage buckets describe "gs://${GCS_CORPUS_BUCKET}" --project="${PROJECT_ID}" --format="json(name)" --quiet >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${GCS_CORPUS_BUCKET}" \
    --project="${PROJECT_ID}" \
    --location="${LOCATION}" \
    --default-storage-class=STANDARD \
    --uniform-bucket-level-access \
    --public-access-prevention \
    --clear-soft-delete \
    --quiet
fi

if [[ -d "${CORPUS_DIR}" ]]; then
  echo "==> Uploading corpus files from ${CORPUS_DIR} to gs://${GCS_CORPUS_BUCKET}/sample_docs/..."
  gcloud storage cp --recursive --no-clobber "${CORPUS_DIR}/"* "gs://${GCS_CORPUS_BUCKET}/sample_docs/" \
    --project="${PROJECT_ID}" \
    --quiet
fi

echo "==> Creating BigQuery dataset ${PROJECT_ID}:${BQ_DATASET} (if not exists)..."
if ! bq show --dataset "${PROJECT_ID}:${BQ_DATASET}" >/dev/null 2>&1; then
  bq --location="${LOCATION}" mk --dataset --label datacloud:ai-agent "${PROJECT_ID}:${BQ_DATASET}"
fi

echo "==> Creating BigQuery table ${PROJECT_ID}:${BQ_DATASET}.${BQ_TABLE}..."
bq query --use_legacy_sql=false --label datacloud:ai-agent --project_id="${PROJECT_ID}" "
CREATE TABLE IF NOT EXISTS \`${PROJECT_ID}.${BQ_DATASET}.${BQ_TABLE}\` (
  chunk_id STRING NOT NULL,
  tier_profile STRING NOT NULL,
  solicitation_id STRING NOT NULL,
  doc_name STRING NOT NULL,
  doc_type STRING NOT NULL,
  element_type STRING NOT NULL,
  section_id STRING,
  amendment_number INT64,
  effective_date DATE,
  amends_section STRING,
  page_number INT64,
  bbox STRUCT<page INT64, x0 FLOAT64, y0 FLOAT64, x1 FLOAT64, y1 FLOAT64>,
  content STRING,
  embedding ARRAY<FLOAT64>,
  embedding_multimodal ARRAY<FLOAT64>
)
CLUSTER BY tier_profile, solicitation_id;
"

echo "==> Creating BigQuery Full-Text SEARCH Index for Level 2 Hybrid RRF..."
bq query --use_legacy_sql=false --label datacloud:ai-agent --project_id="${PROJECT_ID}" "
CREATE SEARCH INDEX IF NOT EXISTS \`${BQ_TABLE}_search_idx\`
ON \`${PROJECT_ID}.${BQ_DATASET}.${BQ_TABLE}\`(content);
"

echo "==> Creating BigQuery COSINE IVF Vector Indexes (if >= 5,000 rows populated)..."
bq query --use_legacy_sql=false --label datacloud:ai-agent --project_id="${PROJECT_ID}" "
CREATE VECTOR INDEX IF NOT EXISTS \`${BQ_TABLE}_vec_idx\`
ON \`${PROJECT_ID}.${BQ_DATASET}.${BQ_TABLE}\`(embedding)
OPTIONS(distance_type = 'COSINE', index_type = 'IVF');

CREATE VECTOR INDEX IF NOT EXISTS \`${BQ_TABLE}_mm_vec_idx\`
ON \`${PROJECT_ID}.${BQ_DATASET}.${BQ_TABLE}\`(embedding_multimodal)
OPTIONS(distance_type = 'COSINE', index_type = 'IVF');
" || echo "WARN: Skipping IVF vector index creation until table has >= 5,000 non-null embedding rows (BigQuery VECTOR_SEARCH uses exact search automatically on smaller tables)."

echo "==> Bootstrap complete for ${PROJECT_ID}:${BQ_DATASET}.${BQ_TABLE}"
