"""0138_tip_evidence_kind: the one CHECK on of_fans_evidence.kind is widened in place; nothing else moves, nothing is lost.

Real PostgreSQL, populated databases. No provider, no model, no network.
"""
import uuid

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from db.schema import HEAD, _config, check_schema
from onlyfans_runtime.store import utcnow
from test_onlyfans_foundation_pg import database, seed  # noqa: F401

PREVIOUS = "0137_model_retry_due_repair"


def evidence(engine, kind, n, account='acc-t', **over):
    now = utcnow()
    row = dict(account_id=account, evidence_id=str(uuid.uuid4()), dedupe_key=f'{kind}:{n}', kind=kind, source='webhook', fan_id=101,
               amount_cents=500 if kind == 'tip' else 1200, occurred_at=now, status='pending', raw_sha256=f'{n:064x}',
               created_at=now, updated_at=now)
    row.update(over)
    cols = ','.join(row)
    marks = ','.join(':' + k for k in row)
    with engine.begin() as c:
        c.execute(text(f'INSERT INTO of_fans_evidence ({cols}) VALUES ({marks})'), row)
    return row


def refused(engine, kind, n):
    with pytest.raises(DBAPIError):
        evidence(engine, kind, n)


def test_head_is_0138_on_top_of_the_0137_repair(database):
    url, engine = database
    assert HEAD == '0138_tip_evidence_kind'
    with engine.connect() as c:
        check_schema(c)


def test_a_populated_0137_database_upgrades_keeping_every_row_and_then_accepts_tip(database):
    url, engine = database
    store = seed(url, engine, account='acc-t')
    try:
        with engine.begin() as c:
            command.downgrade(_config(c), PREVIOUS)
        with engine.connect() as c:
            assert c.scalar(text('SELECT version_num FROM alembic_version')) == PREVIOUS
        before = [evidence(engine, kind, i) for i, kind in enumerate(('unlock', 'transaction', 'chargeback', 'human_sent'), start=1)]
        refused(engine, 'tip', 50)                          # the old schema cannot hold a tip
        with engine.begin() as c:
            command.upgrade(_config(c), 'head')
        with engine.connect() as c:
            check_schema(c)
            rows = c.execute(text('SELECT evidence_id, kind, dedupe_key, amount_cents, status, raw_sha256 FROM of_fans_evidence ORDER BY dedupe_key')).all()
        assert sorted((r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows) == sorted(
            (b['evidence_id'], b['kind'], b['dedupe_key'], b['amount_cents'], b['status'], b['raw_sha256']) for b in before)
        evidence(engine, 'tip', 51)                         # now valid
        refused(engine, 'gift', 52)                         # the check is widened by exactly one kind
        refused(engine, 'tip', 51)                          # the natural key still deduplicates
    finally:
        store.close()


def test_downgrade_refuses_while_tip_evidence_exists_and_succeeds_without_it(database):
    url, engine = database
    store = seed(url, engine, account='acc-t')
    try:
        evidence(engine, 'tip', 60)
        with pytest.raises(Exception, match='Preserve tip evidence'):
            with engine.begin() as c:
                command.downgrade(_config(c), PREVIOUS)
        with engine.connect() as c:
            assert c.scalar(text('SELECT version_num FROM alembic_version')) == HEAD     # nothing moved
            assert c.scalar(text("SELECT count(*) FROM of_fans_evidence WHERE kind='tip'")) == 1
        with engine.begin() as c:
            c.execute(text("UPDATE of_fans_evidence SET kind='transaction' WHERE kind='tip'"))   # an explicit human decision, not ours
            command.downgrade(_config(c), PREVIOUS)
        refused(engine, 'tip', 61)
        with engine.begin() as c:
            command.upgrade(_config(c), 'head')
        with engine.connect() as c:
            check_schema(c)
    finally:
        store.close()
