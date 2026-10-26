#!/usr/bin/env bash
# Remove ONLY what scripts/deploy_cloudrun.sh created (same deploy/cloudrun.env): the replay + live Cloud Run services,
# the live service account's project/dataset/bucket bindings, both service accounts and the Artifact Registry repo.
# Never touches APIs, BigQuery data, the corpus bucket, Vertex AI Search, Document AI or anything else.
# The upload bucket (v1.1) loses only the grants deploy added; the bucket and your documents are KEPT.
#
#   scripts/teardown_cloudrun.sh --dry-run   # print what would be deleted (never calls gcloud)
#   scripts/teardown_cloudrun.sh --yes       # delete
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly CMD_TIMEOUT=600
ENV_FILE="${SCRIPT_DIR}/../deploy/cloudrun.env"
MODE=""
declare -A CFG=()

die() { echo "ERROR: $*" >&2; exit 1; }
cfg() { echo "${CFG[$1]:-${2:-}}"; }

parse_args() {
  while (($#)); do
    case "$1" in
      --env-file) (($# >= 2)) || die "--env-file needs a path"; ENV_FILE="$2"; shift 2 ;;
      --dry-run) MODE="dry-run"; shift ;;
      --yes) MODE="apply"; shift ;;
      *) die "unknown argument: $1" ;;
    esac
  done
  [[ -n "$MODE" ]] || die "refusing to delete without --yes (use --dry-run to preview)"
}

load_env_file() {
  [[ -f "$ENV_FILE" ]] || die "config file not found: $ENV_FILE"
  local line key value count=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    count=$((count + 1)); ((count <= 400)) || die "config file too long"
    line="${line%$'\r'}"
    [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# || "$line" != *=* ]] && continue
    key="${line%%=*}"; key="${key//[[:space:]]/}"; value="${line#*=}"
    value="${value#\"}"; value="${value%\"}"; value="${value#\'}"; value="${value%\'}"
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] && CFG["$key"]="$value"
  done < "$ENV_FILE"
  local def
  for def in REGION=us-central1 SERVICE_NAME=rag-arena REPLAY_SERVICE_NAME=rag-arena-replay ARTIFACT_REPO=rag-arena \
             RUNTIME_SA_NAME=rag-arena-run REPLAY_SA_NAME=rag-arena-replay BQ_DATASET=rag_arena LIVE_INGEST=true; do
    key="${def%%=*}"; [[ -n "${CFG[$key]:-}" ]] || CFG["$key"]="${def#*=}"
  done
  [[ -n "$(cfg GCP_PROJECT_ID)" ]] || die "GCP_PROJECT_ID is required"
  [[ "$(cfg GCP_PROJECT_ID)" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "GCP_PROJECT_ID is invalid"
  [[ -n "$(cfg GCS_CORPUS_BUCKET)" ]] || CFG[GCS_CORPUS_BUCKET]="$(cfg GCP_PROJECT_ID)-rag-arena-corpus"
  [[ -n "$(cfg UPLOAD_BUCKET)" ]] || CFG[UPLOAD_BUCKET]="$(cfg GCP_PROJECT_ID)-rag-arena-uploads"
}

print_plan() {
  echo "DELETE service $(cfg REPLAY_SERVICE_NAME) ($(cfg REGION))"
  echo "DELETE service $(cfg SERVICE_NAME) ($(cfg REGION))"
  echo "DELETE iam project/dataset/bucket bindings of $(cfg RUNTIME_SA_NAME)"
  echo "DELETE iam upload-bucket bindings of $(cfg RUNTIME_SA_NAME) + discoveryengine-service-agent"
  echo "DELETE service-account $(cfg REPLAY_SA_NAME)"
  echo "DELETE service-account $(cfg RUNTIME_SA_NAME)"
  echo "DELETE artifact-repo $(cfg ARTIFACT_REPO) ($(cfg REGION))"
  echo "KEEP upload bucket $(cfg UPLOAD_BUCKET) (your documents; delete it yourself if you want)"
}

gc() { timeout "$CMD_TIMEOUT" gcloud "$@" --project="$(cfg GCP_PROJECT_ID)" --quiet; }

delete_service() {
  if gc run services describe "$1" --region="$(cfg REGION)" >/dev/null 2>&1; then
    gc run services delete "$1" --region="$(cfg REGION)"
  else echo "    service $1 not found (skipped)"; fi
}

remove_live_bindings() {
  local sa="$(cfg RUNTIME_SA_NAME)@$(cfg GCP_PROJECT_ID).iam.gserviceaccount.com" role
  for role in roles/aiplatform.user roles/bigquery.jobUser roles/documentai.apiUser roles/discoveryengine.editor \
              roles/discoveryengine.viewer; do
    gc projects remove-iam-policy-binding "$(cfg GCP_PROJECT_ID)" --member="serviceAccount:$sa" --role="$role" \
      --condition=None >/dev/null 2>&1 || echo "    project binding $role not present (skipped)"
  done
  for role in roles/storage.objectUser roles/storage.objectViewer; do
    gc storage buckets remove-iam-policy-binding "gs://$(cfg GCS_CORPUS_BUCKET)" --member="serviceAccount:$sa" \
      --role="$role" >/dev/null 2>&1 || echo "    bucket binding $role not present (skipped)"
  done
  remove_dataset_entry "$sa"
  remove_upload_bindings "$sa"
}

remove_upload_bindings() {
  local sa="$1" role number agent
  for role in roles/storage.objectUser roles/storage.legacyBucketReader; do
    gc storage buckets remove-iam-policy-binding "gs://$(cfg UPLOAD_BUCKET)" --member="serviceAccount:$sa" \
      --role="$role" >/dev/null 2>&1 || echo "    upload bucket binding $role not present (skipped)"
  done
  number="$(gc projects describe "$(cfg GCP_PROJECT_ID)" --format='value(projectNumber)' 2>/dev/null || true)"
  [[ "$number" =~ ^[0-9]+$ ]] || { echo "    project number unreadable; Vertex AI Search agent grant left in place"; return 0; }
  agent="serviceAccount:service-${number}@gcp-sa-discoveryengine.iam.gserviceaccount.com"
  gc storage buckets remove-iam-policy-binding "gs://$(cfg UPLOAD_BUCKET)" --member="$agent" \
    --role=roles/storage.objectViewer >/dev/null 2>&1 || echo "    upload bucket agent binding not present (skipped)"
}

remove_dataset_entry() {
  local dataset="$(cfg GCP_PROJECT_ID):$(cfg BQ_DATASET)" acl
  acl="$(mktemp)"; trap 'rm -f "$acl"' RETURN
  timeout "$CMD_TIMEOUT" bq --project_id="$(cfg GCP_PROJECT_ID)" show --format=prettyjson "$dataset" > "$acl"
  if python3 - "$acl" "$1" <<'PY'
import json, sys
path, sa = sys.argv[1], sys.argv[2]
access = json.load(open(path, encoding="utf-8")).get("access", [])
kept = [e for e in access if e.get("userByEmail") != sa]
if len(kept) == len(access):
    sys.exit(1)
json.dump({"access": kept}, open(path, "w", encoding="utf-8"))
PY
  then timeout "$CMD_TIMEOUT" bq --project_id="$(cfg GCP_PROJECT_ID)" update --source "$acl" "$dataset" >/dev/null
  else echo "    dataset entry not present (skipped)"; fi
}

delete_sa() {
  local email="$1@$(cfg GCP_PROJECT_ID).iam.gserviceaccount.com"
  if gc iam service-accounts describe "$email" >/dev/null 2>&1; then gc iam service-accounts delete "$email"
  else echo "    service account $1 not found (skipped)"; fi
}

delete_repo() {
  if gc artifacts repositories describe "$(cfg ARTIFACT_REPO)" --location="$(cfg REGION)" >/dev/null 2>&1; then
    gc artifacts repositories delete "$(cfg ARTIFACT_REPO)" --location="$(cfg REGION)"
  else echo "    artifact repo not found (skipped)"; fi
}

main() {
  parse_args "$@"
  load_env_file
  print_plan
  if [[ "$MODE" == "dry-run" ]]; then echo "DRY RUN: nothing was deleted"; return 0; fi
  command -v gcloud >/dev/null || die "gcloud not found on PATH"
  [[ -z "$(cfg CLOUDSDK_CONFIG)" ]] || export CLOUDSDK_CONFIG="$(cfg CLOUDSDK_CONFIG)"
  delete_service "$(cfg REPLAY_SERVICE_NAME)"
  delete_service "$(cfg SERVICE_NAME)"
  remove_live_bindings
  delete_sa "$(cfg REPLAY_SA_NAME)"
  delete_sa "$(cfg RUNTIME_SA_NAME)"
  delete_repo
  echo "DONE"
}

main "$@"
