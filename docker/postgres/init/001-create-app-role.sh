#!/usr/bin/env bash
set -euo pipefail

role_exists="$(
  psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --tuples-only \
    --command "SELECT 1 FROM pg_roles WHERE rolname = 'openwikirag_app'"
)"

if [ -z "${role_exists//[[:space:]]/}" ]; then
  password_sql=${POSTGRES_APP_PASSWORD//\'/\'\'}
  psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" <<EOSQL
CREATE ROLE openwikirag_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$password_sql';
EOSQL
fi

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" <<EOSQL
GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO openwikirag_app;
GRANT USAGE ON SCHEMA public TO openwikirag_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO openwikirag_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO openwikirag_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO openwikirag_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO openwikirag_app;
EOSQL
