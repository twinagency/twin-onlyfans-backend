"""Same native ledger owners, scoped report window, no real provider or model."""
import asyncio
from datetime import timedelta
from sqlalchemy import select,func,event as sql_event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker,create_async_engine
from sqlalchemy.orm import Session
import pytest
from test_onlyfans_foundation_pg import database,seed
from test_onlyfans_ledger_pg import record,parsed,transaction
from test_onlyfans_provider_adapters import OWNER
from db.models import Transaction
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_ledger_models import OfLedgerScan
from onlyfans_runtime.store import utcnow
from onlyfans_read.earnings import read_earnings,window
from onlyfans_read.errors import BadRequest


def setup(database,*,account="acc-a",tenant="tenant-a",coverage=True):
    store=seed(*database,account=account,tenant=tenant)
    now=utcnow()
    with Session(database[1]) as s,s.begin():
        s.add(OfProviderConnection(account_id=account,binding_id="binding-"+account,owner_id=OWNER+(0 if account=="acc-a" else 1),revision=1,
            sealed_config="synthetic-not-a-real-secret",expires_at=now+timedelta(hours=1),status="verified",verified_at=now,updated_at=now))
        if coverage:s.add(OfLedgerScan(account_id=account,binding_id="binding-"+account,owner_id=OWNER+(0 if account=="acc-a" else 1),connection_revision=1,
            window_start=now-timedelta(days=90),window_end=now,page_offset=0,status="idle",next_poll_at=now+timedelta(minutes=5),
            last_complete_at=now,last_full_at=now,updated_at=now))
    return store


def read(database,*,tenant="tenant-a",start=None,end=None):
    now=utcnow()
    a=start or (now-timedelta(days=30)).isoformat(timespec="milliseconds")+"Z"
    b=end or now.isoformat(timespec="milliseconds")+"Z"
    async def run():
        engine=create_async_engine(make_url(database[0]).set(drivername="postgresql+asyncpg"))
        def no_credentials(conn,cursor,statement,parameters,context,many):
            assert "sealed_config" not in statement, "financial summaries must not load provider credentials"
        sql_event.listen(engine.sync_engine,"before_cursor_execute",no_credentials)
        try:
            async with async_sessionmaker(engine)() as session:
                async with session.begin():return await read_earnings(session,tenant_id=tenant,start=a,end=b)
        finally:await engine.dispose()
    return asyncio.run(run())


def test_unknown_or_missing_accounts_are_unavailable_and_verified_empty_is_zero(database):
    assert read(database)["reasons"]==["no_bound_accounts"]
    store=setup(database,coverage=False)
    result=read(database);assert result["status"]=="unavailable" and result["clearedGrossCents"] is None
    assert result["reasons"]==["ledger_unavailable"]
    store.close()


def test_native_payments_are_counted_once_with_refunds_pending_and_missing_net_explicit(database):
    store=setup(database)
    first=parsed(transaction(net="8.00"));assert record(store,"acc-a",first)=="applied"
    assert record(store,"acc-a",first)=="duplicate"
    record(store,"acc-a",parsed(transaction(id="pending",status="loading")))
    record(store,"acc-a",parsed(transaction(id="reversed",status="undo")))
    result=read(database)
    assert result["status"]=="current" and result["currency"]=="USD"
    assert (result["clearedGrossCents"],result["clearedNetCents"],result["clearedCount"],result["pendingCount"],result["refundedCount"])==(1000,800,1,1,1)
    record(store,"acc-a",parsed(transaction(id="no-net",amount="5.00")))
    result=read(database);assert result["clearedGrossCents"]==1500 and result["clearedNetCents"] is None
    # Money remains real without inventing an exact purchased-media/message link.
    with Session(database[1]) as s:assert all(row.message_id is None for row in s.scalars(select(Transaction)))
    store.close()


def test_scope_and_window_exclude_other_tenants_and_old_transactions(database):
    store=setup(database);other=setup(database,account="acc-b",tenant="tenant-b")
    record(store,"acc-a",parsed(transaction(id="current",amount="3.00")))
    record(store,"acc-a",parsed(transaction(id="old",amount="99.00",createdAt=(utcnow()-timedelta(days=40)).isoformat()+"Z")))
    # Financial proof is created through the same native observation owner.
    with Session(other.engine) as s,s.begin():
        from onlyfans_runtime.ledger import apply_observation
        apply_observation(s,"acc-b",parsed(transaction(id="other",amount="100.00")),now=utcnow())
    result=read(database);assert result["accountCount"]==1 and result["clearedGrossCents"]==300
    assert read(database,tenant="unknown")["clearedGrossCents"] is None
    store.close();other.close()


def test_stale_held_or_unverified_records_are_never_a_false_complete_total(database):
    store=setup(database)
    assert read(database)["clearedGrossCents"]==0
    with Session(database[1]) as s,s.begin():s.get(OfLedgerScan,"acc-a").last_complete_at=utcnow()-timedelta(minutes=16)
    result=read(database);assert result["clearedGrossCents"] is None and result["reasons"]==["ledger_stale"]
    with Session(database[1]) as s,s.begin():
        scan=s.get(OfLedgerScan,"acc-a");scan.last_complete_at=utcnow();scan.status="held"
    assert read(database)["reasons"]==["ledger_held"]
    with Session(database[1]) as s,s.begin():
        s.get(OfLedgerScan,"acc-a").status="idle"
        s.add(Transaction(account_id="acc-a",kind="tip",amount_cents=900,status="cleared",currency="USD",occurred_at=utcnow()-timedelta(minutes=1)))
    result=read(database);assert result["clearedGrossCents"] is None and result["reasons"]==["unverified_ledger_rows"]
    store.close()


def test_bounded_timezone_aware_window_refuses_invalid_ranges():
    for a,b in [("bad","bad"),("2026-10-01","2026-10-02"),("2026-10-02T00:00:00Z","2026-10-01T00:00:00Z"),
            ("2026-01-01T00:00:00Z","2026-10-01T00:00:00Z")]:
        with pytest.raises(BadRequest):window(a,b)
