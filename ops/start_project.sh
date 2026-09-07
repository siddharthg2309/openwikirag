#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: ops/start_project.sh [--all] [PROJECT_NAME]

Start the persistent local OpenWikiRAG Compose project. PROJECT_NAME defaults
to "openwikirag" and must be "openwikirag" or start with "openwikirag-".
Use --all to also start the optional Neo4j and MinIO containers.
EOF
  exit 2
}

project_name=openwikirag
start_optional=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)
      start_optional=true
      shift
      ;;
    --help|-h)
      usage
      ;;
    --*)
      printf 'Unknown option: %s\n' "$1" >&2
      usage
      ;;
    *)
      [[ "$project_name" == openwikirag ]] || usage
      project_name=$1
      shift
      ;;
  esac
done

case "$project_name" in
  openwikirag|openwikirag-[A-Za-z0-9_-]*) ;;
  *)
    printf 'Project name must be openwikirag or start with openwikirag-.\n' >&2
    exit 2
    ;;
esac

api_port=${OPENWIKIRAG_API_PORT:-8000}
qdrant_http_port=${OPENWIKIRAG_QDRANT_HTTP_PORT:-6333}

validate_port() {
  local name=$1
  local value=$2
  if ! [[ "$value" =~ ^[0-9]+$ ]] || (( value < 1024 || value > 65535 )); then
    printf '%s must be an integer between 1024 and 65535.\n' "$name" >&2
    exit 2
  fi
}

validate_port OPENWIKIRAG_API_PORT "$api_port"
validate_port OPENWIKIRAG_QDRANT_HTTP_PORT "$qdrant_http_port"

command -v docker >/dev/null 2>&1 || {
  printf 'Docker is required but was not found on PATH.\n' >&2
  exit 1
}
command -v curl >/dev/null 2>&1 || {
  printf 'curl is required for startup readiness checks but was not found on PATH.\n' >&2
  exit 1
}

compose=(docker compose --project-name "$project_name")

show_failure() {
  printf '\nCompose status for %s:\n' "$project_name" >&2
  "${compose[@]}" ps >&2 || true
  printf '\nRecent Compose logs:\n' >&2
  "${compose[@]}" logs --no-color --tail=80 >&2 || true
}

wait_for_url() {
  local url=$1
  local description=$2
  for _ in {1..60}; do
    if curl --fail --silent --show-error "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  printf 'Timed out waiting for %s.\n' "$description" >&2
  show_failure
  return 1
}

wait_for_worker() {
  for _ in {1..60}; do
    local worker_id
    worker_id=$("${compose[@]}" ps -q worker 2>/dev/null || true)
    if [[ -n "$worker_id" ]]; then
      local status
      status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$worker_id" 2>/dev/null || true)
      if [[ "$status" == healthy ]]; then
        return 0
      fi
    fi
    sleep 1
  done
  printf 'Timed out waiting for worker readiness.\n' >&2
  show_failure
  return 1
}

services=(postgres redis qdrant)
if [[ "$start_optional" == true ]]; then
  services+=(neo4j minio)
fi

"${compose[@]}" up --detach --build "${services[@]}"
wait_for_url "http://127.0.0.1:${qdrant_http_port}/healthz" "Qdrant"

if ! "${compose[@]}" up --build migrate; then
  printf 'Database migration or role bootstrap failed.\n' >&2
  show_failure
  exit 1
fi

if ! "${compose[@]}" up --detach --build api worker; then
  printf 'API or worker startup failed.\n' >&2
  show_failure
  exit 1
fi

wait_for_url "http://127.0.0.1:${api_port}/healthz" "API liveness"
wait_for_worker

"${compose[@]}" ps
printf '\nOpenWikiRAG is running with Compose project %s.\n' "$project_name"
printf 'API: http://127.0.0.1:%s\n' "$api_port"
printf 'Qdrant: http://127.0.0.1:%s\n' "$qdrant_http_port"
printf 'Stop without deleting data: docker compose --project-name %s stop\n' "$project_name"
