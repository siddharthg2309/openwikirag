#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s openwikirag-demo-NAME\n' "$0" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage
project_name=$1
case "$project_name" in
  openwikirag-demo-[A-Za-z0-9_-]*) ;;
  *)
    printf 'Project name must start with openwikirag-demo-.\n' >&2
    exit 2
    ;;
esac

api_port=${OPENWIKIRAG_DEMO_API_PORT:-28080}
postgres_port=${OPENWIKIRAG_DEMO_POSTGRES_PORT:-25432}
redis_port=${OPENWIKIRAG_DEMO_REDIS_PORT:-26381}
qdrant_http_port=${OPENWIKIRAG_DEMO_QDRANT_HTTP_PORT:-26333}
qdrant_grpc_port=${OPENWIKIRAG_DEMO_QDRANT_GRPC_PORT:-26334}
timeout_seconds=${OPENWIKIRAG_DEMO_TIMEOUT_SECONDS:-180}

validate_port() {
  local name=$1
  local value=$2
  if ! [[ "$value" =~ ^[0-9]+$ ]] || (( value < 1024 || value > 65535 )); then
    printf '%s must be an integer between 1024 and 65535.\n' "$name" >&2
    exit 2
  fi
}

validate_port OPENWIKIRAG_DEMO_API_PORT "$api_port"
validate_port OPENWIKIRAG_DEMO_POSTGRES_PORT "$postgres_port"
validate_port OPENWIKIRAG_DEMO_REDIS_PORT "$redis_port"
validate_port OPENWIKIRAG_DEMO_QDRANT_HTTP_PORT "$qdrant_http_port"
validate_port OPENWIKIRAG_DEMO_QDRANT_GRPC_PORT "$qdrant_grpc_port"
if ! [[ "$timeout_seconds" =~ ^[0-9]+$ ]] || (( timeout_seconds < 10 || timeout_seconds > 300 )); then
  printf 'OPENWIKIRAG_DEMO_TIMEOUT_SECONDS must be an integer between 10 and 300.\n' >&2
  exit 2
fi

export OPENWIKIRAG_API_PORT="$api_port"
export OPENWIKIRAG_POSTGRES_PORT="$postgres_port"
export OPENWIKIRAG_REDIS_PORT="$redis_port"
export OPENWIKIRAG_QDRANT_HTTP_PORT="$qdrant_http_port"
export OPENWIKIRAG_QDRANT_GRPC_PORT="$qdrant_grpc_port"

# Force the disposable run onto deterministic local providers even if the
# developer's shell or .env file configures an optional external provider.
export OPENWIKIRAG_ENVIRONMENT=development
export OPENWIKIRAG_AUTH_MODE=local
export OPENWIKIRAG_OBJECT_STORAGE_BACKEND=filesystem
export OPENWIKIRAG_OBJECT_STORE_ENDPOINT_URL=
export OPENWIKIRAG_OBJECT_STORE_ACCESS_KEY_ID=
export OPENWIKIRAG_OBJECT_STORE_SECRET_ACCESS_KEY=
export OPENWIKIRAG_GRAPH_ENABLED=false
export OPENWIKIRAG_WIKI_GENERATION_MODEL=
export OPENWIKIRAG_WIKI_GENERATION_MODEL_DIGEST=
export OPENWIKIRAG_DENSE_EMBEDDING_MODEL=
export OPENWIKIRAG_DENSE_EMBEDDING_MODEL_DIGEST=
export OPENWIKIRAG_ANSWER_MODEL=
export OPENWIKIRAG_ANSWER_MODEL_DIGEST=

compose=(docker compose --project-name "$project_name")

cleanup() {
  local status=$?
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

wait_for_url() {
  local url=$1
  local description=$2
  for attempt in {1..60}; do
    if curl --fail --silent --show-error "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  printf 'Timed out waiting for %s.\n' "$description" >&2
  "${compose[@]}" ps >&2
  "${compose[@]}" logs --no-color --tail=80 >&2
  return 1
}

"${compose[@]}" up --detach --build postgres redis qdrant
wait_for_url "http://127.0.0.1:${qdrant_http_port}/healthz" "Qdrant"
"${compose[@]}" up --build migrate
"${compose[@]}" up --detach api worker
wait_for_url "http://127.0.0.1:${api_port}/healthz" "API liveness"

uv run --no-sync python ops/non_voice_demo.py \
  --base-url "http://127.0.0.1:${api_port}" \
  --timeout-seconds "$timeout_seconds"
