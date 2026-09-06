#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  printf 'Usage: %s /absolute/path/to/new-backup-directory\n' "$0" >&2
  exit 2
fi

backup_dir=$1
case "$backup_dir" in
  /*) ;;
  *)
    printf 'Backup destination must be an absolute path.\n' >&2
    exit 2
    ;;
esac

if [[ -e "$backup_dir" ]]; then
  printf 'Backup destination already exists; refusing to overwrite it.\n' >&2
  exit 2
fi

mkdir -p "$backup_dir"

database_user=${POSTGRES_USER:-openwikirag}
database_name=${POSTGRES_DB:-openwikirag}
docker compose exec -T postgres \
  pg_dump \
    --username "$database_user" \
    --dbname "$database_name" \
    --format=custom \
    --no-owner \
    --no-privileges \
  > "$backup_dir/postgres.dump"

api_container=$(docker compose ps -q api)
if [[ -z "$api_container" ]]; then
  printf 'A running Compose API container is required to discover the object volume.\n' >&2
  exit 1
fi

object_volume=$(docker inspect "$api_container" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')
if [[ -z "$object_volume" ]]; then
  printf 'The API container has no named /data object volume.\n' >&2
  exit 1
fi

docker run --rm \
  --volume "$object_volume:/data:ro" \
  --volume "$backup_dir:/backup" \
  alpine:3.20 \
  tar -czf /backup/objects.tar.gz -C /data .

hash_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

file_size() {
  if stat -f %z "$1" >/dev/null 2>&1; then
    stat -f %z "$1"
  else
    stat -c %s "$1"
  fi
}

postgres_checksum=$(hash_file "$backup_dir/postgres.dump")
objects_checksum=$(hash_file "$backup_dir/objects.tar.gz")

cat > "$backup_dir/manifest.txt" <<EOF
schema_version=local-backup-manifest-v1
scope=compose-postgresql-and-filesystem-object-volume
database_name=$database_name
postgres_dump=postgres.dump
postgres_dump_format=custom
postgres_dump_bytes=$(file_size "$backup_dir/postgres.dump")
postgres_dump_sha256=$postgres_checksum
object_snapshot=objects.tar.gz
object_snapshot_bytes=$(file_size "$backup_dir/objects.tar.gz")
object_snapshot_sha256=$objects_checksum
object_volume=$object_volume
EOF

printf 'Backup bundle created: %s\n' "$backup_dir"
