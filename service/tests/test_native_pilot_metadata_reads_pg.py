"""Consistent metadata reads must not serialize behind unrelated ledger writes."""
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import text
from test_onlyfans_foundation_pg import database
from native_pilot.ledger import Ledger
from twin_bridge import store

def test_metadata_reads_see_committed_state_without_taking_global_write_lock(database):
    url, engine = database
    store.ensure_runtime_schema(url)
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO fastt_runtime.native_pilots(
                pilot_id,account_id,fan_id,owner_token,cutoff,deadline,grant_limit,
                baseline_used,end_limit,revision,state,model_calls,created_at,updated_at)
            VALUES('svc-900000001-1','of-900000001',1,'synthetic-owner-token',
                '2026-10-25T00:00:00','2026-10-26T00:00:00',30,
                0,30,1,'active',0,'2026-10-25T00:00:00','2026-10-25T00:00:00')
        """))
    ledger = Ledger(url, allowance=True, concurrent_reads=True)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            with store.transaction(url) as writer:
                writer.execute("UPDATE native_pilots SET state='stopped' WHERE pilot_id=?",
                               ("svc-900000001-1",))
                status = pool.submit(ledger.status, "svc-900000001-1")
                receipts = pool.submit(ledger.receipts, pilot_id="svc-900000001-1")
                # The uncommitted stop is not yet effective. Readers retain a
                # consistent committed snapshot and do not wait for the writer.
                report = status.result(timeout=3)
                assert report["state"] == "active"
                assert report["account_id"] == "of-900000001"
                assert report["attempts"] == {}
                assert receipts.result(timeout=3) == []
        assert ledger.status("svc-900000001-1")["state"] == "stopped"
    finally:
        store.dispose_pools()


def test_quiet_fans_do_not_take_generation_candidates_but_keep_controls(database, monkeypatch):
    import argparse
    from datetime import datetime, timedelta
    from sqlalchemy.orm import Session
    from db.models import Account, Fan, Message
    from native_pilot.service import Service
    url, engine = database
    store.ensure_runtime_schema(url)
    account, now = "of-900000001", datetime.utcnow()
    with Session(engine) as session, session.begin():
        session.add(Account(id=account))
        session.flush()
        for fid, age in [(1, 0), (2, 20)]:
            session.add(Fan(account_id=account, fan_id=fid))
            session.add(Message(account_id=account, fan_id=fid, message_id=100 + fid,
                                direction="in", body="synthetic fixture",
                                created_at=now - timedelta(seconds=age),
                                ingested_at=now - timedelta(seconds=age)))
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO of_conversation_state(account_id,fan_id,mode,takeover_state)
            VALUES(:account,1,'automated','none'),(:account,2,'automated','none')
        """), {"account": account})
    monkeypatch.setenv("FASTT_MAX_CONCURRENT_FANS", "2")
    service = Service(argparse.Namespace(account=account, fan=None, all_fans=True))
    service.path = url
    service.started_at = now - timedelta(minutes=1)
    service.ledger = Ledger(url, allowance=True, concurrent_reads=True)
    service._owned = lambda fan: True
    try:
        pending, controlled = service.pending_fans()
        assert pending == [(2, 102)]
        assert set(controlled) == {1, 2}
        with engine.begin() as connection:
            connection.execute(text("UPDATE messages SET ingested_at=:at WHERE account_id=:account AND fan_id=1"),
                               {"at": now - timedelta(seconds=20), "account": account})
        pending, _ = service.pending_fans()
        assert set(pending) == {(1, 101), (2, 102)}
        # Serial fallback retains selection-before-wait behavior.
        monkeypatch.setenv("FASTT_MAX_CONCURRENT_FANS", "1")
        serial = Service(argparse.Namespace(account=account, fan=None, all_fans=True))
        serial.path, serial.started_at, serial.ledger, serial._owned = (
            url, service.started_at, service.ledger, service._owned)
        with engine.begin() as connection:
            connection.execute(text("UPDATE messages SET ingested_at=:at WHERE account_id=:account AND fan_id=1"),
                               {"at": now, "account": account})
        pending, _ = serial.pending_fans()
        assert set(pending) == {(1, 101), (2, 102)}
    finally:
        store.dispose_pools()
