#!/usr/bin/env bash
# Ephemeral Level 8 (Vertex AI Vector Search 2.0, `vertex_vector_v2`) benchmark lifecycle:
#   scripts/ephemeral_vvs2.sh --dry-run          # preview full lifecycle (zero GCP calls)
#   scripts/ephemeral_vvs2.sh --up --yes         # create rag-arena-ephem-* Collection + load 3072-d chunks + DenseScann index
#   scripts/ephemeral_vvs2.sh --record --yes     # record golden benchmark runs into data/recordings/vertex_vector_v2/
#   scripts/ephemeral_vvs2.sh --down --yes       # delete DenseScann index + Collection and verify 0 ephemeral resources remain
#   scripts/ephemeral_vvs2.sh --full --yes       # --up -> --record -> automatic --down (via EXIT trap) -> $0/mo idle
#
# Safety invariants:
#   - Every created/deleted collection ID MUST begin with `rag-arena-ephem-` (refuses to touch neighbor collections).
#   - Mutating actions (--up, --record, --down, --full) require explicit `--yes`.
#   - Never writes GCP project IDs, tokens, or resource paths into tracked repository files.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly PY_BIN="${REPO_ROOT}/.venv/bin/python"
readonly CMD_TIMEOUT=6600

ENV_FILE="${REPO_ROOT}/.env"
ACTION=""
APPROVED="false"
SYNTHESIS_MODELS="gemini-4-argon,gemini-3.8-flash"
JUDGE_MODEL="claude-opus-5-5"
QUESTIONS=""
WRITE_QUERY_RUNS="false"
declare -A CFG=()

die() { echo "ERROR: $*" >&2; exit 1; }
cfg() { echo "${CFG[$1]:-${2:-}}"; }

parse_args() {
  while (($#)); do
    case "$1" in
      --env-file) (($# >= 2)) || die "--env-file requires a path"; ENV_FILE="$2"; shift 2 ;;
      --dry-run) ACTION="dry-run"; shift ;;
      --up) ACTION="up"; shift ;;
      --record) ACTION="record"; shift ;;
      --down) ACTION="down"; shift ;;
      --full) ACTION="full"; shift ;;
      --yes) APPROVED="true"; shift ;;
      --synthesis-models) (($# >= 2)) || die "--synthesis-models requires CSV"; SYNTHESIS_MODELS="$2"; shift 2 ;;
      --judge-model) (($# >= 2)) || die "--judge-model requires model id"; JUDGE_MODEL="$2"; shift 2 ;;
      --questions) (($# >= 2)) || die "--questions requires CSV"; QUESTIONS="$2"; shift 2 ;;
      --write-query-runs) WRITE_QUERY_RUNS="true"; shift ;;
      *) die "unknown argument: $1" ;;
    esac
  done
  [[ -n "$ACTION" ]] || die "specify one of --dry-run, --up, --record, --down, or --full"
  if [[ "$ACTION" != "dry-run" && "$APPROVED" != "true" ]]; then
    die "refusing to run --${ACTION} without explicit --yes (use --dry-run to preview)"
  fi
}

load_env_file() {
  [[ -f "$ENV_FILE" ]] || die "config file not found: $ENV_FILE"
  local line key value count=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    count=$((count + 1)); ((count <= 500)) || die "config file too long"
    line="${line%$'\r'}"
    [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# || "$line" != *=* ]] && continue
    key="${line%%=*}"; key="${key//[[:space:]]/}"; value="${line#*=}"
    value="${value#\"}"; value="${value%\"}"; value="${value#\'}"; value="${value%\'}"
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] && CFG["$key"]="$value"
  done < "$ENV_FILE"

  if [[ -n "${EPHEM_VVS2_COLLECTION:-}" ]]; then
    CFG[VVS2_COLLECTION]="$EPHEM_VVS2_COLLECTION"
  elif [[ -n "${CFG[EPHEM_VVS2_COLLECTION]:-}" ]]; then
    CFG[VVS2_COLLECTION]="${CFG[EPHEM_VVS2_COLLECTION]}"
  fi
  [[ -n "${CFG[VVS2_LOCATION]:-}" ]] || CFG[VVS2_LOCATION]="us-central1"
  [[ -n "${CFG[VVS2_COLLECTION]:-}" ]] || CFG[VVS2_COLLECTION]="rag-arena-ephem-vvs2"
  [[ -n "${CFG[VVS2_INDEX_ID]:-}" ]] || CFG[VVS2_INDEX_ID]="dense-scann"
  [[ -n "${CFG[BQ_DATASET]:-}" ]] || CFG[BQ_DATASET]="rag_arena"
  [[ -n "${CFG[BQ_TABLE]:-}" ]] || CFG[BQ_TABLE]="document_chunks_v3"

  [[ -n "$(cfg GCP_PROJECT_ID)" ]] || die "GCP_PROJECT_ID is required in $ENV_FILE"
  [[ "$(cfg GCP_PROJECT_ID)" =~ ^[a-z][a-z0-9-]{4,29}$ ]] || die "GCP_PROJECT_ID format is invalid"
  [[ "$(cfg VVS2_LOCATION)" =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || die "VVS2_LOCATION must be regional (e.g. us-central1)"
  if [[ ! "$(cfg VVS2_COLLECTION)" =~ ^rag-arena-ephem-[a-z0-9-]{1,42}$ ]]; then
    die "safety guard: VVS2_COLLECTION ($(cfg VVS2_COLLECTION)) must match ^rag-arena-ephem-[a-z0-9-]{1,42}$"
  fi
  [[ "$(cfg VVS2_INDEX_ID)" =~ ^[a-z][a-z0-9-]{0,62}$ ]] || die "VVS2_INDEX_ID format is invalid"
}

print_plan() {
  echo "Ephemeral Level 8 (Vertex AI Vector Search 2.0) lifecycle plan:"
  echo "  location      : $(cfg VVS2_LOCATION)"
  echo "  collection    : $(cfg VVS2_COLLECTION) (prefix-guarded rag-arena-ephem-*)"
  echo "  scann index   : $(cfg VVS2_INDEX_ID) (COSINE_DISTANCE, DenseScann UNIT_L2_NORM)"
  echo "  source vectors: BigQuery $(cfg BQ_DATASET).$(cfg BQ_TABLE) (tier_profile='hybrid', 0 re-embedding)"
  echo "  models        : synthesis=[${SYNTHESIS_MODELS}] judge=${JUDGE_MODEL}"
  echo "  teardown      : DELETE index $(cfg VVS2_INDEX_ID) -> DELETE collection $(cfg VVS2_COLLECTION) -> verify 0 rag-arena-ephem-* remain"
}

export_runtime_env() {
  local k
  for k in "${!CFG[@]}"; do
    export "${k}=${CFG[$k]}"
  done
  export VVS2_LOCATION="$(cfg VVS2_LOCATION)"
  export VVS2_COLLECTION="$(cfg VVS2_COLLECTION)"
  export VVS2_INDEX_ID="$(cfg VVS2_INDEX_ID)"
  [[ -x "$PY_BIN" ]] || die "Python virtualenv not found at $PY_BIN"
}

do_up() {
  echo "[vvs2] Provisioning Collection $(cfg VVS2_COLLECTION), loading BigQuery 3072-d chunks, and building DenseScann index..." >&2
  PYTHONPATH="${REPO_ROOT}/backend" timeout "$CMD_TIMEOUT" "$PY_BIN" "${SCRIPT_DIR}/record_scann_benchmarks.py" load-vvs2 --index-timeout-seconds 5400
}

do_record() {
  echo "[vvs2] Recording golden benchmarks for vertex_vector_v2..." >&2
  local rec_args=(record --engine vertex_vector_v2 --synthesis-models "$SYNTHESIS_MODELS" --judge-model "$JUDGE_MODEL")
  [[ -z "$QUESTIONS" ]] || rec_args+=(--questions "$QUESTIONS")
  [[ "$WRITE_QUERY_RUNS" != "true" ]] || rec_args+=(--write-query-runs)
  PYTHONPATH="${REPO_ROOT}/backend" timeout 3600 "$PY_BIN" "${SCRIPT_DIR}/record_scann_benchmarks.py" "${rec_args[@]}"
}

do_down() {
  local col="$(cfg VVS2_COLLECTION)"
  if [[ ! "$col" =~ ^rag-arena-ephem-[a-z0-9-]{1,42}$ ]]; then
    die "safety guard blocked teardown: VVS2_COLLECTION ($col) does not start with rag-arena-ephem-"
  fi
  echo "[vvs2] Tearing down DenseScann index $(cfg VVS2_INDEX_ID) and Collection ${col}..." >&2
  PYTHONPATH="${REPO_ROOT}/backend" timeout "$CMD_TIMEOUT" "$PY_BIN" - <<'PY'
import os, sys
from app.core.config import get_settings
from app.core.gcp_clients import get_access_token
from app.core.vvs2_client import Vvs2Client, Vvs2HttpError, Vvs2Resources

settings = get_settings({**os.environ, "DEMO_MODE": "live"})
res = Vvs2Resources.from_settings(settings)
if not res.collection_id.startswith("rag-arena-ephem-"):
    raise RuntimeError(f"Refusing to delete non-ephemeral collection: {res.collection_id}")
client = Vvs2Client(settings, token_provider=lambda: get_access_token(settings))

# 1. Delete DenseScann index first (ignore 404)
try:
    op_idx, _ = client.request("DELETE", res.index_name)
    if isinstance(op_idx.get("name"), str) and "/operations/" in op_idx["name"] and not op_idx.get("done"):
        client.wait_operation(op_idx["name"], timeout_seconds=1800.0)
except Vvs2HttpError as exc:
    if exc.status_code != 404:
        raise

# 2. Delete Collection with force=true so remaining DataObjects are purged cleanly (ignore 404)
try:
    op_col, _ = client.request("DELETE", res.collection_name, params={"force": "true"})
    if isinstance(op_col.get("name"), str) and "/operations/" in op_col["name"] and not op_col.get("done"):
        client.wait_operation(op_col["name"], timeout_seconds=2400.0)
except Vvs2HttpError as exc:
    if exc.status_code != 404:
        raise

# 3. Verify 0 rag-arena-ephem-* collections remain in this location
listed, _ = client.request("GET", f"{res.parent}/collections")
remaining = [
    str(c.get("name", "")).rsplit("/", 1)[-1]
    for c in (listed.get("collections") or [])
    if str(c.get("name", "")).rsplit("/", 1)[-1].startswith("rag-arena-ephem-")
]
if remaining:
    raise RuntimeError(f"Ephemeral VVS2 collections still present after teardown: {remaining}")
print('{"teardown":"verified_zero_ephemeral_collections"}')
PY
}

main() {
  parse_args "$@"
  load_env_file
  print_plan
  if [[ "$ACTION" == "dry-run" ]]; then
    echo "DRY RUN: zero GCP API calls executed."
    return 0
  fi
  export_runtime_env
  case "$ACTION" in
    up) do_up ;;
    record) do_record ;;
    down) do_down ;;
    full)
      trap 'echo "[vvs2] Running automatic EXIT teardown..." >&2; do_down' EXIT
      do_up
      do_record
      trap - EXIT
      do_down
      ;;
  esac
}

main "$@"
