#!/usr/bin/env bash
# Ephemeral Level 5 (Cloud SQL for PostgreSQL 16 + pgvector 0.8.5, `cloudsql_pgvector`) benchmark lifecycle:
#   scripts/ephemeral_cloudsql.sh --dry-run      # preview Cloud SQL Enterprise 2-vCPU/8GiB ZONAL lifecycle (zero calls)
#   scripts/ephemeral_cloudsql.sh --up --yes     # provision instance + loopback cloud-sql-proxy + load halfvec(3072) HNSW
#   scripts/ephemeral_cloudsql.sh --record --yes # record golden + ACL benchmarks into data/recordings/cloudsql_pgvector/
#   scripts/ephemeral_cloudsql.sh --down --yes   # stop proxy + delete rag-arena-ephem-* instance ($0/mo)
#   scripts/ephemeral_cloudsql.sh --full --yes   # --up -> --record -> automatic --down (via EXIT trap) -> $0/mo idle
#
# Safety invariants:
#   - Every created/deleted Cloud SQL instance MUST match `^rag-arena-ephem-[a-z0-9-]{1,42}$`.
#   - Local cloud-sql-proxy binds strictly to `127.0.0.1` (NEVER `0.0.0.0`).
#   - Ephemeral DB password lives only in memory/mode-0600 runtime state; never echoed or committed.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly PY_BIN="${REPO_ROOT}/.venv/bin/python"
readonly CMD_TIMEOUT=1800

ENV_FILE="${REPO_ROOT}/.env"
ACTION=""
APPROVED="false"
SYNTHESIS_MODELS="gemini-4-neon"
JUDGE_MODEL="claude-opus-5-5"
QUESTIONS=""
ACL_PERSONAS=""
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
      --acl-personas) (($# >= 2)) || die "--acl-personas requires CSV"; ACL_PERSONAS="$2"; shift 2 ;;
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

  [[ -n "${CFG[REGION]:-}" ]] || CFG[REGION]="us-central1"
  [[ -n "${CFG[EPHEM_CLOUDSQL_INSTANCE]:-}" ]] || CFG[EPHEM_CLOUDSQL_INSTANCE]="rag-arena-ephem-pgvec"
  [[ -n "${CFG[EPHEM_CLOUDSQL_TIER]:-}" ]] || CFG[EPHEM_CLOUDSQL_TIER]="db-custom-2-8192"
  [[ -n "${CFG[EPHEM_CLOUDSQL_PORT]:-}" ]] || CFG[EPHEM_CLOUDSQL_PORT]="15433"
  [[ -n "${CFG[CLOUDSQL_TABLE]:-}" ]] || CFG[CLOUDSQL_TABLE]="document_chunks"
  [[ -n "${CFG[BQ_DATASET]:-}" ]] || CFG[BQ_DATASET]="rag_arena"
  [[ -n "${CFG[BQ_TABLE]:-}" ]] || CFG[BQ_TABLE]="document_chunks_v3"

  [[ -n "$(cfg GCP_PROJECT_ID)" ]] || die "GCP_PROJECT_ID is required in $ENV_FILE"
  [[ "$(cfg GCP_PROJECT_ID)" =~ ^[a-z][a-z0-9-]{4,29}$ ]] || die "GCP_PROJECT_ID format is invalid"
  [[ "$(cfg EPHEM_CLOUDSQL_PORT)" =~ ^[0-9]{4,5}$ ]] || die "EPHEM_CLOUDSQL_PORT must be a local port in 1024..65535"

  local inst="$(cfg EPHEM_CLOUDSQL_INSTANCE)"
  if [[ ! "$inst" =~ ^rag-arena-ephem-[a-z0-9-]{1,42}$ ]]; then
    die "safety guard: Cloud SQL ephemeral instance ($inst) must match ^rag-arena-ephem-[a-z0-9-]{1,42}$"
  fi
}

state_file_path() {
  echo "${TMPDIR:-/tmp}/rag_arena_ephem_cloudsql_$(id -u).state"
}

print_plan() {
  local port="$(cfg EPHEM_CLOUDSQL_PORT)"
  echo "Ephemeral Level 5 (Cloud SQL PostgreSQL 16 + pgvector 0.8.5) lifecycle plan:"
  echo "  backend mode  : Cloud SQL Enterprise ZONAL $(cfg EPHEM_CLOUDSQL_TIER) ($(cfg REGION), 10 GB SSD)"
  echo "  instance      : $(cfg EPHEM_CLOUDSQL_INSTANCE) (public-ip + cloud-sql-proxy v2)"
  echo "  proxy bind    : 127.0.0.1:${port} (loopback only)"
  echo "  pg table      : $(cfg CLOUDSQL_TABLE) (halfvec(3072) HNSW + GIN FTS + Postgres RLS)"
  echo "  source vectors: BigQuery $(cfg BQ_DATASET).$(cfg BQ_TABLE) (tier_profile='hybrid', 0 re-embedding)"
  echo "  models        : synthesis=[${SYNTHESIS_MODELS}] judge=${JUDGE_MODEL}"
  echo "  teardown      : stop loopback proxy -> delete rag-arena-ephem-* instance -> verify 0 remain"
}

export_base_env() {
  local k
  for k in "${!CFG[@]}"; do
    export "${k}=${CFG[$k]}"
  done
  export CLOUDSQL_TABLE="$(cfg CLOUDSQL_TABLE)"
  [[ -x "$PY_BIN" ]] || die "Python virtualenv not found at $PY_BIN"
}

gc() {
  timeout "$CMD_TIMEOUT" gcloud "$@" --project="$(cfg GCP_PROJECT_ID)" --quiet
}

wait_for_pg_ready() {
  local attempts=0
  while ((attempts < 45)); do
    if PYTHONPATH="${REPO_ROOT}/backend" "$PY_BIN" - <<'PY' >/dev/null 2>&1
import os, psycopg
with psycopg.connect(os.environ["CLOUDSQL_DSN"], connect_timeout=3) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT 1;")
PY
    then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 2
  done
  die "Cloud SQL proxy endpoint on 127.0.0.1:$(cfg EPHEM_CLOUDSQL_PORT) did not become ready within 90s"
}

write_runtime_state() {
  local sf
  sf="$(state_file_path)"
  umask 077
  cat > "$sf" <<EOF
CLOUDSQL_DSN=${CLOUDSQL_DSN}
PROXY_PID=${PROXY_PID:-}
EOF
}

load_runtime_state() {
  local sf
  sf="$(state_file_path)"
  if [[ -f "$sf" ]]; then
    # shellcheck disable=SC1090
    source "$sf"
    export CLOUDSQL_DSN
  fi
}

stop_local_proxy() {
  local sf
  sf="$(state_file_path)"
  if [[ -f "$sf" ]]; then
    local pid=""
    pid="$(grep -E '^PROXY_PID=' "$sf" | cut -d= -f2- || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
    rm -f "$sf"
  fi
}

do_up() {
  command -v gcloud >/dev/null || die "gcloud not found on PATH"
  local proxy_bin="${CLOUD_SQL_PROXY_BIN:-$(command -v cloud-sql-proxy || true)}"
  [[ -n "$proxy_bin" && -x "$proxy_bin" ]] || die "cloud-sql-proxy binary not found on PATH (or set CLOUD_SQL_PROXY_BIN)"
  local port="$(cfg EPHEM_CLOUDSQL_PORT)"
  local inst="$(cfg EPHEM_CLOUDSQL_INSTANCE)"
  local region="$(cfg REGION)"
  local tier="$(cfg EPHEM_CLOUDSQL_TIER)"
  local pw
  pw="$("$PY_BIN" -c 'import secrets; print("Ephem9A" + secrets.token_hex(12))')"

  if ! gc sql instances describe "$inst" >/dev/null 2>&1; then
    echo "[cloudsql] Creating ephemeral ZONAL Enterprise PostgreSQL 16 instance ${inst} (${tier}) in ${region}..." >&2
    gc sql instances create "$inst" \
      --database-version=POSTGRES_16 \
      --edition=ENTERPRISE \
      --tier="$tier" \
      --region="$region" \
      --availability-type=zonal \
      --storage-type=SSD \
      --storage-size=10GB \
      --no-backup \
      --root-password="$pw"
  else
    echo "[cloudsql] Reusing existing ephemeral instance ${inst} and rotating postgres password..." >&2
    gc sql users set-password postgres --instance="$inst" --password="$pw"
  fi

  stop_local_proxy
  local conn_name="$(cfg GCP_PROJECT_ID):${region}:${inst}"
  echo "[cloudsql] Starting loopback cloud-sql-proxy on 127.0.0.1:${port}..." >&2
  "$proxy_bin" "$conn_name" --address 127.0.0.1 --port "$port" >/dev/null 2>&1 &
  PROXY_PID="$!"
  export CLOUDSQL_DSN="postgresql://postgres:${pw}@127.0.0.1:${port}/postgres"
  write_runtime_state

  wait_for_pg_ready
  echo "[cloudsql] Loading BigQuery 3072-d hybrid chunks + building pgvector halfvec HNSW, GIN FTS & RLS..." >&2
  PYTHONPATH="${REPO_ROOT}/backend" timeout "$CMD_TIMEOUT" "$PY_BIN" "${SCRIPT_DIR}/record_scann_benchmarks.py" load-cloudsql
}

do_record() {
  load_runtime_state
  [[ -n "${CLOUDSQL_DSN:-}" ]] || die "CLOUDSQL_DSN is unset — run --up first or pass --full --yes"
  echo "[cloudsql] Recording golden benchmarks for cloudsql_pgvector..." >&2
  local rec_args=(record --engine cloudsql_pgvector --synthesis-models "$SYNTHESIS_MODELS" --judge-model "$JUDGE_MODEL")
  [[ -z "$QUESTIONS" ]] || rec_args+=(--questions "$QUESTIONS")
  [[ -z "$ACL_PERSONAS" ]] || rec_args+=(--acl-personas "$ACL_PERSONAS")
  [[ "$WRITE_QUERY_RUNS" != "true" ]] || rec_args+=(--write-query-runs)
  PYTHONPATH="${REPO_ROOT}/backend" timeout 3600 "$PY_BIN" "${SCRIPT_DIR}/record_scann_benchmarks.py" "${rec_args[@]}"
}

do_down() {
  stop_local_proxy
  local inst="$(cfg EPHEM_CLOUDSQL_INSTANCE)"
  [[ "$inst" =~ ^rag-arena-ephem- ]] || die "safety guard blocked instance deletion: $inst"
  if gc sql instances describe "$inst" >/dev/null 2>&1; then
    echo "[cloudsql] Deleting ephemeral Cloud SQL instance ${inst}..." >&2
    gc sql instances delete "$inst"
  fi
  local rem
  rem="$(gc sql instances list --filter="name~^rag-arena-ephem-" --format="value(name)" 2>/dev/null || true)"
  [[ -z "$rem" ]] || die "residual ephemeral Cloud SQL instance(s) still present: $rem"
  echo "[cloudsql] Verified 0 residual rag-arena-ephem-* Cloud SQL instances (\$0/mo idle)." >&2
}

main() {
  parse_args "$@"
  load_env_file
  if [[ "$ACTION" == "dry-run" ]]; then
    print_plan
    return 0
  fi
  export_base_env
  case "$ACTION" in
    up) do_up ;;
    record) do_record ;;
    down) do_down ;;
    full)
      trap 'do_down' EXIT
      do_up
      do_record
      ;;
  esac
}

main "$@"
