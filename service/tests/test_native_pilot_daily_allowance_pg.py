"""Real PostgreSQL quota serialization using a disposable database, never live."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from test_onlyfans_recovery_pg import empty_database
from native_pilot.ledger import Ledger, Refused
from twin_bridge import store


def test_daily_quota_concurrency_expiry_isolation_and_uncertain_accounting(monkeypatch):
    monkeypatch.setenv("FASTT_FAN_DAILY_REPLY_LIMIT", "2000")
    import native_pilot.tuning as _tuning
    monkeypatch.setattr(_tuning, "MIN_SEND_INTERVAL_FLOOR_S", 0.0)  # engine v2 floor is a code constant
    with empty_database() as (url, engine):
        now = datetime.utcnow()
        ns = now.isoformat(sep=" ", timespec="microseconds")
        with engine.begin() as c:
            c.exec_driver_sql("CREATE SCHEMA fastt_runtime")
            c.exec_driver_sql("CREATE TABLE fastt_runtime.native_pilot_receipts(attempt_id bigint primary key,state text,created_at text)")
            c.exec_driver_sql("CREATE TABLE fastt_runtime.twin_bridge_send_attempts(id bigint primary key,account_id text,fan_id bigint,state text)")
            c.exec_driver_sql("CREATE TABLE public.of_delivery_policy(account_id text primary key,enabled boolean,limit_units int,used_units int,throttle_seconds int,next_dispatch_at timestamp,active_intent_id text)")
            c.exec_driver_sql("INSERT INTO public.of_delivery_policy VALUES('of-test',true,500,500,0,NULL,NULL)")
            c.exec_driver_sql("INSERT INTO fastt_runtime.twin_bridge_send_attempts SELECT i,'of-test',42,'sent' FROM generate_series(1,1999)i")
            c.execute(text("INSERT INTO fastt_runtime.native_pilot_receipts SELECT i,'sent',:now FROM generate_series(1,1999)i"), {"now": ns})
        ledger = Ledger(url, allowance=True)
        def admit(i, fan=42):
            try:
                with ledger._tx() as tx:
                    ledger._consume_allowance(tx, "of-test", ns, fan_id=fan, daily_limit=2000)
                    tx.execute("INSERT INTO twin_bridge_send_attempts VALUES(?,'of-test',?,'dispatching')", (i, fan))
                    tx.execute("INSERT INTO native_pilot_receipts VALUES(?,'dispatching',?)", (i, ns))
                return "admitted"
            except Refused as exc:
                return exc.reason
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(admit, [2000, 2001]))
        assert sorted(results) == ["admitted", "allowance-exhausted"]
        assert ledger.allowance_status("of-test", 42, now=now)["used"] == 2000
        assert ledger.allowance_status("of-test", 43, now=now)["used"] == 0
        assert Ledger(url, allowance=True).allowance_status("of-test", 42, now=now)["used"] == 2000
        assert ledger.allowance_status("of-test", 42, now=now+timedelta(hours=24, microseconds=-1))["used"] == 2000
        assert ledger.allowance_status("of-test", 42, now=now+timedelta(hours=24))["used"] == 0
        # A different fan's quota cannot bypass the existing account in-flight fence.
        assert admit(3000, 43) == "allowance-busy"
        with engine.begin() as c:
            c.exec_driver_sql("UPDATE fastt_runtime.twin_bridge_send_attempts SET state='sent' WHERE state='dispatching'")
            c.exec_driver_sql("UPDATE fastt_runtime.native_pilot_receipts SET state='not_sent' WHERE attempt_id=1")
            c.exec_driver_sql("UPDATE fastt_runtime.native_pilot_receipts SET state='delivery_unknown' WHERE attempt_id=2")
            c.exec_driver_sql("INSERT INTO fastt_runtime.twin_bridge_send_attempts VALUES(4000,'another-creator',42,'sent')")
            c.execute(text("INSERT INTO fastt_runtime.native_pilot_receipts VALUES(4000,'sent',:now)"), {"now": ns})
        assert ledger.allowance_status("of-test", 42, now=now)["used"] == 1999
        assert ledger.allowance_status("of-test")["used"] == 500
        assert admit(3001, 43) == "admitted"
        with engine.begin() as c:
            c.exec_driver_sql("UPDATE public.of_delivery_policy SET enabled=false")
        assert admit(3002, 43) == "allowance-disabled"
        store.dispose_pools()
