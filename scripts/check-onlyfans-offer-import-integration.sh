#!/usr/bin/env bash
# Offline offer-import + content-sync integration: disposable PostgreSQL, the real
# native API/worker, the dashboard (next dev) and the browser checks, through the
# existing test_onlyfans_http_pg.py harness. Synthetic data only; never reads a
# production .env or credential. Removes only the container it started.
#
#   bash scripts/check-onlyfans-offer-import-integration.sh --dashboard-dir /abs/path/to/twin-dashboard
#
# Fails on a missing checkout, an unavailable fixture server or ANY skipped test.
set -euo pipefail

dashboard=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dashboard-dir) dashboard="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$dashboard" ] || { echo "--dashboard-dir is required" >&2; exit 2; }
case "$dashboard" in /*) ;; *) echo "--dashboard-dir must be absolute" >&2; exit 2 ;; esac
for f in package.json scripts/check-onlyfans-http-browser.mjs scripts/check-onlyfans-offer-import-browser.mjs \
         preview/onlyfans-integration/package.json node_modules/next/dist/bin/next; do
  [ -e "$dashboard/$f" ] || { echo "dashboard checkout incomplete: missing $f (run npm ci?)" >&2; exit 2; }
done
if ls "$dashboard"/preview/onlyfans-integration/.env* >/dev/null 2>&1; then
  echo "refusing: an .env file exists in the integration harness" >&2; exit 2
fi

root="$(cd "$(dirname "$0")/.." && pwd)"
python="${PYTHON:-$root/.venv/bin/python}"
[ -x "$python" ] || python="$(command -v python3)"
evidence="${ONLYFANS_TEST_EVIDENCE:-$root/offer-import-evidence/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$evidence"

# Disposable PostgreSQL unless the caller provides one.
container=""
cleanup() { [ -z "$container" ] || docker rm -f "$container" >/dev/null 2>&1 || true; }
trap cleanup EXIT
if [ -z "${ONLYFANS_TEST_PG_URL:-}" ]; then
  container="offer-import-it-$$"
  port=$(( 40000 + RANDOM % 20000 ))
  docker run -d --name "$container" -e POSTGRES_PASSWORD=synthetic-it-only -p "127.0.0.1:$port:5432" \
    --tmpfs /var/lib/postgresql/data:rw,size=4g postgres:16 \
    -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=300 >/dev/null
  for _ in $(seq 1 60); do docker exec "$container" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1 && break; sleep 1; done
  docker exec "$container" pg_isready -h 127.0.0.1 -U postgres >/dev/null || { echo "fixture database unavailable" >&2; exit 1; }
  export ONLYFANS_TEST_PG_URL="postgresql+psycopg2://postgres:synthetic-it-only@127.0.0.1:$port/postgres"
fi

export PYTHONPATH="$root/service:$root"
export ONLYFANS_TEST_DASHBOARD="$dashboard" ONLYFANS_TEST_EVIDENCE="$evidence"
export ONLYFANS_TEST_CONTENT=1 ONLYFANS_TEST_OFFER_IMPORT=1 ONLYFANS_TEST_CONTENT_SYNC=1

suites=(
  service/tests/test_onlyfans_offer_import.py
  service/tests/test_onlyfans_offer_import_pg.py
  service/tests/test_onlyfans_fixed_price_pg.py
  service/tests/test_onlyfans_offer_import_upgrade_pg.py
  service/tests/test_onlyfans_content_sync_pg.py
  service/tests/test_onlyfans_content_sync_delivery_pg.py
  service/tests/test_onlyfans_http_pg.py
)
cd "$root"
set +e
"$python" -m pytest -q -rs -p no:cacheprovider --junitxml="$evidence/junit.xml" "${suites[@]}" 2>&1 | tee "$evidence/pytest.log"
status=${PIPESTATUS[0]}
set -e
summary=$(tail -n 1 "$evidence/pytest.log")
echo "evidence: $evidence"
[ "$status" -eq 0 ] || { echo "FAILED: $summary" >&2; exit "$status"; }
if grep -qE '[0-9]+ skipped' <<<"$summary"; then
  echo "FAILED: required tests were skipped: $summary" >&2; exit 1
fi
echo "PASSED: $summary"
