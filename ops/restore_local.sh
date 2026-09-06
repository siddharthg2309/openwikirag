#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s /absolute/path/to/backup --database-container TARGET --object-volume TARGET_VOLUME --confirm-target TARGET:TARGET_VOLUME\n' "$0" >&2
  exit 2
}

[[ $# -eq 7 ]] || usage
backup_dir=$1
[[ $2 == "--database-container" ]] || usage
database_container=$3
[[ $4 == "--object-volume" ]] || usage
object_volume=$5
[[ $6 == "--confirm-target" ]] || usage
confirmed_target=$7

case "$backup_dir" in
  /*) ;;
  *) printf 'Backup directory must be an absolute path.\n' >&2; exit 2 ;;
esac
[[ -d "$backup_dir" ]] || { printf 'Backup directory does not exist.\n' >&2; exit 2; }
[[ -n "$database_container" && "$database_container" != -* ]] || { printf 'Database target is invalid.\n' >&2; exit 2; }
[[ -n "$object_volume" && "$object_volume" != -* ]] || { printf 'Object-volume target is invalid.\n' >&2; exit 2; }
[[ "$confirmed_target" == "$database_container:$object_volume" ]] || {
  printf 'Target confirmation must match the database container and object volume.\n' >&2
  exit 2
}

manifest="$backup_dir/manifest.txt"
postgres_dump="$backup_dir/postgres.dump"
objects_archive="$backup_dir/objects.tar.gz"
[[ -f "$manifest" && -f "$postgres_dump" && -f "$objects_archive" ]] || {
  printf 'Backup bundle is incomplete.\n' >&2
  exit 1
}

manifest_value() {
  key=$1
  awk -F= -v wanted="$key" '$1 == wanted {print substr($0, index($0, "=") + 1); found=1} END {if (!found) exit 1}' "$manifest"
}

hash_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

expected_postgres=$(manifest_value postgres_dump_sha256)
expected_objects=$(manifest_value object_snapshot_sha256)
actual_postgres=$(hash_file "$postgres_dump")
actual_objects=$(hash_file "$objects_archive")
[[ $actual_postgres == "$expected_postgres" ]] || {
  printf 'PostgreSQL dump checksum mismatch.\n' >&2
  exit 1
}
[[ $actual_objects == "$expected_objects" ]] || {
  printf 'Object snapshot checksum mismatch.\n' >&2
  exit 1
}

docker inspect "$database_container" >/dev/null 2>&1 || {
  printf 'Database target container does not exist.\n' >&2
  exit 1
}
docker volume inspect "$object_volume" >/dev/null 2>&1 || {
  printf 'Object target volume does not exist.\n' >&2
  exit 1
}

object_entries=$(docker run --rm \
  --volume "$object_volume:/data:ro" \
  alpine:3.20 \
  sh -c 'find /data -mindepth 1 -print -quit')
[[ -z "$object_entries" ]] || {
  printf 'Object target volume must be empty.\n' >&2
  exit 1
}

database_user=${POSTGRES_USER:-openwikirag}
database_name=${POSTGRES_DB:-openwikirag}
docker exec -i "$database_container" \
  pg_restore \
  --username "$database_user" \
  --dbname "$database_name" \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  < "$postgres_dump"

docker run --rm \
  --volume "$object_volume:/data" \
  --volume "$backup_dir:/backup:ro" \
  alpine:3.20 \
  sh -c 'tar -xzf /backup/objects.tar.gz -C /data'

printf 'Restore completed into database container %s and object volume %s.\n' \
  "$database_container" "$object_volume"
