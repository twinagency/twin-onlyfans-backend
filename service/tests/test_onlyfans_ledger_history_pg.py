"""Lifetime imports use real PostgreSQL and synthetic provider pages."""
import threading
import pytest
from datetime import datetime,timedelta
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified,TENANT
from test_onlyfans_provider_adapters import OWNER
from test_onlyfans_ledger_pg import transaction,payload,record
from test_onlyfans_earnings_pg import setup,read
from db.models import Transaction
from db.onlyfans_ledger_models import OfLedgerScan,OfLedgerHistory
from onlyfans_runtime.ledger import process_ledger_pages,parse_page
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.ledger_worker import LedgerWorker
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.ledger import _coverage_receipt


@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("active_old_lease", [False, True])
@pytest.mark.parametrize("read_fails", [False, True])

def test_verified_reconnect_revalidates_future_scan_without_reusing_old_coverage(
        database, monkeypatch, history, active_old_lease, read_fails):
    from db.onlyfans_connection_models import OfProviderConnection
    from db.onlyfans_models import OfRuntime
    from onlyfans_runtime.connections import _open, _seal
    from onlyfans_runtime.native_protocol import NativeProtocolError
    store,account,_=verified(database,monkeypatch)
    model=OfLedgerHistory if history else OfLedgerScan
    now=utcnow()
    old_token="f"*36 if active_old_lease else None
    with Session(database[1]) as s,s.begin():
        runtime=s.get(OfRuntime,account)
        connection=s.get(OfProviderConnection,account)
        old_revision=connection.revision
        s.add(model(account_id=account,binding_id=runtime.binding_id,owner_id=connection.owner_id,
            connection_revision=old_revision,window_start=datetime(1970,1,1) if history else now-timedelta(days=90),
            window_end=now,page_offset=70,status="running" if active_old_lease else "idle",
            lease_token=old_token,lease_until=now+timedelta(seconds=60) if active_old_lease else None,
            next_poll_at=now+timedelta(days=1),last_complete_at=now,last_full_at=now,updated_at=now))
        # The credential generation is verified and sealed by the same owner;
        # retaining yesterday's deadline must not delay its coverage recovery.
        clear=_open(connection,TENANT)
        connection.revision+=1
        connection.sealed_config=_seal(clear,TENANT,account,runtime.binding_id,connection.revision)
    calls=[]
    def page(self,**kw):
        calls.append(kw)
        assert kw["offset"]==0
        if read_fails:
            raise NativeProtocolError("synthetic read failure")
        return payload([])
    monkeypatch.setattr(NativeProviderConnector,"transaction_page",page)
    assert process_ledger_pages(store,TENANT,history=history)==(0 if read_fails else 1)
    assert len(calls)==1
    with Session(database[1]) as s:
        scan=s.get(model,account)
        assert scan.connection_revision==old_revision+1 and scan.page_offset==0
        assert scan.lease_token is None and scan.lease_until is None
        assert s.get(OfRuntime,account).enabled is False
        if read_fails:
            assert scan.status=="held" and scan.last_complete_at is None and scan.last_full_at is None
        else:
            assert scan.status=="idle" and scan.last_complete_at and scan.last_full_at
    store.close()


@pytest.mark.parametrize("history", [False, True])
def test_current_generation_still_respects_scan_deadline_and_active_lease(database,monkeypatch,history):
    from db.onlyfans_connection_models import OfProviderConnection
    from db.onlyfans_models import OfRuntime
    store,account,_=verified(database,monkeypatch)
    model=OfLedgerHistory if history else OfLedgerScan
    now=utcnow()
    with Session(database[1]) as s,s.begin():
        runtime=s.get(OfRuntime,account);connection=s.get(OfProviderConnection,account)
        s.add(model(account_id=account,binding_id=runtime.binding_id,owner_id=connection.owner_id,
            connection_revision=connection.revision,window_start=datetime(1970,1,1) if history else now-timedelta(days=90),
            window_end=now,page_offset=70,status="running",lease_token="f"*36,lease_until=now+timedelta(seconds=60),
            next_poll_at=now-timedelta(seconds=1),updated_at=now))
    def forbidden(*args,**kwargs):
        raise AssertionError("same-generation active scan must retain its lease")
    monkeypatch.setattr(NativeProviderConnector,"transaction_page",forbidden)
    assert process_ledger_pages(store,TENANT,history=history)==0
    with Session(database[1]) as s,s.begin():
        scan=s.get(model,account)
        assert scan.lease_token=="f"*36
        scan.lease_token=scan.lease_until=None
        scan.next_poll_at=now+timedelta(days=1)
    assert process_ledger_pages(store,TENANT,history=history)==0
    store.close()

def due(engine,account,model=OfLedgerHistory):
    with Session(engine) as s,s.begin():
        scan=s.get(model,account)
        scan.next_poll_at=utcnow()-timedelta(seconds=1)

def test_full_history_resumes_deduplicates_and_keeps_recent_poll_independent(database,monkeypatch):
    store,account,_=verified(database,monkeypatch)
    old=transaction(id="old",amount="15.00",net="12.00",
        createdAt=(utcnow()-timedelta(days=900)).isoformat()+"Z")
    new=transaction(id="recent",amount="10.00",net="8.00")
    calls=[]
    def page(self,**kw):
        calls.append(kw)
        if kw.get("history"):
            assert kw["start"]==datetime(1970,1,1)
            return payload([new],True) if kw["offset"]==0 else payload([old])
        return payload([new])
    monkeypatch.setattr(NativeProviderConnector,"transaction_page",page)
    assert process_ledger_pages(store,TENANT,history=True)==1
    with Session(database[1]) as s:
        h=s.get(OfLedgerHistory,account)
        assert h.page_offset==1 and h.status=="running" and h.last_full_at is None
    assert process_ledger_pages(store,TENANT)==1
    due(database[1],account)
    assert process_ledger_pages(store,TENANT,history=True)==1
    with Session(database[1]) as s:
        h=s.get(OfLedgerHistory,account);r=s.get(OfLedgerScan,account)
        assert h.status=="idle" and h.last_full_at and h.last_complete_at
        assert h.page_offset==2 and r.last_full_at
        assert s.scalar(select(func.count()).select_from(Transaction))==2
        assert s.scalar(select(func.sum(Transaction.amount_cents)))==2500
    assert [x["offset"] for x in calls if x.get("history")]==[0,1]
    assert process_ledger_pages(store,TENANT,history=True)==0
    store.close()

def history_row(account,*,complete=True,revision=1):
    now=utcnow()
    row=OfLedgerHistory(account_id=account,binding_id="binding-"+account,
        connection_revision=revision,owner_id=OWNER,window_start=datetime(1970,1,1),
        window_end=now,page_offset=1,status="idle" if complete else "running",
        next_poll_at=now+timedelta(hours=24),last_complete_at=now if complete else None,
        last_full_at=now if complete else None,updated_at=now)
    row.coverage_json=_coverage_receipt(row) if complete else None
    return row

def test_lifetime_total_requires_terminal_history_and_current_binding(database):
    store=setup(database)
    now=utcnow()
    old=transaction(id="old",amount="15.00",net="12.00",
        createdAt=(now-timedelta(days=900)).isoformat()+"Z")
    observation=parse_page(payload([old]),owner_id=OWNER,start=datetime(1970,1,1),end=now)[0][0]
    record(store,"acc-a",observation)
    start="1970-01-01T00:00:00.000Z"
    result=read(database,start=start,include_pending=True,include_history=True)
    assert result["recorded"] is None and result["reasons"]==["outside_coverage"]
    assert result["history"]["completedAccountCount"]==0
    with Session(database[1]) as s,s.begin():s.add(history_row("acc-a",complete=False))
    assert read(database,start=start,include_pending=True,include_history=True)["recorded"] is None
    with Session(database[1]) as s,s.begin():
        h=s.get(OfLedgerHistory,"acc-a");h.last_full_at=h.last_complete_at=utcnow();h.status="idle";h.coverage_json=_coverage_receipt(h)
    result=read(database,start=start,include_pending=True,include_history=True)
    assert result["status"]=="current" and result["recorded"]["netCents"]==1200
    assert result["history"]["completedAccountCount"]==1
    assert result["statistics"]["byAccount"]==[{"accountId":"acc-a","paymentCount":1,"grossCents":1500,"netCents":1200}]
    with Session(database[1]) as s,s.begin():s.get(OfLedgerHistory,"acc-a").connection_revision=2
    assert read(database,start=start,include_pending=True,include_history=True)["recorded"] is None
    store.close()

def test_ledger_lane_keeps_running_without_chat_and_honours_stop_and_redaction():
    stop=threading.Event();calls=[];reports=[]
    def process(store,tenant,**kw):
        calls.append(kw.get("history",False))
        if len(calls)==2:raise RuntimeError("private provider capture")
        if len(calls)==4:stop.set()
    stop.wait=lambda seconds:None
    LedgerWorker(object(),"tenant",stop,observe=reports.append,process=process).run()
    assert calls==[False,True,False,True]
    assert reports==[{"schemaVersion":"of-worker.v1","state":"ledger_unavailable"}]

def test_history_page_is_still_verified_identity_bounded_and_get_only(monkeypatch):
    from test_onlyfans_provider_adapters import credential
    from urllib.parse import urlsplit,parse_qs
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    wire=NativeProviderConnector(credential());seen=[]
    monkeypatch.setattr(wire,"verify_identity",lambda:seen.append("identity"))
    def call(method,path,body=b""):
        assert seen==["identity"] and method=="GET" and body==b""
        assert urlsplit(path).path=="/api2/v2/payouts/transactions"
        assert parse_qs(urlsplit(path).query)["start"]==["1970-01-01T00:00:00Z"]
        return payload([])
    monkeypatch.setattr(wire,"_call",call)
    assert wire.transaction_page(offset=20000,start=datetime(1970,1,1),end=datetime(2026,10,26),history=True)

def test_additive_upgrade_from_91_retains_financial_evidence(database):
    from db.schema import upgrade,check_schema
    store=setup(database)
    from test_onlyfans_ledger_pg import parsed
    record(store,"acc-a",parsed(transaction(id="retained",net="8.00")))
    with database[1].begin() as conn:
        # Reconstruct the previous additive schema in this disposable DB.
        conn.exec_driver_sql("DROP TABLE of_ledger_history")
        conn.exec_driver_sql("ALTER TABLE of_ledger_scan DROP COLUMN continuation_json CASCADE, DROP COLUMN coverage_json CASCADE")
        from onlyfans_upgrade_fixture import restore_pre95_content_vault_shape
        restore_pre95_content_vault_shape(conn)
        conn.exec_driver_sql("ALTER TABLE fans DROP COLUMN of_profile_checked_at CASCADE, DROP COLUMN of_profile_error")
        conn.exec_driver_sql("ALTER TABLE content_offers DROP COLUMN post_buy_started_at")
        conn.exec_driver_sql("UPDATE alembic_version SET version_num='0091_launch_controls'")
        check_schema(conn,expected="0091_launch_controls")
        upgrade(conn)
        check_schema(conn)
    with Session(database[1]) as session:
        assert session.scalar(select(Transaction.provider_transaction_id))=="retained"
        assert session.scalar(select(func.count()).select_from(OfLedgerHistory))==0
    store.close()

def test_old_import_does_not_cancel_current_engagement(database,monkeypatch):
    store=setup(database)
    from onlyfans_runtime.ledger import apply_observation
    import onlyfans_runtime.launch_engagement.events as events
    calls=[]
    monkeypatch.setattr(events,"cancel_for_fan",lambda *a,**kw:calls.append(True))
    now=utcnow()
    row=transaction(createdAt=(now-timedelta(days=900)).isoformat()+"Z")
    observation=parse_page(payload([row]),owner_id=OWNER,start=datetime(1970,1,1),end=now)[0][0]
    with Session(database[1]) as session,session.begin():
        store._runtime(session,"tenant-a","acc-a")
        assert apply_observation(session,"acc-a",observation,now=now,backfill=True)=="applied"
    assert calls==[]
    with Session(database[1]) as session:
        assert session.scalar(select(func.count()).select_from(Transaction))==1
    store.close()
