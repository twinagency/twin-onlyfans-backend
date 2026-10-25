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
