#!/usr/bin/env bash
# Local, offline OnlyFans-engine dev setup. Idempotent. Never touches shared DBs or prod.
# Usage: ./init.sh   (then copy the printed env line in front of pytest; see docs/learn/local-run-backend.md)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PG_NAME=twin-of-test-pg PG_PORT=55432
PG_URL="postgresql+psycopg2://postgres@127.0.0.1:${PG_PORT}/postgres"

# 1. throwaway Postgres (docker). Data is disposable; fsync off on purpose.
if ! docker info >/dev/null 2>&1; then echo "docker daemon not reachable (open -a Docker, wait)"; exit 1; fi
if docker ps --format '{{.Names}}' | grep -qx "$PG_NAME"; then echo "pg: $PG_NAME already running"
elif docker ps -a --format '{{.Names}}' | grep -qx "$PG_NAME"; then docker start "$PG_NAME" >/dev/null; echo "pg: started existing $PG_NAME"
else docker run -d --name "$PG_NAME" -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:${PG_PORT}:5432 postgres:16 -c fsync=off -c max_connections=300 >/dev/null; echo "pg: created $PG_NAME"; fi
for _ in $(seq 1 30); do docker exec "$PG_NAME" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
docker exec "$PG_NAME" pg_isready -U postgres

# 2. venv (requirements-onlyfans-test.txt = requirements.txt + pytest)
if [ ! -x "$ROOT/.venv/bin/python" ]; then python3 -m venv "$ROOT/.venv"; fi
"$ROOT/.venv/bin/python" -c 'import pytest, psycopg2, fastapi, uvicorn' 2>/dev/null \
  || nice -n 10 "$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements-onlyfans-test.txt"
echo "venv: ok ($("$ROOT/.venv/bin/python" --version))"

# 3. app database for booting the API process (schema via db.schema bootstrap, per CLAUDE.md no alembic by hand)
docker exec "$PG_NAME" psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='twin_of_app'" | grep -q 1 \
  || docker exec "$PG_NAME" psql -U postgres -qc 'CREATE DATABASE twin_of_app'
( cd "$ROOT/service" && PYTHONPATH=.:.. ONLYFANS_MIGRATION_URL="postgresql://postgres@127.0.0.1:${PG_PORT}/twin_of_app" \
  "$ROOT/.venv/bin/python" -m db.schema check >/dev/null 2>&1 \
  || PYTHONPATH=.:.. ONLYFANS_MIGRATION_URL="postgresql://postgres@127.0.0.1:${PG_PORT}/twin_of_app" nice -n 10 "$ROOT/.venv/bin/python" -m db.schema bootstrap | tail -1 )

cat <<EOT

Test env (run from service/tests):
  . .venv/bin/activate; cd service/tests
  ONLYFANS_TEST_PG_URL=${PG_URL} ONLYFANS_TEST_PG_EMPTY_TEMPLATE=1 PYTHONPATH=../..:..:. nice -n 10 python -m pytest -x -q <file>
API boot (offline, dummy tokens): see docs/learn/local-run-backend.md
EOT
