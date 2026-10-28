#!/usr/bin/env bash
# Deploy The RAG Arena to Cloud Run in YOUR Google Cloud project (spec §8.1).
#
#   cp deploy/cloudrun.env.example deploy/cloudrun.env   # git-ignored; fill in your values
#   scripts/deploy_cloudrun.sh --dry-run                  # validate + print the plan (never calls gcloud)
#   scripts/deploy_cloudrun.sh --yes                      # apply (idempotent; safe to re-run)
#   scripts/deploy_cloudrun.sh --upload-bucket-only       # only create/verify the private upload bucket + grants
#
# Creates only: an Artifact Registry repo, one Cloud Build image, the live runtime service account (+ least-privilege
# bindings), the live Cloud Run service (IAP or public) and, when DEPLOY_REPLAY=true, a role-less replay service
# account + a public replay-only service. scripts/teardown_cloudrun.sh removes exactly these.
# Also (v1.1): the private upload bucket for user documents (UPLOAD_BUCKET) is created by YOUR identity when missing
# (UPLOAD_BUCKET_MODE=auto) and its readers are granted; a denied create is reported, never silently skipped.
# Teardown removes the bucket grants but keeps the bucket and your documents.
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/deploy/cloudrun.env"
MODE=""
declare -A CFG=()

# Infra keys (consumed by this script, never forwarded to the container).
readonly INFRA_KEYS="GCP_PROJECT_ID REGION SERVICE_NAME ACCESS_MODE IAP_MEMBERS DEPLOY_REPLAY REPLAY_SERVICE_NAME
ARTIFACT_REPO RUNTIME_SA_NAME REPLAY_SA_NAME LIVE_INGEST IMAGE_TAG MIN_INSTANCES MAX_INSTANCES REPLAY_MAX_INSTANCES
CPU MEMORY REPLAY_MEMORY TIMEOUT REPLAY_TIMEOUT CONCURRENCY REPLAY_CONCURRENCY LEAK_SCAN_TOKENS"
# App settings forwarded to the LIVE service as env vars (same names as .env).
readonly APP_KEYS="DEMO_MODE GCP_LOCATION VERTEX_GENAI_LOCATION GCS_CORPUS_BUCKET UPLOAD_BUCKET UPLOAD_BUCKET_MODE
UPLOAD_BUCKET_KMS_KEY BQ_DATASET BQ_TABLE ENABLED_LEVELS
GEMINI_FAST_MODEL GEMINI_PRO_MODEL GEMINI_REASONING_MODEL GEMINI_THINKING_LEVEL TEXT_EMBEDDING_MODEL
MULTIMODAL_EMBEDDING_MODEL RERANKER_MODEL JUDGE_PROVIDER JUDGE_MODEL JUDGE_LOCATION JUDGE_MAX_TOKENS
JUDGE_TIMEOUT_SECONDS SYNTHESIS_MAX_TOKENS SYNTHESIS_TIMEOUT_SECONDS DOCAI_LOCATION DOCAI_PROCESSOR_ID
DOCAI_PROCESSOR_VERSION DOCAI_TIMEOUT_SECONDS VISION_MODEL VAIS_LOCATION VAIS_DATA_STORE_ID VAIS_ENGINE_ID
VAIS_UPLOADS_DATA_STORE_ID VAIS_UPLOADS_ENGINE_ID
VAIS_PAGE_SIZE VAIS_MAX_SEGMENTS VAIS_SUMMARY_RESULT_COUNT VAIS_TIMEOUT_SECONDS VAIS_SUMMARY_MODEL_VERSION
HYBRID_CANDIDATE_K RERANK_TOP_N RANKING_TIMEOUT_SECONDS AGENT_MAX_TURNS AGENT_MAX_TOOL_CALLS_PER_TURN
AGENT_THINKING_LEVEL AGENT_MODEL_CALL_TIMEOUT_SECONDS QUERY_TIMEOUT_SECONDS INGEST_JOB_TIMEOUT_SECONDS
MAX_SSE_FRAMES REPLAY_SPEED APP_VERSION CORS_ALLOW_ORIGIN_REGEX"
# Local-only keys tolerated (e.g. pasted from .env) but NEVER forwarded to Cloud Run.
readonly LOCAL_ONLY_KEYS="CLOUDSDK_CONFIG GOOGLE_APPLICATION_CREDENTIALS PUBLISH_SCRUB_TOKENS ALLOW_DEV_FIXTURES
RECORDINGS_DIR MODEL_VERIFICATION_FILE STATIC_DIR ALLOYDB_DSN ALLOYDB_TABLE VVS2_COLLECTION"
readonly APIS_BASE="run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com"
readonly CMD_TIMEOUT=600      # seconds per gcloud call
readonly BUILD_TIMEOUT=2400   # seconds for Cloud Build (npm ci + pip install)
readonly IAM_RETRIES=6        # new service accounts take a few seconds to become bindable
readonly UPLOAD_FAIL_EXIT=3   # --upload-bucket-only: bucket not usable (permission, org policy, conflict, unsafe)
readonly UPLOAD_SA_ROLES="roles/storage.objectUser roles/storage.legacyBucketReader"
readonly CREATE_HINT='gcloud storage buckets create gs://${UPLOAD_BUCKET} --project=${GCP_PROJECT_ID} --location=${REGION} --uniform-bucket-level-access --public-access-prevention'
UPLOAD_STATE="not-run"

die() { echo "ERROR: $*" >&2; exit 1; }
warn() { echo "WARNING: $*" >&2; }
plan() { echo "PLAN $*"; }
is_bool() { [[ "$1" == "true" || "$1" == "false" ]]; }
in_list() { local needle="$1" word; shift; for word in $*; do [[ "$word" == "$needle" ]] && return 0; done; return 1; }

usage() {
  cat <<'EOF'
Usage: scripts/deploy_cloudrun.sh [--env-file PATH] (--dry-run | --yes | --upload-bucket-only)
  --env-file PATH       deploy config (default: deploy/cloudrun.env, copy of deploy/cloudrun.env.example)
  --dry-run             validate the config and print the plan; never calls gcloud
  --yes                 apply the plan (idempotent)
  --upload-bucket-only  only create/verify the private upload bucket and grant its readers (exit 3 if not usable)
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      --env-file) (($# >= 2)) || die "--env-file needs a path"; ENV_FILE="$2"; shift 2 ;;
      --dry-run) MODE="dry-run"; shift ;;
      --yes) MODE="apply"; shift ;;
      --upload-bucket-only) MODE="upload-only"; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
  done
  [[ -n "$MODE" ]] || { usage >&2; die "refusing to deploy without --yes (use --dry-run to preview the plan)"; }
}

load_env_file() {
  [[ -f "$ENV_FILE" ]] || die "config file not found: $ENV_FILE (copy deploy/cloudrun.env.example)"
  local line key value count=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    count=$((count + 1)); ((count <= 400)) || die "config file too long (> 400 lines)"
    line="${line%$'\r'}"
    [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" == *=* ]] || die "line $count is not KEY=VALUE"
    key="${line%%=*}"; key="${key//[[:space:]]/}"; value="${line#*=}"
    value="${value#\"}"; value="${value%\"}"; value="${value#\'}"; value="${value%\'}"
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "line $count: invalid key '$key'"
    if ! in_list "$key" $INFRA_KEYS $APP_KEYS $LOCAL_ONLY_KEYS && [[ ! "$key" =~ ^MODEL_LOCATION_[A-Z0-9_]+$ ]]; then
      die "unknown key $key in $ENV_FILE (typo? see deploy/cloudrun.env.example)"
    fi
    CFG["$key"]="$value"
  done < "$ENV_FILE"
}

cfg() { echo "${CFG[$1]:-${2:-}}"; }

set_defaults() {
  local key def
  for def in REGION=us-central1 SERVICE_NAME=rag-arena ACCESS_MODE=iap DEPLOY_REPLAY=true \
             REPLAY_SERVICE_NAME=rag-arena-replay ARTIFACT_REPO=rag-arena RUNTIME_SA_NAME=rag-arena-run \
             REPLAY_SA_NAME=rag-arena-replay LIVE_INGEST=true MIN_INSTANCES=0 MAX_INSTANCES=2 REPLAY_MAX_INSTANCES=3 \
             CPU=1 MEMORY=2Gi REPLAY_MEMORY=1Gi TIMEOUT=900 REPLAY_TIMEOUT=300 CONCURRENCY=20 REPLAY_CONCURRENCY=40 \
             DEMO_MODE=live BQ_DATASET=rag_arena; do
    key="${def%%=*}"; [[ -n "${CFG[$key]:-}" ]] || CFG["$key"]="${def#*=}"
  done
  [[ -n "${CFG[GCS_CORPUS_BUCKET]:-}" || -z "${CFG[GCP_PROJECT_ID]:-}" ]] || CFG[GCS_CORPUS_BUCKET]="${CFG[GCP_PROJECT_ID]}-rag-arena-corpus"
  [[ -n "${CFG[UPLOAD_BUCKET]:-}" || -z "${CFG[GCP_PROJECT_ID]:-}" ]] || CFG[UPLOAD_BUCKET]="${CFG[GCP_PROJECT_ID]}-rag-arena-uploads"
  [[ -n "${CFG[UPLOAD_BUCKET_MODE]:-}" ]] || CFG[UPLOAD_BUCKET_MODE]="auto"
}

check_re() { [[ "$(cfg "$1")" =~ $2 ]] || die "$1 is invalid ('$(cfg "$1")'): $3"; }
check_int() {
  local v; v="$(cfg "$1")"
  [[ "$v" =~ ^[0-9]+$ ]] && ((v >= $2 && v <= $3)) || die "$1 must be an integer between $2 and $3, got '$v'"
}

validate_names() {
  [[ -n "$(cfg GCP_PROJECT_ID)" ]] || die "GCP_PROJECT_ID is required"
  check_re GCP_PROJECT_ID '^[a-z][a-z0-9-]{4,28}[a-z0-9]$' "6-30 lowercase letters, digits, hyphens"
  check_re REGION '^[a-z]+-[a-z]+[0-9]+$' "a Cloud Run region such as us-central1"
  check_re SERVICE_NAME '^[a-z]([a-z0-9-]{0,47}[a-z0-9])?$' "lowercase, <= 49 chars"
  check_re REPLAY_SERVICE_NAME '^[a-z]([a-z0-9-]{0,47}[a-z0-9])?$' "lowercase, <= 49 chars"
  check_re ARTIFACT_REPO '^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$' "lowercase repository id"
  check_re RUNTIME_SA_NAME '^[a-z][a-z0-9-]{4,28}[a-z0-9]$' "6-30 chars service account id"
  check_re REPLAY_SA_NAME '^[a-z][a-z0-9-]{4,28}[a-z0-9]$' "6-30 chars service account id"
  check_re GCS_CORPUS_BUCKET '^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$' "a Cloud Storage bucket name"
  check_re BQ_DATASET '^[A-Za-z0-9_]{1,1024}$' "a BigQuery dataset id"
  validate_upload_bucket
  [[ "$(cfg REPLAY_SERVICE_NAME)" != "$(cfg SERVICE_NAME)" ]] || die "REPLAY_SERVICE_NAME must differ from SERVICE_NAME"
  [[ "$(cfg REPLAY_SA_NAME)" != "$(cfg RUNTIME_SA_NAME)" ]] || die "REPLAY_SA_NAME must differ from RUNTIME_SA_NAME"
  [[ -z "$(cfg IMAGE_TAG)" ]] || check_re IMAGE_TAG '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$' "a docker tag"
}

validate_upload_bucket() {
  check_re UPLOAD_BUCKET '^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$' "a Cloud Storage bucket name"
  [[ "$(cfg UPLOAD_BUCKET)" != *google* && "$(cfg UPLOAD_BUCKET)" != goog* ]] || die "UPLOAD_BUCKET may not contain 'google' or start with 'goog'"
  [[ "$(cfg UPLOAD_BUCKET)" != "$(cfg GCS_CORPUS_BUCKET)" ]] || die "UPLOAD_BUCKET must differ from GCS_CORPUS_BUCKET"
  [[ "$(cfg UPLOAD_BUCKET_MODE)" == "auto" || "$(cfg UPLOAD_BUCKET_MODE)" == "existing" ]] \
    || die "UPLOAD_BUCKET_MODE must be auto or existing, got '$(cfg UPLOAD_BUCKET_MODE)'"
  [[ -z "$(cfg UPLOAD_BUCKET_KMS_KEY)" ]] || check_re UPLOAD_BUCKET_KMS_KEY \
    '^projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/]+$' "projects/P/locations/L/keyRings/R/cryptoKeys/K"
}

validate_access() {
  local mode member members
  mode="$(cfg ACCESS_MODE)"
  [[ "$mode" == "iap" || "$mode" == "public" ]] || die "ACCESS_MODE must be iap or public, got '$mode'"
  is_bool "$(cfg DEPLOY_REPLAY)" || die "DEPLOY_REPLAY must be true or false, got '$(cfg DEPLOY_REPLAY)'"
  is_bool "$(cfg LIVE_INGEST)" || die "LIVE_INGEST must be true or false, got '$(cfg LIVE_INGEST)'"
  [[ "$(cfg DEMO_MODE)" == "live" ]] || die "DEMO_MODE must be live for the live service (the replay copy is DEPLOY_REPLAY)"
  [[ "$mode" == "iap" ]] || return 0
  members="$(cfg IAP_MEMBERS)"
  [[ -n "$members" ]] || die "IAP_MEMBERS is required when ACCESS_MODE=iap (e.g. user:you@example.com,group:team@example.com)"
  IFS=',' read -r -a _members <<< "$members"
  ((${#_members[@]} <= 50)) || die "IAP_MEMBERS has more than 50 entries"
  for member in "${_members[@]}"; do
    [[ "$member" =~ ^(user|group|serviceAccount):[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ || "$member" =~ ^domain:[a-z0-9.-]+\.[a-z]{2,}$ ]] \
      || die "IAP_MEMBERS entry '$member' must be user:EMAIL, group:EMAIL, serviceAccount:EMAIL or domain:DOMAIN"
  done
}

validate_sizing() {
  check_int MIN_INSTANCES 0 2
  check_int MAX_INSTANCES 1 10
  check_int REPLAY_MAX_INSTANCES 1 10
  (($(cfg MIN_INSTANCES) <= $(cfg MAX_INSTANCES))) || die "MIN_INSTANCES must not exceed MAX_INSTANCES"
  check_re CPU '^(1|2|4)$' "1, 2 or 4"
  check_re MEMORY '^[0-9]{3,5}Mi$|^[1-8]Gi$' "e.g. 512Mi or 2Gi"
  check_re REPLAY_MEMORY '^[0-9]{3,5}Mi$|^[1-8]Gi$' "e.g. 512Mi or 1Gi"
  check_int TIMEOUT 300 3600
  check_int REPLAY_TIMEOUT 60 3600
  check_int CONCURRENCY 1 1000
  check_int REPLAY_CONCURRENCY 1 1000
}

validate_values() {
  local key
  for key in "${!CFG[@]}"; do
    in_list "$key" $LOCAL_ONLY_KEYS && continue
    case "${CFG[$key]}" in
      *[\"\'\`]*|*$'\n'*|*$'\t'*) die "$key contains quotes, backticks, tabs or newlines" ;;
    esac
  done
}

validate_all() { validate_values; validate_names; validate_access; validate_sizing; }

image_uri() {
  echo "$(cfg REGION)-docker.pkg.dev/$(cfg GCP_PROJECT_ID)/$(cfg ARTIFACT_REPO)/$(cfg SERVICE_NAME):${1}"
}

ingest_roles() {
  if [[ "$(cfg LIVE_INGEST)" == "true" ]]; then echo "roles/discoveryengine.editor roles/storage.objectUser"
  else echo "roles/discoveryengine.viewer roles/storage.objectViewer"; fi
}

print_plan() {
  local sa apis de_role gcs_role member access
  sa="$(cfg RUNTIME_SA_NAME)"; read -r de_role gcs_role <<< "$(ingest_roles)"
  apis="$APIS_BASE"; [[ "$(cfg ACCESS_MODE)" == "iap" ]] && apis="$apis iap.googleapis.com"
  plan "enable-apis $apis"
  plan "artifact-repo $(cfg ARTIFACT_REPO) ($(cfg REGION), docker)"
  plan "cloud-build $(image_uri '<tag>')"
  plan "service-account $sa"
  plan "iam project roles/aiplatform.user -> $sa"
  plan "iam project roles/bigquery.jobUser -> $sa"
  plan "iam dataset $(cfg BQ_DATASET) roles/bigquery.dataEditor -> $sa"
  plan "iam project roles/documentai.apiUser -> $sa"
  plan "iam project $de_role -> $sa"
  plan "iam bucket $(cfg GCS_CORPUS_BUCKET) $gcs_role -> $sa"
  if [[ "$(cfg UPLOAD_BUCKET_MODE)" == "auto" ]]; then
    plan "upload-bucket $(cfg UPLOAD_BUCKET) (auto: create if missing; private; $(cfg REGION))"
  else plan "upload-bucket $(cfg UPLOAD_BUCKET) (existing: verify only; never created)"; fi
  local role
  for role in $UPLOAD_SA_ROLES; do plan "iam bucket $(cfg UPLOAD_BUCKET) $role -> $sa"; done
  plan "iam bucket $(cfg UPLOAD_BUCKET) roles/storage.objectViewer -> discoveryengine-service-agent"
  access="$(cfg ACCESS_MODE)"
  plan "service $(cfg SERVICE_NAME) (live, $access, sa=$sa, min=$(cfg MIN_INSTANCES), max=$(cfg MAX_INSTANCES), cpu=$(cfg CPU), memory=$(cfg MEMORY), timeout=$(cfg TIMEOUT)s)"
  if [[ "$access" == "iap" ]]; then
    plan "iap-service-agent identity"
    plan "iam service $(cfg SERVICE_NAME) roles/run.invoker -> iap-service-agent"
    IFS=',' read -r -a _members <<< "$(cfg IAP_MEMBERS)"
    for member in "${_members[@]}"; do plan "iam iap $(cfg SERVICE_NAME) roles/iap.httpsResourceAccessor -> $member"; done
  fi
  [[ "$(cfg DEPLOY_REPLAY)" == "true" ]] || return 0
  plan "service-account $(cfg REPLAY_SA_NAME) (no roles)"
  plan "service $(cfg REPLAY_SERVICE_NAME) (replay, public, sa=$(cfg REPLAY_SA_NAME), min=0, max=$(cfg REPLAY_MAX_INSTANCES), cpu=$(cfg CPU), memory=$(cfg REPLAY_MEMORY), timeout=$(cfg REPLAY_TIMEOUT)s)"
}

live_env_pairs() {
  local key
  echo "DEMO_MODE=live"
  echo "GCP_PROJECT_ID=$(cfg GCP_PROJECT_ID)"
  echo "ALLOW_DEV_FIXTURES=0"
  for key in $APP_KEYS; do
    [[ "$key" == "DEMO_MODE" || -z "${CFG[$key]:-}" ]] && continue
    echo "$key=${CFG[$key]}"
  done
  for key in "${!CFG[@]}"; do [[ "$key" =~ ^MODEL_LOCATION_ ]] && echo "$key=${CFG[$key]}"; done
  return 0
}

replay_env_pairs() {
  echo "DEMO_MODE=replay"
  echo "ALLOW_DEV_FIXTURES=0"
  [[ -z "${CFG[ENABLED_LEVELS]:-}" ]] || echo "ENABLED_LEVELS=${CFG[ENABLED_LEVELS]}"
}

print_env() {
  local pair
  while IFS= read -r pair; do echo "ENV live $pair"; done < <(live_env_pairs)
  [[ "$(cfg DEPLOY_REPLAY)" == "true" ]] || return 0
  while IFS= read -r pair; do echo "ENV replay $pair"; done < <(replay_env_pairs)
}

print_warnings() {
  if [[ "$(cfg ACCESS_MODE)" == "public" ]]; then
    warn "ACCESS_MODE=public: anyone on the internet can start LIVE runs that bill Vertex AI, BigQuery, Document AI and"
    warn "Vertex AI Search in project $(cfg GCP_PROJECT_ID) (cost exposure bounded only by MAX_INSTANCES=$(cfg MAX_INSTANCES)). Prefer iap."
  fi
  if [[ "$(cfg LIVE_INGEST)" == "true" ]]; then
    echo "NOTE: LIVE_INGEST=true grants write roles for in-service ingestion; ingestion must finish within TIMEOUT=$(cfg TIMEOUT)s"
    echo "      (set INGEST_JOB_TIMEOUT_SECONDS below it). Longer ingestion: run it locally (README) against the same project."
  fi
}

# ---------------------------------------------------------------------------------------------------- apply helpers

gc() { timeout "$CMD_TIMEOUT" gcloud "$@" --project="$(cfg GCP_PROJECT_ID)" --quiet; }

preflight() {
  command -v gcloud >/dev/null || die "gcloud not found on PATH (install the Google Cloud CLI)"
  command -v python3 >/dev/null || die "python3 not found on PATH"
  [[ -z "$(cfg CLOUDSDK_CONFIG)" ]] || export CLOUDSDK_CONFIG="$(cfg CLOUDSDK_CONFIG)"
  ACTIVE_ACCOUNT="$(timeout 60 gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n1)"
  [[ -n "$ACTIVE_ACCOUNT" ]] || die "no active gcloud account (run gcloud auth login)"
  PROJECT_NUMBER="$(gc projects describe "$(cfg GCP_PROJECT_ID)" --format='value(projectNumber)')"
  [[ "$PROJECT_NUMBER" =~ ^[0-9]+$ ]] || die "cannot read project number of $(cfg GCP_PROJECT_ID)"
  LIVE_SA="$(cfg RUNTIME_SA_NAME)@$(cfg GCP_PROJECT_ID).iam.gserviceaccount.com"
  REPLAY_SA="$(cfg REPLAY_SA_NAME)@$(cfg GCP_PROJECT_ID).iam.gserviceaccount.com"
}

enable_apis() {
  local apis="$APIS_BASE"; [[ "$(cfg ACCESS_MODE)" == "iap" ]] && apis="$apis iap.googleapis.com"
  echo "==> enabling APIs: $apis"
  # shellcheck disable=SC2086
  gc services enable $apis
}

ensure_repo() {
  if gc artifacts repositories describe "$(cfg ARTIFACT_REPO)" --location="$(cfg REGION)" >/dev/null 2>&1; then
    echo "==> artifact repo $(cfg ARTIFACT_REPO) exists"; return 0
  fi
  echo "==> creating artifact repo $(cfg ARTIFACT_REPO)"
  gc artifacts repositories create "$(cfg ARTIFACT_REPO)" --repository-format=docker --location="$(cfg REGION)" \
    --description="The RAG Arena images (scripts/deploy_cloudrun.sh)"
}

leak_tokens() {
  local token
  for token in "$(cfg GCP_PROJECT_ID)" "$PROJECT_NUMBER" "$(cfg GCS_CORPUS_BUCKET)" "$(cfg UPLOAD_BUCKET)" "$ACTIVE_ACCOUNT"; do
    [[ -n "$token" ]] && echo "$token"
  done
  IFS=',' read -r -a _extra <<< "$(cfg LEAK_SCAN_TOKENS)"
  for token in "${_extra[@]}"; do [[ -n "$token" ]] && echo "$token"; done
  return 0
}

scan_upload() {
  local files tokens hits
  echo "==> leak scan of the Cloud Build upload (.gcloudignore applied)"
  files="$(mktemp)"; tokens="$(mktemp)"; TMP_FILES+=("$files" "$tokens")
  (cd "$REPO_ROOT" && timeout 120 gcloud meta list-files-for-upload . > "$files")
  [[ -s "$files" ]] || die "upload file list is empty"
  if grep -E '(^|/)\.env($|\.)|^deploy/[^/]*\.env$|credentials.*\.json$|\.pem$|\.key$' "$files"; then
    die "secret-looking files would be uploaded (fix .gcloudignore)"
  fi
  leak_tokens > "$tokens"
  hits="$(cd "$REPO_ROOT" && tr '\n' '\0' < "$files" | xargs -0 grep -I -l -i -F -f "$tokens" -- 2>/dev/null || true)"
  [[ -z "$hits" ]] || die "identity tokens found in files that would be uploaded:"$'\n'"$hits"
  echo "    clean: $(wc -l < "$files") files, $(wc -l < "$tokens") identity tokens checked"
}

build_image() {
  local tag
  tag="$(cfg IMAGE_TAG)"
  [[ -n "$tag" ]] || tag="$(cd "$REPO_ROOT" && git rev-parse --short=12 HEAD 2>/dev/null || echo manual)-$(date -u +%Y%m%d%H%M%S)"
  IMAGE="$(image_uri "$tag")"
  echo "==> Cloud Build: $IMAGE"
  (cd "$REPO_ROOT" && timeout "$BUILD_TIMEOUT" gcloud builds submit . --tag="$IMAGE" --region="$(cfg REGION)" \
    --timeout="${BUILD_TIMEOUT}s" --project="$(cfg GCP_PROJECT_ID)" --quiet)
}

ensure_sa() {
  local name="$1" display="$2" email="$1@$(cfg GCP_PROJECT_ID).iam.gserviceaccount.com"
  if gc iam service-accounts describe "$email" >/dev/null 2>&1; then echo "==> service account $name exists"; return 0; fi
  echo "==> creating service account $name"
  gc iam service-accounts create "$name" --display-name="$display"
}

retry_iam() {
  local attempt
  for ((attempt = 1; attempt <= IAM_RETRIES; attempt++)); do
    if "$@" >/dev/null; then return 0; fi
    ((attempt < IAM_RETRIES)) || break
    echo "    IAM call failed (attempt $attempt/$IAM_RETRIES); retrying in 10 s (new service accounts propagate slowly)"
    sleep 10
  done
  die "IAM binding failed after $IAM_RETRIES attempts: $*"
}

grant_dataset_writer() {
  local dataset="$(cfg GCP_PROJECT_ID):$(cfg BQ_DATASET)" acl
  acl="$(mktemp)"; TMP_FILES+=("$acl")
  timeout "$CMD_TIMEOUT" bq --project_id="$(cfg GCP_PROJECT_ID)" show --format=prettyjson "$dataset" > "$acl"
  if ! python3 - "$acl" "$LIVE_SA" <<'PY'
import json, sys
path, sa = sys.argv[1], sys.argv[2]
ds = json.load(open(path, encoding="utf-8"))
access = ds.get("access", [])
if any(e.get("role") in ("WRITER", "roles/bigquery.dataEditor") and e.get("userByEmail") == sa for e in access):
    sys.exit(1)  # already granted
access.append({"role": "WRITER", "userByEmail": sa})  # WRITER == roles/bigquery.dataEditor on this dataset only
json.dump({"access": access}, open(path, "w", encoding="utf-8"))
PY
  then echo "    dataset $(cfg BQ_DATASET): WRITER already granted"; return 0; fi
  retry_iam timeout "$CMD_TIMEOUT" bq --project_id="$(cfg GCP_PROJECT_ID)" update --source "$acl" "$dataset"
}

grant_live_roles() {
  local role de_role gcs_role member="serviceAccount:$LIVE_SA"
  read -r de_role gcs_role <<< "$(ingest_roles)"
  echo "==> least-privilege bindings for $(cfg RUNTIME_SA_NAME)"
  for role in roles/aiplatform.user roles/bigquery.jobUser roles/documentai.apiUser "$de_role"; do
    retry_iam gc projects add-iam-policy-binding "$(cfg GCP_PROJECT_ID)" --member="$member" --role="$role" --condition=None
  done
  grant_dataset_writer
  retry_iam gc storage buckets add-iam-policy-binding "gs://$(cfg GCS_CORPUS_BUCKET)" --member="$member" --role="$gcs_role"
}

# ---------------------------------------------------------------------------------------------- upload bucket (v1.1)

upload_report() {  # state message [remedy]
  UPLOAD_STATE="$1"
  echo "UPLOAD BUCKET $1: $2"
  [[ -z "${3:-}" ]] || echo "    remedy: $3" >&2
}

classify_create_failure() {  # stderr of a failed create -> sets UPLOAD_STATE + prints remedy
  local err="$1" constraints
  echo "    gcloud said: $(head -c 600 <<< "$err" | tr '\n' ' ')" >&2
  constraints="$(grep -o 'constraints/[A-Za-z0-9_.]*' <<< "$err" | sort -u | tr '\n' ' ' || true)"
  if [[ -n "$constraints" ]] || grep -qi 'organization policy' <<< "$err"; then
    upload_report org_policy "creating the bucket was blocked by organization policy: ${constraints:-unnamed constraint}" \
      "ask your organization admin to allow it for this project, or set UPLOAD_BUCKET to an approved private bucket with UPLOAD_BUCKET_MODE=existing"
  elif grep -q "HTTPError 403\|storage.buckets.create access\|Permission 'storage.buckets.create'" <<< "$err"; then
    upload_report permission_denied "your identity cannot create buckets in this project" \
      "needs permission storage.buckets.create (for example role roles/storage.admin); ask a project admin to grant it or to run: $CREATE_HINT"
  elif grep -q 'HTTPError 409\|not available' <<< "$err"; then
    upload_report name_conflict "the bucket name is already taken by another project (bucket names are global)" \
      "set UPLOAD_BUCKET to a different, unique name in $ENV_FILE and re-run"
  else
    upload_report error "bucket create failed (see gcloud output above)" "fix the error above and re-run with --upload-bucket-only"
  fi
}

upload_privacy_problems() {  # describe JSON on stdin -> one problem per line (empty = private)
  python3 -c '
import json, sys
d = json.load(sys.stdin)
if d.get("public_access_prevention") != "enforced":
    print("public access prevention is not enforced")
if d.get("uniform_bucket_level_access") is not True:
    print("uniform bucket-level access is off (object ACLs could make files public)")
'
}

describe_upload_bucket() {  # -> 0 + JSON on stdout | 1 missing | 2 denied | 3 other (stderr kept in UPLOAD_ERR)
  local out errf rc=0
  errf="$(mktemp)"; TMP_FILES+=("$errf")
  out="$(gc storage buckets describe "gs://$(cfg UPLOAD_BUCKET)" --format=json 2>"$errf")" || rc=$?
  UPLOAD_ERR="$(cat "$errf")"
  if ((rc == 0)); then echo "$out"; return 0; fi
  if grep -q 'not found: 404\|HTTPError 404' <<< "$UPLOAD_ERR"; then return 1; fi
  if grep -q 'HTTPError 403\|storage.buckets.get access' <<< "$UPLOAD_ERR"; then return 2; fi
  return 3
}

create_upload_bucket() {  # -> 0 created | 1 failed (UPLOAD_STATE set)
  local errf args
  errf="$(mktemp)"; TMP_FILES+=("$errf")
  args=(storage buckets create "gs://$(cfg UPLOAD_BUCKET)" --location="$(cfg REGION)" --uniform-bucket-level-access
        --public-access-prevention)
  [[ -z "$(cfg UPLOAD_BUCKET_KMS_KEY)" ]] || args+=(--default-encryption-key="$(cfg UPLOAD_BUCKET_KMS_KEY)")
  echo "==> creating private upload bucket gs://$(cfg UPLOAD_BUCKET) as $ACTIVE_ACCOUNT"
  if gc "${args[@]}" >/dev/null 2>"$errf"; then return 0; fi
  classify_create_failure "$(cat "$errf")"
  return 1
}

grant_upload_readers() {
  local role member="serviceAccount:$LIVE_SA" agent
  echo "==> upload bucket readers: $(cfg RUNTIME_SA_NAME) ($UPLOAD_SA_ROLES) + Vertex AI Search service agent (objectViewer)"
  for role in $UPLOAD_SA_ROLES; do
    retry_iam gc storage buckets add-iam-policy-binding "gs://$(cfg UPLOAD_BUCKET)" --member="$member" --role="$role"
  done
  if ! timeout "$CMD_TIMEOUT" gcloud beta services identity create --service=discoveryengine.googleapis.com \
      --project="$(cfg GCP_PROJECT_ID)" --quiet >/dev/null 2>&1; then
    warn "could not ensure the Vertex AI Search service agent (is discoveryengine.googleapis.com enabled?)"
    warn "Level 5 imports of uploaded documents will fail until it exists; re-run with --upload-bucket-only"
    return 0
  fi
  agent="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com"
  retry_iam gc storage buckets add-iam-policy-binding "gs://$(cfg UPLOAD_BUCKET)" --member="$agent" --role=roles/storage.objectViewer
}

ensure_upload_bucket() {  # -> 0 usable | 1 not usable (UPLOAD_STATE + remedy printed)
  local meta rc=0 problems created=false
  meta="$(describe_upload_bucket)" || rc=$?
  case "$rc" in
    0) ;;
    1)
      if [[ "$(cfg UPLOAD_BUCKET_MODE)" == "existing" ]]; then
        upload_report not_found "gs://$(cfg UPLOAD_BUCKET) does not exist and UPLOAD_BUCKET_MODE=existing" \
          "create the bucket, fix UPLOAD_BUCKET, or set UPLOAD_BUCKET_MODE=auto"
        return 1
      fi
      create_upload_bucket || return 1
      created=true
      rc=0; meta="$(describe_upload_bucket)" || rc=$?
      ((rc == 0)) || { upload_report error "bucket created but cannot be read back" "re-run with --upload-bucket-only"; return 1; }
      ;;
    2) upload_report permission_denied "your identity cannot read gs://$(cfg UPLOAD_BUCKET)" \
         "needs storage.buckets.get on the bucket (for example roles/storage.admin), or ask the bucket owner"; return 1 ;;
    *) echo "    gcloud said: $(head -c 600 <<< "$UPLOAD_ERR" | tr '\n' ' ')" >&2
       upload_report error "cannot read gs://$(cfg UPLOAD_BUCKET)" "fix the error above and re-run with --upload-bucket-only"; return 1 ;;
  esac
  problems="$(upload_privacy_problems <<< "$meta")"
  if [[ -n "$problems" ]]; then
    upload_report unsafe_bucket "gs://$(cfg UPLOAD_BUCKET) is not private: $(tr '\n' ';' <<< "$problems")" \
      "fix those settings or choose another bucket; this script never changes an existing bucket"
    return 1
  fi
  grant_upload_readers
  if [[ "$created" == "true" ]]; then upload_report created "gs://$(cfg UPLOAD_BUCKET) created private (public access prevention enforced, uniform access on, no auto-delete)"
  else upload_report ok "gs://$(cfg UPLOAD_BUCKET) is private (public access prevention enforced, uniform access on)"; fi
}

upload_bucket_only() {
  TMP_FILES=()
  trap 'rm -f "${TMP_FILES[@]:-}"' EXIT
  preflight
  ensure_upload_bucket || exit "$UPLOAD_FAIL_EXIT"
}

write_env_yaml() {
  local out="$1" pair key value
  : > "$out"; chmod 600 "$out"
  while IFS= read -r pair; do
    key="${pair%%=*}"; value="${pair#*=}"
    printf '%s: "%s"\n' "$key" "${value//\\/\\\\}" >> "$out"
  done
}

deploy_live() {
  local envf access_flags
  envf="$(mktemp)"; TMP_FILES+=("$envf")
  live_env_pairs | write_env_yaml "$envf"
  if [[ "$(cfg ACCESS_MODE)" == "iap" ]]; then access_flags=(--no-allow-unauthenticated --iap)
  else access_flags=(--allow-unauthenticated --no-iap); fi
  echo "==> deploying live service $(cfg SERVICE_NAME) ($(cfg ACCESS_MODE))"
  gc run deploy "$(cfg SERVICE_NAME)" --image="$IMAGE" --region="$(cfg REGION)" --service-account="$LIVE_SA" \
    "${access_flags[@]}" --env-vars-file="$envf" --port=8080 --cpu="$(cfg CPU)" --memory="$(cfg MEMORY)" \
    --concurrency="$(cfg CONCURRENCY)" --min-instances="$(cfg MIN_INSTANCES)" --max-instances="$(cfg MAX_INSTANCES)" \
    --timeout="$(cfg TIMEOUT)"
}

configure_iap() {
  local agent member
  [[ "$(cfg ACCESS_MODE)" == "iap" ]] || return 0
  echo "==> IAP: service agent invoker + allowlist"
  timeout "$CMD_TIMEOUT" gcloud beta services identity create --service=iap.googleapis.com \
    --project="$(cfg GCP_PROJECT_ID)" --quiet >/dev/null
  agent="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com"
  retry_iam gc run services add-iam-policy-binding "$(cfg SERVICE_NAME)" --region="$(cfg REGION)" \
    --member="$agent" --role=roles/run.invoker
  IFS=',' read -r -a _members <<< "$(cfg IAP_MEMBERS)"
  for member in "${_members[@]}"; do
    retry_iam gc iap web add-iam-policy-binding --resource-type=cloud-run --service="$(cfg SERVICE_NAME)" \
      --region="$(cfg REGION)" --member="$member" --role=roles/iap.httpsResourceAccessor
  done
}

deploy_replay() {
  local envf
  [[ "$(cfg DEPLOY_REPLAY)" == "true" ]] || return 0
  ensure_sa "$(cfg REPLAY_SA_NAME)" "RAG Arena replay (no roles)"
  envf="$(mktemp)"; TMP_FILES+=("$envf")
  replay_env_pairs | write_env_yaml "$envf"
  echo "==> deploying public replay service $(cfg REPLAY_SERVICE_NAME)"
  gc run deploy "$(cfg REPLAY_SERVICE_NAME)" --image="$IMAGE" --region="$(cfg REGION)" --service-account="$REPLAY_SA" \
    --allow-unauthenticated --no-iap --env-vars-file="$envf" --port=8080 --cpu="$(cfg CPU)" \
    --memory="$(cfg REPLAY_MEMORY)" --concurrency="$(cfg REPLAY_CONCURRENCY)" --min-instances=0 \
    --max-instances="$(cfg REPLAY_MAX_INSTANCES)" --timeout="$(cfg REPLAY_TIMEOUT)"
}

print_urls() {
  local svc
  echo "==> service URLs (contain your project number; do not commit them)"
  local services=("$(cfg SERVICE_NAME)")
  [[ "$(cfg DEPLOY_REPLAY)" != "true" ]] || services+=("$(cfg REPLAY_SERVICE_NAME)")
  for svc in "${services[@]}"; do
    echo "URL $svc $(gc run services describe "$svc" --region="$(cfg REGION)" --format='value(status.url)')"
  done
}

apply_plan() {
  TMP_FILES=()
  trap 'rm -f "${TMP_FILES[@]:-}"' EXIT
  preflight
  enable_apis
  ensure_repo
  scan_upload
  build_image
  ensure_sa "$(cfg RUNTIME_SA_NAME)" "RAG Arena live runtime"
  grant_live_roles
  if ! ensure_upload_bucket; then
    warn "uploads will be DISABLED in the app until the upload bucket is usable ($UPLOAD_STATE; remedy above)."
    warn "fix it, then run: scripts/deploy_cloudrun.sh --upload-bucket-only"
  fi
  deploy_live
  configure_iap
  deploy_replay
  print_urls
}

main() {
  parse_args "$@"
  load_env_file
  set_defaults
  validate_all
  if [[ "$MODE" == "upload-only" ]]; then upload_bucket_only; return 0; fi
  print_warnings
  print_plan
  print_env
  if [[ "$MODE" == "dry-run" ]]; then echo "DRY RUN: nothing was created (re-run with --yes to apply)"; return 0; fi
  apply_plan
  if [[ "$UPLOAD_STATE" == "ok" || "$UPLOAD_STATE" == "created" ]]; then echo "DONE"
  else echo "DONE (uploads disabled: upload bucket $UPLOAD_STATE)"; fi
}

main "$@"
