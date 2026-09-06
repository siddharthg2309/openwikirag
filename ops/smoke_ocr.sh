#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s [image-name]\n' "$0" >&2
  exit 2
}

[[ $# -le 1 ]] || usage
image_name=${1:-${OPENWIKIRAG_OCR_IMAGE:-openwikirag:local}}
[[ -n "$image_name" ]] || {
  printf 'An OCR image name is required.\n' >&2
  exit 2
}

script_path=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/ocr_native_smoke.py
[[ -f "$script_path" ]] || {
  printf 'OCR smoke script is missing: %s\n' "$script_path" >&2
  exit 1
}

docker image inspect "$image_name" >/dev/null
docker run --rm \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --volume "$script_path:/workspace/ocr_native_smoke.py:ro" \
  --env PYTHONPATH=/app/src \
  --entrypoint python \
  "$image_name" \
  /workspace/ocr_native_smoke.py
