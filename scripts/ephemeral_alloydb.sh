#!/usr/bin/env bash
# Ephemeral Level 7 (AlloyDB for PostgreSQL + ScaNN, `alloydb_scann`) benchmark lifecycle:
#   scripts/ephemeral_alloydb.sh --dry-run            # preview Cloud AlloyDB 2-vCPU ZONAL lifecycle (zero calls)
#   scripts/ephemeral_alloydb.sh --omni --dry-run     # preview local Docker AlloyDB Omni lifecycle (zero calls)
#   scripts/ephemeral_alloydb.sh --up --yes           # provision cluster/container + loopback proxy + load ScaNN table
#   scripts/ephemeral_alloydb.sh --record --yes       # record golden benchmarks into data/recordings/alloydb_scann/
#   scripts/ephemeral_alloydb.sh --down --yes         # stop proxy + delete rag-arena-ephem-* cluster/container ($0/mo)
#   scripts/ephemeral_alloydb.sh --full --yes         # --up -> --record -> automatic --down (via EXIT trap) -> $0/mo idle
#   scripts/ephemeral_alloydb.sh --omni --full --yes  # full lifecycle against local google/alloydbomni:16 ($0 GCP DB cost)
#
# Safety invariants:
#   - Every created/deleted cluster, instance, or Docker container MUST begin with `rag-arena-ephem-`.
#   - Local proxy and Docker port mappings bind strictly to `127.0.0.1` (NEVER `0.0.0.0`).
#   - Ephemeral DB password lives only in memory/mode-0600 runtime state; never echoed or written to repo files.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly PY_BIN="${REPO_ROOT}/.venv/bin/python"
readonly CMD_TIMEOUT=1800

ENV_FILE="${REPO_ROOT}/.env"
ACTION=""
APPROVED="false"
USE_OMNI="false"
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
      --omni) USE_OMNI="true"; shift ;;
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

  [[ -n "${CFG[REGION]:-}" ]] || CFG[REGION]="us-central1"
  [[ -n "${CFG[EPHEM_ALLOYDB_CLUSTER]:-}" ]] || CFG[EPHEM_ALLOYDB_CLUSTER]="rag-arena-ephem-scann"
  [[ -n "${CFG[EPHEM_ALLOYDB_INSTANCE]:-}" ]] || CFG[EPHEM_ALLOYDB_INSTANCE]="rag-arena-ephem-primary"
  [[ -n "${CFG[EPHEM_OMNI_CONTAINER]:-}" ]] || CFG[EPHEM_OMNI_CONTAINER]="rag-arena-ephem-omni"
  [[ -n "${CFG[EPHEM_OMNI_IMAGE]:-}" ]] || CFG[EPHEM_OMNI_IMAGE]="google/alloydbomni:16"
  [[ -n "${CFG[EPHEM_PG_PORT]:-}" ]] || CFG[EPHEM_PG_PORT]="15432"
  [[ -n "${CFG[ALLOYDB_TABLE]:-}" ]] || CFG[ALLOYDB_TABLE]="document_chunks"
  [[ -n "${CFG[BQ_DATASET]:-}" ]] || CFG[BQ_DATASET]="rag_arena"
  [[ -n "${CFG[BQ_TABLE]:-}" ]] || CFG[BQ_TABLE]="document_chunks_v3"

  [[ -n "$(cfg GCP_PROJECT_ID)" ]] || die "GCP_PROJECT_ID is required in $ENV_FILE"
  [[ "$(cfg GCP_PROJECT_ID)" =~ ^[a-z][a-z0-9-]{4,29}$ ]] || die "GCP_PROJECT_ID format is invalid"
  [[ "$(cfg EPHEM_PG_PORT)" =~ ^[0-9]{4,5}$ ]] || die "EPHEM_PG_PORT must be a local port in 1024..65535"

  local name
  for name in "$(cfg EPHEM_ALLOYDB_CLUSTER)" "$(cfg EPHEM_ALLOYDB_INSTANCE)" "$(cfg EPHEM_OMNI_CONTAINER)"; do
    if [[ ! "$name" =~ ^rag-arena-ephem-[a-z0-9-]{1,42}$ ]]; then
      die "safety guard: AlloyDB ephemeral resource ($name) must match ^rag-arena-ephem-[a-z0-9-]{1,42}$"
    fi
  done
}

state_file_path() {
  echo "${TMPDIR:-/tmp}/rag_arena_ephem_alloydb_$(id -u).state"
}

print_plan() {
  local port="$(cfg EPHEM_PG_PORT)"
  echo "Ephemeral Level 7 (AlloyDB for PostgreSQL + ScaNN) lifecycle plan:"
  if [[ "$USE_OMNI" == "true" ]]; then
    echo "  backend mode  : AlloyDB Omni Docker ($(cfg EPHEM_OMNI_IMAGE), container $(cfg EPHEM_OMNI_CONTAINER))"
    echo "  network bind  : 127.0.0.1:${port}:5432 (loopback only)"
  else
    echo "  backend mode  : Cloud AlloyDB ZONAL 2-vCPU ($(cfg REGION))"
    echo "  cluster/inst  : $(cfg EPHEM_ALLOYDB_CLUSTER) / $(cfg EPHEM_ALLOYDB_INSTANCE) (public-ip + alloydb-auth-proxy)"
    echo "  proxy bind    : 127.0.0.1:${port} (loopback only)"
  fi
  echo "  pg table      : $(cfg ALLOYDB_TABLE) (vector(3072) + alloydb_scann cosine + GIN FTS)"
  echo "  source vectors: BigQuery $(cfg BQ_DATASET).$(cfg BQ_TABLE) (tier_profile='hybrid', 0 re-embedding)"
  echo "  models        : synthesis=[${SYNTHESIS_MODELS}] judge=${JUDGE_MODEL}"
  echo "  teardown      : stop loopback proxy/container -> delete rag-arena-ephem-* cluster -> verify 0 remain"
}

export_base_env() {
  local k
  for k in "${!CFG[@]}"; do
    export "${k}=${CFG[$k]}"
  done
  export ALLOYDB_TABLE="$(cfg ALLOYDB_TABLE)"
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
with psycopg.connect(os.environ["ALLOYDB_DSN"], connect_timeout=3) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT 1;")
PY
    then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 2
  done
  die "PostgreSQL endpoint on 127.0.0.1:$(cfg EPHEM_PG_PORT) did not become ready within 90s"
}

write_runtime_state() {
  local sf
  sf="$(state_file_path)"
  umask 077
  cat > "$sf" <<EOF
ALLOYDB_DSN=${ALLOYDB_DSN}
PROXY_PID=${PROXY_PID:-}
USE_OMNI=${USE_OMNI}
EOF
}

load_runtime_state() {
  local sf
  sf="$(state_file_path)"
  if [[ -f "$sf" ]]; then
    # shellcheck disable=SC1090
    source "$sf"
    export ALLOYDB_DSN
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
  local port="$(cfg EPHEM_PG_PORT)"
  local pw
  pw="$("$PY_BIN" -c 'import secrets; print("Ephem9!" + secrets.token_hex(12))')"

  if [[ "$USE_OMNI" == "true" ]]; then
    command -v docker >/dev/null || die "docker not found on PATH for --omni mode"
    local cname="$(cfg EPHEM_OMNI_CONTAINER)"
    docker rm -f "$cname" >/dev/null 2>&1 || true
    echo "[alloydb-omni] Starting container ${cname} bound strictly to 127.0.0.1:${port}:5432..." >&2
    docker run -d \
      --name "$cname" \
      -e "POSTGRES_PASSWORD=${pw}" \
      -p "127.0.0.1:${port}:5432" \
      "$(cfg EPHEM_OMNI_IMAGE)" >/dev/null
    export ALLOYDB_DSN="postgresql://postgres:${pw}@127.0.0.1:${port}/postgres"
    PROXY_PID=""
    write_runtime_state
  else
    command -v gcloud >/dev/null || die "gcloud not found on PATH"
    local proxy_bin="${ALLOYDB_AUTH_PROXY_BIN:-$(command -v alloydb-auth-proxy || true)}"
    [[ -n "$proxy_bin" && -x "$proxy_bin" ]] || die "alloydb-auth-proxy binary not found on PATH (or set ALLOYDB_AUTH_PROXY_BIN)"
    local cluster="$(cfg EPHEM_ALLOYDB_CLUSTER)"
    local instance="$(cfg EPHEM_ALLOYDB_INSTANCE)"
    local region="$(cfg REGION)"

    if ! gc alloydb clusters describe "$cluster" --region="$region" >/dev/null 2>&1; then
      echo "[alloydb] Creating ephemeral ZONAL cluster ${cluster} in ${region}..." >&2
      gc alloydb clusters create "$cluster" \
        --region="$region" \
        --network="$(cfg EPHEM_ALLOYDB_NETWORK default)" \
        --password="$pw" \
        --database-version=POSTGRES_16
    else
      echo "[alloydb] Reusing existing ephemeral cluster ${cluster} and rotating password for session..." >&2
      gc alloydb users set-password postgres --cluster="$cluster" --region="$region" --password="$pw"
    fi

    if ! gc alloydb instances describe "$instance" --cluster="$cluster" --region="$region" >/dev/null 2>&1; then
      echo "[alloydb] Creating ephemeral 2-vCPU ZONAL primary instance ${instance} (public-ip + auth-proxy)..." >&2
      gc alloydb instances create "$instance" \
        --cluster="$cluster" \
        --region="$region" \
        --instance-type=PRIMARY \
        --cpu-count=2 \
        --availability-type=ZONAL \
        --assign-inbound-public-ip=ASSIGN_IPV4 \
        --database-flags=alloydb.iam_authentication=on,password.enforce_complexity=on
    fi

    stop_local_proxy
    local inst_uri="projects/$(cfg GCP_PROJECT_ID)/locations/${region}/clusters/${cluster}/instances/${instance}"
    echo "[alloydb] Starting loopback alloydb-auth-proxy on 127.0.0.1:${port}..." >&2
    "$proxy_bin" "$inst_uri" --public-ip --address 127.0.0.1 --port "$port" >/dev/null 2>&1 &
    PROXY_PID="$!"
    export ALLOYDB_DSN="postgresql://postgres:${pw}@127.0.0.1:${port}/postgres"
    write_runtime_state
  fi

  wait_for_pg_ready
  echo "[alloydb] Loading BigQuery 3072-d hybrid chunks + building ScaNN cosine & GIN FTS indexes..." >&2
  PYTHONPATH="${REPO_ROOT}/backend" timeout "$CMD_TIMEOUT" "$PY_BIN" "${SCRIPT_DIR}/record_scann_benchmarks.py" load-alloydb
}

do_record() {
  load_runtime_state
  [[ -n "${ALLOYDB_DSN:-}" ]] || die "ALLOYDB_DSN is unset — run --up first or pass --full --yes"
  echo "[alloydb] Recording golden benchmarks for alloydb_scann..." >&2
  local rec_args=(record --engine alloydb_scann --synthesis-models "$SYNTHESIS_MODELS" --judge-model "$JUDGE_MODEL")
  [[ -z "$QUESTIONS" ]] || rec_args+=(--questions "$QUESTIONS")
  [[ "$WRITE_QUERY_RUNS" != "true" ]] || rec_args+=(--write-query-runs)
  PYTHONPATH="${REPO_ROOT}/backend" timeout 3600 "$PY_BIN" "${SCRIPT_DIR}/record_scann_benchmarks.py" "${rec_args[@]}"
}

do_down() {
  stop_local_proxy
  if [[ "$USE_OMNI" == "true" ]]; then
    local cname="$(cfg EPHEM_OMNI_CONTAINER)"
    [[ "$cname" =~ ^rag-arena-ephem- ]] || die "safety guard blocked container removal: $cname"
    echo "[alloydb-omni] Removing container ${cname}..." >&2
    docker rm -f -v "$cname" >/dev/null 2>&1 || true
    local rem
    rem="$(docker ps -a --filter "name=^/${cname}$" --format '{{.Names}}' || true)"
    [[ -z "$rem" ]] || die "Omni container still exists after teardown: $rem"
    echo '{"teardown":"verified_zero_omni_containers"}'
  else
    local cluster="$(cfg EPHEM_ALLOYDB_CLUSTER)"
    local region="$(cfg REGION)"
    [[ "$cluster" =~ ^rag-arena-ephem- ]] || die "safety guard blocked cluster deletion: $cluster"
    if gc alloydb clusters describe "$cluster" --region="$region" >/dev/null 2>&1; then
      echo "[alloydb] Deleting ephemeral cluster ${cluster} (--force)..." >&2
      gc alloydb clusters delete "$cluster" --region="$region" --force
    fi
    local rem
    rem="$(gc alloydb clusters list --region="$region" --format="value(name)" 2>/dev/null | grep -E 'rag-arena-ephem-' || true)"
    [[ -z "${rem//[[:space:]]/}" ]] || die "Ephemeral AlloyDB clusters still exist after teardown: $rem"
    echo '{"teardown":"verified_zero_ephemeral_clusters"}'
  fi
}

main() {
  parse_args "$@"
  load_env_file
  print_plan
  if [[ "$ACTION" == "dry-run" ]]; then
    echo "DRY RUN: zero GCP or Docker commands executed."
    return 0
  fi
  export_base_env
  case "$ACTION" in
    up) do_up ;;
    record) do_record ;;
    down) do_down ;;
    full)
      trap 'echo "[alloydb] Running automatic EXIT teardown..." >&2; do_down' EXIT
      do_up
      do_record
      trap - EXIT
      do_down
      ;;
  esac
}

main "$@"
