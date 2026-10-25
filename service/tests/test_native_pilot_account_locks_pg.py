"""Scoped native writes retain the legacy barrier and same-account exclusion."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import threading

import pytest
from sqlalchemy import text
from test_onlyfans_foundation_pg import database
from native_pilot.ledger import Ledger, Refused
from twin_bridge import store


def _enter_scoped(url, account, started):
    started.set()
    with store.account_transaction(url, lambda con: account):
        return account


def _enter_legacy(url, started):
    started.set()
    with store.transaction(url):
        return "legacy"


def test_same_account_excludes_but_other_accounts_can_commit(database):
    url, _ = database
    store.ensure_runtime_schema(url)
    same_started, other_started = threading.Event(), threading.Event()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            with store.account_transaction(url, lambda con: "of-900000001"):
                same = pool.submit(_enter_scoped, url, "of-900000001", same_started)
                other = pool.submit(_enter_scoped, url, "of-900000002", other_started)
                assert same_started.wait(1) and other_started.wait(1)
                assert other.result(timeout=3) == "of-900000002"
                assert not same.done()
            assert same.result(timeout=3) == "of-900000001"
    finally:
        store.dispose_pools()


def test_legacy_writer_blocks_scoped_writes_for_every_account(database):
    url, _ = database
    store.ensure_runtime_schema(url)
    started = [threading.Event(), threading.Event()]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            with store.transaction(url):
                pending = [pool.submit(_enter_scoped, url, f"of-{900000001+i}", started[i]) for i in range(2)]
                assert all(event.wait(1) for event in started)
                for future in pending:
                    with pytest.raises(TimeoutError):
                        future.result(timeout=0.1)
            assert [future.result(timeout=3) for future in pending] == ["of-900000001", "of-900000002"]
    finally:
        store.dispose_pools()


def test_scoped_writer_blocks_legacy_writer_until_commit_or_rollback(database):
    url, _ = database
    store.ensure_runtime_schema(url)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            started = threading.Event()
            with pytest.raises(RuntimeError, match="synthetic rollback"):
                with store.account_transaction(url, lambda con: "of-900000001"):
                    pending = pool.submit(_enter_legacy, url, started)
                    assert started.wait(1)
                    with pytest.raises(TimeoutError):
                        pending.result(timeout=0.1)
                    raise RuntimeError("synthetic rollback")
            assert pending.result(timeout=3) == "legacy"
    finally:
        store.dispose_pools()


def test_native_ledger_resolves_durable_accounts_and_refuses_mismatched_identities(database):
    url, engine = database
    store.ensure_runtime_schema(url)
    with engine.begin() as connection:
        for offset in [1, 2]:
            connection.execute(text("""
                INSERT INTO fastt_runtime.native_pilots(
                    pilot_id,account_id,fan_id,owner_token,cutoff,deadline,grant_limit,
                    baseline_used,end_limit,revision,state,model_calls,created_at,updated_at)
                VALUES(:pilot,:account,1,'synthetic-owner-token',
                    '2026-10-25T00:00:00','2026-10-26T00:00:00',30,
                    0,30,1,'active',0,'2026-10-25T00:00:00','2026-10-25T00:00:00')
            """), {"pilot": f"svc-{900000000+offset}-1", "account": f"of-{900000000+offset}"})
    ledger = Ledger(url, concurrent_reads=True, account_locks=True)
    try:
        with pytest.raises(Refused, match="account-scope-mismatch"):
            with ledger._tx(pilot_id="svc-900000001-1", account_id="of-900000002"):
                raise AssertionError("mismatched accounts entered transaction")
        with ThreadPoolExecutor(max_workers=2) as pool:
            with ledger._tx(pilot_id="svc-900000001-1"):
                same = pool.submit(ledger.stop, pilot_id="svc-900000001-1",
                                   owner_token="synthetic-owner-token", reason="operator-stop",
                                   now=datetime(2026, 10, 25, 1))
                other = pool.submit(ledger.stop, pilot_id="svc-900000002-1",
                                    owner_token="synthetic-owner-token", reason="operator-stop",
                                    now=datetime(2026, 10, 25, 1))
                other.result(timeout=3)
                assert ledger.status("svc-900000002-1")["state"] == "stopped"
                assert ledger.status("svc-900000001-1")["state"] == "active"
                assert not same.done()
            same.result(timeout=3)
        assert ledger.status("svc-900000001-1")["state"] == "stopped"
    finally:
        store.dispose_pools()


def test_missing_identity_keeps_legacy_exclusion_without_lock_upgrade(database):
    url, _ = database
    store.ensure_runtime_schema(url)
    ledger = Ledger(url, account_locks=True)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            started = threading.Event()
            with ledger._tx(pilot_id="missing-pilot"):
                pending = pool.submit(_enter_scoped, url, "of-900000002", started)
                assert started.wait(1)
                with pytest.raises(TimeoutError):
                    pending.result(timeout=0.1)
            assert pending.result(timeout=3) == "of-900000002"
    finally:
        store.dispose_pools()
