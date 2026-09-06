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
storage_backend=${OPENWIKIRAG_SMOKE_OBJECT_STORAGE_BACKEND:-filesystem}
minio_api_port=${OPENWIKIRAG_SMOKE_MINIO_API_PORT:-19000}
minio_console_port=${OPENWIKIRAG_SMOKE_MINIO_CONSOLE_PORT:-19001}
minio_bucket=${OPENWIKIRAG_SMOKE_OBJECT_STORE_BUCKET:-openwikirag-smoke}
minio_access_key=${MINIO_ROOT_USER:-openwikirag}
minio_secret_key=${MINIO_ROOT_PASSWORD:-openwikirag-dev-password}

case "$storage_backend" in
  filesystem|s3) ;;
  *)
    printf 'OPENWIKIRAG_SMOKE_OBJECT_STORAGE_BACKEND must be filesystem or s3.\n' >&2
    exit 2
    ;;
esac

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
if [[ "$storage_backend" == "s3" ]]; then
  validate_port OPENWIKIRAG_SMOKE_MINIO_API_PORT "$minio_api_port"
  validate_port OPENWIKIRAG_SMOKE_MINIO_CONSOLE_PORT "$minio_console_port"
fi

export OPENWIKIRAG_API_PORT="$api_port"
export OPENWIKIRAG_POSTGRES_PORT="$postgres_port"
export OPENWIKIRAG_REDIS_PORT="$redis_port"
export OPENWIKIRAG_QDRANT_HTTP_PORT="$qdrant_http_port"
export OPENWIKIRAG_QDRANT_GRPC_PORT="$qdrant_grpc_port"
export OPENWIKIRAG_MINIO_API_PORT="$minio_api_port"
export OPENWIKIRAG_MINIO_CONSOLE_PORT="$minio_console_port"
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

services=(postgres redis qdrant)
if [[ "$storage_backend" == "s3" ]]; then
  export OPENWIKIRAG_OBJECT_STORAGE_BACKEND=s3
  export OPENWIKIRAG_OBJECT_STORE_BUCKET="$minio_bucket"
  export OPENWIKIRAG_OBJECT_STORE_ENDPOINT_URL=http://minio:9000
  export OPENWIKIRAG_OBJECT_STORE_ACCESS_KEY_ID="$minio_access_key"
  export OPENWIKIRAG_OBJECT_STORE_SECRET_ACCESS_KEY="$minio_secret_key"
  export OPENWIKIRAG_OBJECT_STORE_PATH_STYLE=true
  services+=(minio)
fi

"${compose[@]}" up --detach --build "${services[@]}"
wait_for_url "http://127.0.0.1:${qdrant_http_port}/healthz" "Qdrant"
if [[ "$storage_backend" == "s3" ]]; then
  wait_for_url "http://127.0.0.1:${minio_api_port}/minio/health/live" "MinIO"
fi
"${compose[@]}" up --build migrate
"${compose[@]}" up --detach api worker
wait_for_url "http://127.0.0.1:${api_port}/healthz" "API liveness"

for attempt in {1..30}; do
  if "${compose[@]}" logs --no-color worker 2>/dev/null | grep -q "worker_cycle_completed"; then
    printf 'Compose smoke passed for project %s (storage=%s).\n' "$project_name" "$storage_backend"
    exit 0
  fi
  sleep 1
done

printf 'Timed out waiting for a worker cycle.\n' >&2
"${compose[@]}" ps >&2
"${compose[@]}" logs --no-color --tail=80 worker >&2
exit 1
