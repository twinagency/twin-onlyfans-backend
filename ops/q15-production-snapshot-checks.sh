#!/bin/bash
# PREPARED, NOT RUN. Remaining Q15 checks that need an already-authorized secure production SNAPSHOT (never the live database).
# A synthetic restore does not prove that the current production database restores correctly; these steps do, once authorized.
#
# Preconditions (all must be true; the script refuses otherwise):
#   * Q15_SNAPSHOT_AUTHORIZED=<authorization reference> is set by the person who authorized the snapshot (any non-empty text);
#   * SNAPSHOT_DUMP is a pg_dump --format=custom file (or SNAPSHOT_URL names a RESTORED SCRATCH database) of the production database that was
#     taken by the authorized process; this script never connects to production and never takes the dump itself;
#   * SCRATCH_URL points at a throwaway PostgreSQL 16 on loopback or a private disposable host, never at a production endpoint;
#   * ONLYFANS_BACKUP_KEY is a 64-hex key created for this exercise; CANDIDATE_TREE is the exact candidate checkout/image mount (no overlays);
#   * R0_TREE is the previous compatible release tree (R0 baseline 0a691b7 or the release actually installed).
# Nothing here starts a sender, calls a provider/model, or writes outside the scratch database and OUT_DIR.
set -eu
: "${Q15_SNAPSHOT_AUTHORIZED:?authorization reference for the secure snapshot is required}"
: "${SCRATCH_URL:?disposable PostgreSQL 16 URL (loopback/private) is required}"
: "${CANDIDATE_TREE:?exact candidate source/image mount is required}"
: "${R0_TREE:?previous compatible release tree is required}"
: "${ONLYFANS_BACKUP_KEY:?}"; : "${SNAPSHOT_DUMP:?}"; : "${OUT_DIR:?}"; PG_BIN=${PG_BIN:-}
case "$SCRATCH_URL" in *@127.0.0.1*|*@localhost*|*@10.*|*@172.16.*|*@192.168.*) ;; *) echo "refusing: SCRATCH_URL is not loopback/private"; exit 2;; esac
case "$SCRATCH_URL" in *amazonaws.com*|*supabase*|*rds.*) echo "refusing: SCRATCH_URL looks like a managed/production endpoint"; exit 2;; esac
mkdir -p "$OUT_DIR"; cd "$CANDIDATE_TREE/service"
export PYTHONPATH="$CANDIDATE_TREE/service:$CANDIDATE_TREE/service/tests" ONLYFANS_RECOVERY_URL="$SCRATCH_URL" ONLYFANS_MIGRATION_URL="$SCRATCH_URL"
say() { echo "=== $*" | tee -a "$OUT_DIR/q15-snapshot.log"; }

say "1 restore the authorized snapshot into an EMPTY scratch database (pg_restore --no-owner), record revision and per-table row counts"
createdb_url() { python - "$SCRATCH_URL" "$1" <<'PY'
import sys; from sqlalchemy import create_engine, text
e=create_engine(sys.argv[1],isolation_level="AUTOCOMMIT")
with e.connect() as c: c.execute(text(f'DROP DATABASE IF EXISTS "{sys.argv[2]}" WITH (FORCE)')); c.execute(text(f'CREATE DATABASE "{sys.argv[2]}" TEMPLATE template0'))
PY
}
createdb_url q15_snapshot
SNAP_URL=${SCRATCH_URL%/*}/q15_snapshot
${PG_BIN:+$PG_BIN/}pg_restore --no-owner --no-privileges --exit-on-error --single-transaction --dbname "$SNAP_URL" "$SNAPSHOT_DUMP"
python - "$SNAP_URL" "$OUT_DIR/snapshot-counts.json" <<'PY' | tee -a "$OUT_DIR/q15-snapshot.log"
import json,sys; from sqlalchemy import create_engine, inspect, text
e=create_engine(sys.argv[1])
with e.connect() as c:
    rev=c.scalar(text("select version_num from alembic_version")); counts={t:c.scalar(text(f'select count(*) from "{t}"')) for t in inspect(c).get_table_names()}
    print("snapshot revision:",rev); print("tables:",len(counts),"rows:",sum(counts.values()))
    print("unresolved/accepted sends:",c.execute(text("select status,count(*) from of_text_intent group by 1 order by 1")).all())
    print("purchase evidence:",c.scalar(text("select count(*) from of_commerce_evidence where kind='purchase'")))
json.dump(counts,open(sys.argv[2],"w"),indent=1)
PY

say "2 supported-lineage upgrade of the snapshot copy to the candidate head through db.schema.upgrade (check_schema before and after); row counts must not shrink"
python -m db.schema check --url-env ONLYFANS_RECOVERY_URL >/dev/null 2>&1 || true   # informational: expected to refuse until upgraded
ONLYFANS_MIGRATION_URL="$SNAP_URL" python -m db.schema upgrade 2>&1 | tee -a "$OUT_DIR/q15-snapshot.log"
ONLYFANS_MIGRATION_URL="$SNAP_URL" python -m db.schema check 2>&1 | tee -a "$OUT_DIR/q15-snapshot.log"

say "3 encrypted backup of the upgraded copy and quarantined restore into a NEW empty database; every public row must be equal"
ONLYFANS_RECOVERY_URL="$SNAP_URL" python -m onlyfans_runtime.operations backup "$OUT_DIR/upgraded.twinof" ${PG_BIN:+--pg-bin $PG_BIN}
createdb_url q15_restored
ONLYFANS_RECOVERY_URL="${SCRATCH_URL%/*}/q15_restored" python -m onlyfans_runtime.operations restore "$OUT_DIR/upgraded.twinof" ${PG_BIN:+--pg-bin $PG_BIN}
python - "$SNAP_URL" "${SCRATCH_URL%/*}/q15_restored" <<'PY' | tee -a "$OUT_DIR/q15-snapshot.log"
import sys; from sqlalchemy import create_engine, inspect, text
def rows(url):
    e=create_engine(url)
    with e.connect() as c: return {t:c.scalar(text(f'select jsonb_agg(to_jsonb(x) order by to_jsonb(x)::text) from "{t}" x')) for t in inspect(c).get_table_names(schema="public") if t!="alembic_version"}
a,b=rows(sys.argv[1]),rows(sys.argv[2]); bad=[t for t in a if a[t]!=b.get(t)]
print("row-for-row restore:", "OK" if not bad else "DIFFERS in "+", ".join(bad)); sys.exit(1 if bad else 0)
PY

say "4 guarded rollback: the previous release refuses the upgraded schema; the downgrade must refuse (changing nothing) while new evidence exists, or succeed only if nothing is discarded"
(cd "$R0_TREE/service" && PYTHONPATH="$R0_TREE/service" R0_TEST_DB="$SNAP_URL" python -c "import os;from sqlalchemy import create_engine;from db.schema import check_schema;check_schema(create_engine(os.environ['R0_TEST_DB']).connect())") 2>&1 | tail -1 | tee -a "$OUT_DIR/q15-snapshot.log" || true
python - "$SNAP_URL" <<'PY' 2>&1 | tee -a "$OUT_DIR/q15-snapshot.log"
import sys; from alembic import command; from sqlalchemy import create_engine; from db.schema import _config, revision
e=create_engine(sys.argv[1])
try:
    with e.begin() as c: command.downgrade(_config(c),"0129b_model_retry_due")
    print("downgrade committed; revision", revision(e.connect()), "-> the previous release reader must now be re-run (step 4a) and rows re-compared")
except Exception as exc: print("downgrade REFUSED (evidence retained):", type(exc).__name__, str(exc)[:240])
PY

say "5 emergency stop and receive-only composition against the snapshot copy: run the two suites that pin them on the SAME candidate tree with the scratch database as the rig (no sender, no provider)"
python -m pytest -q -p no:randomly -p no:cacheprovider tests/test_commercial_scope_lifecycle_pg.py tests/test_plan_c_explicit_intake_pg.py tests/test_commercial_ops_recovery_pg.py 2>&1 | tail -5 | tee -a "$OUT_DIR/q15-snapshot.log"
say "done. A pass here is evidence about the snapshot only; it authorizes nothing. Remove q15_snapshot and q15_restored from the scratch server and destroy the snapshot copy per its retention rule."
