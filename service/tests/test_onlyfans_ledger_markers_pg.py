"""Provider-shaped marker traversal on isolated PostgreSQL, including restart."""
from datetime import timedelta
import json

import pytest
from sqlalchemy import func,select,text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified,TENANT
from test_onlyfans_ledger_pg import transaction
from db.models import Fan,Transaction
from db.onlyfans_ledger_models import OfLedgerHistory,OfLedgerScan,OfLedgerEvidence
from db.onlyfans_models import OfRuntime
from onlyfans_runtime import ledger,provider_read_budget
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import Store,utcnow


def clock(monkeypatch):
    value=[utcnow()]
    def tick():
        value[0]+=timedelta(seconds=1)
        return value[0]
    monkeypatch.setattr(ledger,"utcnow",tick)
    monkeypatch.setattr(provider_read_budget,"utcnow",tick)
    return value


def due(store,account,model):
    with Session(store.engine) as s,s.begin():
        scan=s.get(model,account)
        if scan:
            scan.next_poll_at=utcnow()-timedelta(seconds=1)


@pytest.mark.parametrize("count",[521,1001])
def test_markers_pass_offset_ceiling_and_resume_without_reapplying_money(database,monkeypatch,count):
    store,account,_=verified(database,monkeypatch)
    clock(monkeypatch)
    calls=[]
    source=[transaction(id=f"receipt-{i}",description="Tip from Synthetic Fan") for i in range(count)]
    def page(self,**kw):
        calls.append(kw)
        assert kw["history"] is True
        # This endpoint's offset path falsely ends at 520. Only its opaque
        # continuation establishes the rest, even after the process restarts.
        index=int(kw["marker"]) if "marker" in kw else kw["offset"]
        if "marker" not in kw and index>=520:
            return b'{"list":[],"hasMore":false}'
        end=min(index+20,count)
        result={"list":source[index:end],"hasMore":end<count}
        if end<count:result["nextMarker"]=end
        return json.dumps(result).encode()
    monkeypatch.setattr(NativeProviderConnector,"transaction_page",page)
    for turn in range(count//20+2):
        due(store,account,OfLedgerHistory)
        before=len(calls)
        assert ledger.process_ledger_pages(store,TENANT,history=True)==1
        assert len(calls)-before==1
        with Session(store.engine) as s:
            scan=s.get(OfLedgerHistory,account)
            complete=scan.status=="idle"
            assert ledger.coverage_proven(scan) is complete
            assert s.get(OfRuntime,account).enabled is False
        if turn==8:
            store.close()
            store=Store(database[0])
        if complete:break
    with Session(store.engine) as s:
        scan=s.get(OfLedgerHistory,account)
        assert scan.status=="idle" and scan.page_offset==count
        assert s.scalar(select(func.count()).select_from(Transaction))==count
        assert s.scalar(select(func.count()).select_from(OfLedgerEvidence))==count
        assert s.get(Fan,(account,101)).lifetime_spend_cents==count*1000
    assert calls[0]["offset"]==0 and "marker" not in calls[0]
    assert all("marker" in call for call in calls[1:])
    assert calls[9]["marker"]=="180", "restart resumes the frozen traversal"
    store.close()


def test_ignored_dates_and_new_arrivals_do_not_truncate_or_enter_frozen_money(database,monkeypatch):
    store,account,_=verified(database,monkeypatch)
    clock(monkeypatch)
    calls=[]
    def page(self,**kw):
        calls.append(kw)
        if len(calls)==1:
            def row(identity,stamp):
                return transaction(id=identity,createdAt=stamp.isoformat()+"Z",description="Tip from Synthetic Fan")
            return json.dumps({"list":[row("older",kw["start"]-timedelta(days=1)),
                row("future",kw["end"]+timedelta(seconds=1)),row("within",kw["end"]-timedelta(seconds=1))],
                "hasMore":True,"nextMarker":123}).encode()
        assert kw["marker"]=="123"
        assert (kw["start"],kw["end"])==(calls[0]["start"],calls[0]["end"])
        return json.dumps({"list":[transaction(id="after-older-tail",description="Tip from Synthetic Fan")],"hasMore":False}).encode()
    monkeypatch.setattr(NativeProviderConnector,"transaction_page",page)
    assert ledger.process_ledger_pages(store,TENANT)==1
    with Session(store.engine) as s:
        scan=s.get(OfLedgerScan,account)
        assert scan.last_complete_at is None and not ledger.coverage_proven(scan)
    due(store,account,OfLedgerScan)
    assert ledger.process_ledger_pages(store,TENANT)==1
    with Session(store.engine) as s:
        assert set(s.scalars(select(Transaction.provider_transaction_id)))=={"within","after-older-tail"}
        assert s.get(Fan,(account,101)).lifetime_spend_cents==2000
        assert ledger.coverage_proven(s.get(OfLedgerScan,account))
    store.close()


@pytest.mark.parametrize("markers",[[1,1],[1,2,1,2],[1,None]])
def test_nonadvancing_cycle_or_missing_marker_cannot_certify_coverage(database,monkeypatch,markers):
    store,account,_=verified(database,monkeypatch)
    clock(monkeypatch)
    calls=[]
    def page(self,**kw):
        index=len(calls)
        calls.append(kw)
        result={"list":[transaction(id=f"page-{index}",description="Tip from Synthetic Fan")],"hasMore":True}
        if markers[index] is not None:result["nextMarker"]=markers[index]
        return json.dumps(result).encode()
    monkeypatch.setattr(NativeProviderConnector,"transaction_page",page)
    for _ in markers:
        due(store,account,OfLedgerScan)
        ledger.process_ledger_pages(store,TENANT)
    with Session(store.engine) as s:
        scan=s.get(OfLedgerScan,account)
        assert scan.status=="held" and scan.last_complete_at is None
        assert not ledger.coverage_proven(scan)
        assert s.scalar(select(func.count()).select_from(Transaction))==len(markers)-1
    assert len(calls)==len(markers)
    store.close()


def test_upgrade_invalidates_offset_claims_and_preserves_immutable_financial_rows(database,monkeypatch):
    from db.schema import upgrade,check_schema,LEDGER_HISTORY,SchemaMismatch
    from test_onlyfans_ledger_pg import parsed,record
    store,account,_=verified(database,monkeypatch)
    observed=parsed(transaction(description="Tip from Synthetic Fan"))
    record(store,account,observed)
    now=utcnow()
    with Session(store.engine) as s,s.begin():
        runtime=s.get(OfRuntime,account)
        for model in (OfLedgerScan,OfLedgerHistory):
            s.add(model(account_id=account,binding_id=runtime.binding_id,owner_id=7001,
                connection_revision=1,window_start=now-timedelta(days=90),window_end=now,
                page_offset=520,status="idle",next_poll_at=now+timedelta(days=1),
                last_complete_at=now,last_full_at=now,updated_at=now))
        before=s.scalar(select(Transaction)).raw_json
    with store.engine.begin() as c:
        from onlyfans_upgrade_fixture import restore_pre95_content_vault_shape
        restore_pre95_content_vault_shape(c)
        # The shared historical-shape helper already removes the later fan
        # profile columns; dropping them twice prevents the upgrade test running.
        c.exec_driver_sql("ALTER TABLE content_offers DROP COLUMN post_buy_started_at")
        for table in ("of_ledger_scan","of_ledger_history"):
            c.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN continuation_json CASCADE, DROP COLUMN coverage_json CASCADE")
        c.execute(text("UPDATE alembic_version SET version_num=:v"),{"v":LEDGER_HISTORY})
        check_schema(c,expected=LEDGER_HISTORY)
        upgrade(c)
        check_schema(c)
        with pytest.raises(SchemaMismatch):check_schema(c,expected=LEDGER_HISTORY)
    with Session(store.engine) as s:
        assert s.scalar(select(Transaction)).raw_json==before
        assert s.scalar(select(func.count()).select_from(OfLedgerEvidence))==1
        for model in (OfLedgerScan,OfLedgerHistory):
            scan=s.get(model,account)
            assert scan.status=="held" and scan.last_complete_at is None and scan.last_full_at is None
            assert scan.window_end==now and scan.page_offset==0 and not ledger.coverage_proven(scan)
    store.close()
