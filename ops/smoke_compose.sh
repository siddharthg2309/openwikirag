#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s openwikirag-smoke-NAME\n' "$0" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage
project_name=$1
case "$project_name" in
  openwikirag-smoke-[A-Za-z0-9_-]*) ;;
  *)
    printf 'Project name must start with openwikirag-smoke-.\n' >&2
    exit 2
    ;;
esac

api_port=${OPENWIKIRAG_SMOKE_API_PORT:-18080}
postgres_port=${OPENWIKIRAG_SMOKE_POSTGRES_PORT:-15432}
redis_port=${OPENWIKIRAG_SMOKE_REDIS_PORT:-16381}
qdrant_http_port=${OPENWIKIRAG_SMOKE_QDRANT_HTTP_PORT:-16333}
qdrant_grpc_port=${OPENWIKIRAG_SMOKE_QDRANT_GRPC_PORT:-16334}

validate_port() {
  local name=$1
  local value=$2
  if ! [[ "$value" =~ ^[0-9]+$ ]] || (( value < 1024 || value > 65535 )); then
    printf '%s must be an integer between 1024 and 65535.\n' "$name" >&2
    exit 2
  fi
}

validate_port OPENWIKIRAG_SMOKE_API_PORT "$api_port"
validate_port OPENWIKIRAG_SMOKE_POSTGRES_PORT "$postgres_port"
validate_port OPENWIKIRAG_SMOKE_REDIS_PORT "$redis_port"
validate_port OPENWIKIRAG_SMOKE_QDRANT_HTTP_PORT "$qdrant_http_port"
validate_port OPENWIKIRAG_SMOKE_QDRANT_GRPC_PORT "$qdrant_grpc_port"

export OPENWIKIRAG_API_PORT="$api_port"
export OPENWIKIRAG_POSTGRES_PORT="$postgres_port"
export OPENWIKIRAG_REDIS_PORT="$redis_port"
export OPENWIKIRAG_QDRANT_HTTP_PORT="$qdrant_http_port"
export OPENWIKIRAG_QDRANT_GRPC_PORT="$qdrant_grpc_port"
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

for attempt in {1..30}; do
  if "${compose[@]}" logs --no-color worker 2>/dev/null | grep -q "worker_cycle_completed"; then
    printf 'Compose smoke passed for project %s.\n' "$project_name"
    exit 0
  fi
  sleep 1
done

printf 'Timed out waiting for a worker cycle.\n' >&2
"${compose[@]}" ps >&2
"${compose[@]}" logs --no-color --tail=80 worker >&2
exit 1
