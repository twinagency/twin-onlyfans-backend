"""Explicit additive native-runtime migration. Rollback retains timing evidence.

Run against the selected release database BEFORE enabling pacing:
python -m native_pilot.pacing_schema --url-env ONLYFANS_MIGRATION_URL
This does not advance the public application's Alembic head.
"""
from twin_bridge import store

DDL = """CREATE TABLE IF NOT EXISTS native_reply_pacing (
    schedule_key TEXT PRIMARY KEY, account_id TEXT NOT NULL, fan_id BIGINT NOT NULL,
    generation TEXT NOT NULL, anchor_id BIGINT NOT NULL, first_received_at TEXT NOT NULL,
    provider_created_at TEXT NOT NULL, target_at TEXT NOT NULL, delay_seconds REAL NOT NULL,
    active_exchange INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'waiting',
    admissions INTEGER NOT NULL DEFAULT 0, input_key TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    CHECK(state IN ('waiting','done','invalidated')),
    CONSTRAINT native_reply_pacing_delay_bounds CHECK(delay_seconds>=25 AND delay_seconds<=180), CHECK(admissions>=0))"""
INDEX = """CREATE UNIQUE INDEX IF NOT EXISTS native_reply_pacing_pending
    ON native_reply_pacing(account_id,fan_id,generation) WHERE state='waiting'"""


def upgrade(target):
    with store.transaction(target) as con:
        if not store.has_table(con, "native_pilots"):
            raise RuntimeError("native-runtime-schema-required")
        if store.has_table(con, "native_reply_pacing"):
            if store.is_pg(target):
                import re
                rows = con.execute("SELECT conname,pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                    "WHERE conrelid=to_regclass('native_reply_pacing') AND contype='c'").fetchall()
                found_delay_bound = False
                for row in rows:
                    definition = row['definition']
                    if 'delay_seconds' not in definition:
                        continue
                    found_delay_bound = True
                    normalized = re.sub(r'::(?:double precision|real|numeric|integer)', '', definition)
                    normalized = re.sub(r'[\s()]', '', normalized).lower()
                    if normalized not in ('checkdelay_seconds>=25anddelay_seconds<=90',
                                          'checkdelay_seconds>=25anddelay_seconds<=180'):
                        raise RuntimeError("unrecognized-native-pacing-delay-constraint")
                    if normalized.endswith('<=90'):
                        name = row['conname'].replace('"', '""')
                        con.execute('ALTER TABLE native_reply_pacing DROP CONSTRAINT "'+name+'"')
                        con.execute('ALTER TABLE native_reply_pacing ADD CONSTRAINT "'+name+
                                    '" CHECK(delay_seconds>=25 AND delay_seconds<=180)')
                if not found_delay_bound:
                    raise RuntimeError("unrecognized-native-pacing-delay-constraint")
            else:
                sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='native_reply_pacing'").fetchone()[0]
                normalized = ''.join(sql.lower().split())
                if 'check(delay_seconds>=25anddelay_seconds<=90)' in normalized:
                    # This table has no outbound foreign keys. Copy every column
                    # unchanged; only the known delay bound is widened.
                    con.execute("ALTER TABLE native_reply_pacing RENAME TO native_reply_pacing_legacy_delay")
                    con.execute(DDL)
                    con.execute("INSERT INTO native_reply_pacing SELECT * FROM native_reply_pacing_legacy_delay")
                    con.execute("DROP TABLE native_reply_pacing_legacy_delay")
                elif 'check(delay_seconds>=25anddelay_seconds<=180)' not in normalized:
                    raise RuntimeError("unrecognized-native-pacing-delay-constraint")
        con.execute(DDL)
        con.execute(INDEX)


def downgrade(target):
    # Old runners ignore the additive table. Never delete timing/dispatch evidence.
    return None


if __name__ == "__main__":
    import argparse
    import os
    parser = argparse.ArgumentParser()
    parser.add_argument("--url-env", required=True)
    args = parser.parse_args()
    target = store.target_from_url(os.environ[args.url_env])
    if target is None:
        raise SystemExit("explicit-database-required")
    upgrade(target)
    print("native-reply-pacing-schema-ready")
