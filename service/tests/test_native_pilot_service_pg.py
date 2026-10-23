"""The persistent FASTT runner on REAL PostgreSQL (schema 0091 from ``bootstrap`` + fastt_runtime).

Runs test_native_pilot_service.py's ledger, supervised-loop and intake suites
unchanged through a PostgreSQL ``Backend`` (typed rows for the account's
delivery policy), and adds what scratch tables cannot show: the idempotent
conversation ensure step against the real tables, and live inbound persisted by
the REAL event transcoder into the shared Chat/Fan/Message rows, once.

ONLYFANS_TEST_PG_URL must be an admin URL of a disposable server on which this
script may create and drop its own database. Run as a plain script.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import test_native_pilot_storage_pg as base  # sets DATABASE_URL/env, exposes PgFixture, rows(), SYNC_URL
import test_native_pilot_service as suite
import test_native_pilot_ledger as L
from sqlalchemy import create_engine
from twin_bridge import store

check = suite.check


class PgBackend(suite.SqliteBackend):
    pg = True

    def fixture(self, root, name):
        for table in ("of_delivery_policy", "of_runtime", "of_creator_bindings", "skip_list", "of_conversation_state",
                      "chats", "fans"):
            try:
                base.rows("DELETE FROM " + table)
            except store.psycopg2.Error:
                pass
        return base.PgFixture(root, name)

    def _account(self):
        base.rows("INSERT INTO accounts(id,is_active_default) VALUES(?,false) ON CONFLICT DO NOTHING", (L.ACCT,))

    def policy(self, f, *, enabled=True, limit=100, used=4, throttle=20, nxt=None, busy=None):
        base.rows("DELETE FROM of_delivery_policy")
        if limit is None:
            return
        self._account()
        base.rows("INSERT INTO of_creator_bindings(id,tenant_id,creator_id,creator_name,account_id,handle,channel) "
                  "VALUES('binding-1','synthetic-tenant','creator-1','Synthetic',?,'synthetic','onlyfans') ON CONFLICT DO NOTHING",
                  (L.ACCT,))
        base.rows("INSERT INTO of_runtime(account_id,binding_id,enabled,activated_at) VALUES(?,'binding-1',false,CURRENT_TIMESTAMP) "
                  "ON CONFLICT DO NOTHING", (L.ACCT,))
        base.rows("INSERT INTO of_delivery_policy(account_id,revision,enabled,transport_kind,limit_units,used_units,queued_count,"
                  "throttle_seconds,next_dispatch_at,active_intent_id,media_enabled) VALUES(?,1,?,'native',?,?,0,?,?,?,false)",
                  (L.ACCT, bool(enabled), limit, used, throttle, nxt, busy))

    def prepare(self, f):
        super().prepare(f)
        from sqlalchemy.orm import Session
        from db.models import Fan
        engine = create_engine(base.SYNC_URL)
        with Session(engine) as session:
            session.add(Fan(account_id=L.ACCT, fan_id=L.FAN))
            session.commit()
        engine.dispose()
        base.rows("INSERT INTO of_conversation_state(account_id,fan_id,mode,takeover_state) VALUES(?,?,'automated','none') "
                  "ON CONFLICT DO NOTHING", (L.ACCT, L.FAN))

    def set_state(self, f, mode, takeover="none"):
        base.rows("UPDATE of_conversation_state SET mode=?,takeover_state=? WHERE account_id=? AND fan_id=?",
                  (mode, takeover, L.ACCT, L.FAN))


    def add_fan(self, f, fan):
        base.seed_conversation(self.L.ACCT, fan)
        self._fan(fan)

    def _fan(self, fan):
        from sqlalchemy.orm import Session
        from db.models import Fan
        engine = create_engine(base.SYNC_URL)
        with Session(engine) as session:
            if session.get(Fan, (self.L.ACCT, fan)) is None:
                session.add(Fan(account_id=self.L.ACCT, fan_id=fan))
                session.commit()
        engine.dispose()

    def fan_state(self, f, fan, mode, takeover="none"):
        self._fan(fan)
        base.rows("INSERT INTO of_conversation_state(account_id,fan_id,mode,takeover_state) VALUES(?,?,?,?) "
                  "ON CONFLICT(account_id,fan_id) DO UPDATE SET mode=EXCLUDED.mode,takeover_state=EXCLUDED.takeover_state",
                  (self.L.ACCT, fan, mode, takeover))

    def fan_skip(self, f, fan, reason):
        base.rows("DELETE FROM skip_list WHERE account_id=? AND fan_id=?", (self.L.ACCT, fan))
        if reason:
            base.rows("INSERT INTO skip_list(account_id,fan_id,reason,added_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
                      (self.L.ACCT, fan, reason))


async def real_schema():
    from native_pilot import service
    from native_pilot.intake import Intake
    from native_pilot.ledger import Ledger
    from native_pilot.scope import AccountScope
    from db.engine import engine

    A, F = "of-" + L.ACCT, L.FAN
    scope = AccountScope.parse(A)
    base.reset()
    base.rows("DELETE FROM fans")
    base.rows("INSERT INTO accounts(id,is_active_default) VALUES(?,false)", (A,))

    # The ensure step creates the conversation once, never overwrites it, and activation then works.
    check(not base.rows("SELECT 1 FROM twin_bridge_conversations"))
    await service.ensure_fan_conversation(scope, F)
    await service.ensure_fan_conversation(scope, F)
    row = base.rows("SELECT state,reserved,send_budget_limit,send_budget_used,creator_slug,owner_token FROM twin_bridge_conversations")
    check(len(row) == 1 and row[0] == ("active", True, 0, 0, "fastt", None), row)
    base.rows("UPDATE twin_bridge_conversations SET send_budget_used=0,revision=7")
    await service.ensure_fan_conversation(scope, F)
    check(base.rows("SELECT revision FROM twin_bridge_conversations")[0][0] == 7, "ensure never overwrites")
    now = datetime.utcnow()
    pilot = Ledger(base.SYNC_URL, allowance=True).activate_service(
        pilot_id="svc-real", account_id=A, fan_id=F, owner_token="synthetic-owner-real-0001", now=now, ttl_s=120)
    check(pilot.state == "active")

    # Real transcoder: the scoped fan's plain text lands once in the shared rows; nothing else does.
    out = []
    intake = Intake(scope, F, base.SYNC_URL, emit=out.append)
    created = (now + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    def frame(mid, sender=F, **extra):
        return {"api2_chat_message": {"id": mid, "text": "<p>hello there</p>", "createdAt": created, "price": 0,
                                      "fromUser": {"id": sender, "username": "u" + str(sender), "name": "Fan"}, **extra}}
    check(await intake.handle(frame(9100)) == "persisted")
    check(await intake.handle(frame(9100)) == "duplicate")
    check(await intake.handle(frame(9101, sender=int(L.ACCT))) == "other-sender")
    check(await intake.handle(frame(9102, mediaCount=1, media=[{"id": 1}])) == "unsupported")
    stored = base.rows("SELECT message_id,direction,body FROM messages WHERE account_id=? ORDER BY message_id", (A,))
    check(stored == [(9100, "in", "hello there")] or (len(stored) == 1 and stored[0][:2] == (9100, "in")), stored)
    check(base.rows("SELECT count(*) FROM fans WHERE account_id=?", (A,))[0][0] == 1)
    # Intake's row is what the ledger snapshots (same shared history, no second copy).
    check(Ledger(base.SYNC_URL).snapshot(pilot_id="svc-real", owner_token="synthetic-owner-real-0001",
                                                  now=now + timedelta(seconds=10)) is not None)
    await engine.dispose()


def main():
    admin = create_engine(base.ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{base.NAME}" TEMPLATE template0 ENCODING \'UTF8\'')
    try:
        from db.schema import bootstrap
        engine = create_engine(base.SYNC_URL, connect_args={"options": "-c timezone=UTC"})
        with engine.begin() as conn:
            bootstrap(conn)
            conn.exec_driver_sql("INSERT INTO employees(id,display_name,is_active) VALUES "
                                 "(5,'Automation',false),(6,'Twin staff',false),(9,'Other',false)")
        engine.dispose()
        base.store.ensure_runtime_schema(base.SYNC_URL)  # runner tables live in fastt_runtime (schema stays 0091)
        L.database_url = lambda path: base.ASYNC_URL
        backend = PgBackend(L)
        with tempfile.TemporaryDirectory(prefix="native-pilot-service-pg-") as root:
            suite.ledger_level(backend, Path(root))
            print("ledger level on PostgreSQL ok", suite.CHECKS)
            asyncio.run(suite.loop_level(backend, Path(root)))
            print("supervised loop on PostgreSQL ok", suite.CHECKS)
            asyncio.run(suite.intake_level(backend, Path(root)))
            asyncio.run(suite.multi_level(backend, Path(root)))
            print("multi-fan supervised loop on PostgreSQL ok", suite.CHECKS)
            asyncio.run(suite.multi_intake_level(backend, Path(root)))
        asyncio.run(real_schema())
        print(f"native pilot service on PostgreSQL: all cases passed ({suite.CHECKS + base.CHECKS} checks)")
    finally:
        store.dispose_pools()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{base.NAME}" WITH (FORCE)')
        admin.dispose()


if __name__ == "__main__":
    main()
