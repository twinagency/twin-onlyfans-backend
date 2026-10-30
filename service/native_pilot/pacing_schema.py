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
    CHECK(delay_seconds>=25 AND delay_seconds<=90), CHECK(admissions>=0))"""
INDEX = """CREATE UNIQUE INDEX IF NOT EXISTS native_reply_pacing_pending
    ON native_reply_pacing(account_id,fan_id,generation) WHERE state='waiting'"""


def upgrade(target):
    with store.transaction(target) as con:
        if not store.has_table(con, "native_pilots"):
            raise RuntimeError("native-runtime-schema-required")
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
