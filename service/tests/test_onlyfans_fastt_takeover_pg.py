"""Native admission and Twin takeover share the existing PostgreSQL account fence."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import threading
import pytest
from sqlalchemy import select, text, func
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from test_fastt_controls_pg import database, template
from test_onlyfans_foundation_pg import seed
from test_onlyfans_text_delivery_pg import current, TENANT
from db.models import Message, OfConversationState
from db.onlyfans_models import OfRuntime
from onlyfans_runtime.store import utcnow, Refused as TwinRefused
from onlyfans_runtime.text_delivery import TextAuthority
from native_pilot.ledger import Ledger, Refused
from native_pilot import reporting
from twin_bridge import store as bridge

ACCOUNT="of-7001"; FAN=101; OWNER="synthetic-native-owner-0001"


def prepared(database, *, service=False):
    store=seed(*database,account=ACCOUNT,tenant=TENANT,fan=FAN)
    current(store.engine,account=ACCOUNT,fan=FAN,mode="automated")
    authority=TextAuthority(store)
    authority.configure(TENANT,ACCOUNT,expected_revision=0,enabled=True,limit_units=20,
        throttle_seconds=1,actor="synthetic-operator",reason="Offline takeover proof")
    bridge.ensure_runtime_schema(database[0])
    now=utcnow()
    with bridge.transaction(database[0]) as c:
        c.execute("INSERT INTO twin_bridge_conversations(account_id,fan_id,creator_slug,conversation_id,scope_fingerprint,"
            "state,revision,reserved,send_budget_limit,send_budget_used,updated_at) "
            "VALUES(?,?,'synthetic','c','f','active',1,true,0,0,?)",(ACCOUNT,FAN,now.isoformat()))
    ledger=Ledger(database[0],allowance=True)
    if service:
        pilot=ledger.activate_service(pilot_id="svc-recovery-proof",account_id=ACCOUNT,fan_id=FAN,owner_token=OWNER,
            cutoff=now-timedelta(seconds=10),ttl_s=20,now=now)
    else:
        pilot=ledger.activate(pilot_id="takeover-proof",account_id=ACCOUNT,fan_id=FAN,owner_token=OWNER,
            cutoff=now-timedelta(seconds=10),deadline=now+timedelta(minutes=10),limit=3,now=now)
    with Session(store.engine) as s,s.begin():
        s.add(Message(account_id=ACCOUNT,fan_id=FAN,message_id=100,direction="in",body="Synthetic inbound",
            created_at=now-timedelta(seconds=5),ingested_at=now-timedelta(seconds=4),media_ids="[]",media_count=0))
    snapshot=ledger.snapshot(pilot_id=pilot.pilot_id,owner_token=OWNER,now=now)
    assert snapshot is not None
    key=ledger.claim_input(pilot_id=pilot.pilot_id,owner_token=OWNER,snapshot=snapshot,now=now)
    args=dict(pilot_id=pilot.pilot_id,owner_token=OWNER,input_key=key,bubble_no=1,revision=pilot.revision,now=now)
    return store,authority,ledger,pilot,args


def takeover(store):
    return store.control(TENANT,ACCOUNT,FAN,mode="human_controlled",action="request_takeover",expected_revision=0,
        command_key="native-takeover",actor="synthetic-operator",reason="Offline takeover")


def state(store):
    with Session(store.engine) as s:
        row=s.get(OfConversationState,(ACCOUNT,FAN))
        return row.takeover_state,row.unresolved_attempt_id,row.revision


async def report(store,ledger,pilot,monkeypatch,*,project_attempt=None):
    from importlib import import_module
    db_engine=import_module("db.engine")
    engine=create_async_engine(store.engine.url.set(drivername="postgresql+asyncpg"))
    factory=async_sessionmaker(engine,expire_on_commit=False)
    @asynccontextmanager
    async def session():
        async with factory() as s:
            yield s
            await s.commit()
    monkeypatch.setattr(db_engine,"get_session",session)
    try:
        if project_attempt is not None:
            import attribution
            from native_pilot.recovery import project
            monkeypatch.setattr(attribution,"get_session",session)
            await project(ledger,pilot_id=pilot.pilot_id,attempt_id=project_attempt,account_id=ACCOUNT,fan_id=FAN)
        return await reporting.report_outcomes(account_id=ACCOUNT,fan_id=FAN,receipts=ledger.receipts(pilot_id=pilot.pilot_id))
    finally:
        await engine.dispose()


def test_control_committed_before_native_admission_prevents_an_attempt(database):
    store,authority,ledger,pilot,args=prepared(database)
    try:
        assert takeover(store)==1 and state(store)==("effective",None,1)
        with pytest.raises(Refused,match="canonical-control-held"):ledger.begin_attempt(**args)
        assert ledger.receipts(pilot_id=pilot.pilot_id)==[]
    finally:store.close()


@pytest.mark.parametrize("outcome",["sent","not_sent","unknown"])
def test_admitted_native_send_keeps_takeover_requested_until_exact_settlement(database,monkeypatch,outcome):
    store,authority,ledger,pilot,args=prepared(database)
    try:
        attempt=ledger.begin_attempt(**args)
        ref=reporting.attempt_ref(attempt.id)
        assert state(store)[1]==ref
        assert takeover(store)==1 and state(store)==("requested",ref,1)
        with pytest.raises(TwinRefused,match="takeover_pending"):
            authority.prepare(TENANT,ACCOUNT,FAN,command_key="too-early",expected_revision=1,
                body="Human reply",origin="manual",actor="synthetic-operator")
        with pytest.raises(Refused,match="canonical-control-held"):
            ledger.validate_dispatch(pilot_id=pilot.pilot_id,owner_token=OWNER,attempt_id=attempt.id,now=utcnow())
        kw=dict(pilot_id=pilot.pilot_id,owner_token=OWNER,attempt_id=attempt.id,now=utcnow())
        if outcome=="sent":
            ledger.record_sent(**kw,provider_message_id="9001",validated_response={"id":9001},evidence={"synthetic":True})
            with Session(store.engine) as s,s.begin():
                s.add(Message(account_id=ACCOUNT,fan_id=FAN,message_id=9001,direction="out",body="Synthetic accepted reply",created_at=utcnow()))
        elif outcome=="not_sent":ledger.record_not_sent(**kw)
        else:ledger.record_unknown(**kw)
        asyncio.run(report(store,ledger,pilot,monkeypatch))
        expected=("requested",ref,1) if outcome=="unknown" else ("effective",None,2)
        assert state(store)==expected
        asyncio.run(report(store,ledger,pilot,monkeypatch))
        assert state(store)==expected, "replayed reporting cannot change settlement or revision"
    finally:store.close()


def test_retained_native_attempt_still_blocks_takeover_when_old_pointer_is_missing(database):
    store,authority,ledger,pilot,args=prepared(database)
    try:
        ledger.begin_attempt(**args)
        with Session(store.engine) as s,s.begin():
            s.get(OfConversationState,(ACCOUNT,FAN)).unresolved_attempt_id=None
        assert takeover(store)==1 and state(store)==("requested",None,1)
    finally:store.close()


def test_native_admission_waits_for_the_same_account_row_as_controls(database,monkeypatch):
    store,authority,ledger,pilot,args=prepared(database)
    entered=threading.Event(); original=ledger._canonical_guard
    def guarded(*a,**kw):
        entered.set()
        return original(*a,**kw)
    monkeypatch.setattr(ledger,"_canonical_guard",guarded)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with Session(store.engine) as s,s.begin():
                s.scalar(select(OfRuntime).where(OfRuntime.account_id==ACCOUNT).with_for_update())
                future=pool.submit(ledger.begin_attempt,**args)
                assert entered.wait(5)
                with pytest.raises(FutureTimeout):future.result(timeout=0.1)
                row=s.get(OfConversationState,(ACCOUNT,FAN));row.mode="human_controlled";row.takeover_state="effective"
            with pytest.raises(Refused,match="canonical-control-held"):future.result(timeout=5)
        assert ledger.receipts(pilot_id=pilot.pilot_id)==[]
    finally:store.close()


def test_proven_first_bubble_does_not_block_a_distinct_second_attempt(database):
    store,authority,ledger,pilot,args=prepared(database)
    try:
        first=ledger.begin_attempt(**args)
        ledger.record_not_sent(pilot_id=pilot.pilot_id,owner_token=OWNER,attempt_id=first.id,now=utcnow())
        second=ledger.begin_attempt(**{**args,"bubble_no":2,"now":args["now"]+timedelta(seconds=2)})
        assert first.id != second.id and state(store)[1]==reporting.attempt_ref(second.id)
    finally:store.close()


def test_stale_settlement_reporting_cannot_clear_a_newer_pointer(database,monkeypatch):
    store,authority,ledger,pilot,args=prepared(database)
    try:
        first=ledger.begin_attempt(**args)
        ledger.record_not_sent(pilot_id=pilot.pilot_id,owner_token=OWNER,attempt_id=first.id,now=utcnow())
        takeover(store)
        with Session(store.engine) as s,s.begin():
            s.get(OfConversationState,(ACCOUNT,FAN)).unresolved_attempt_id="fastt-pilot-999999"
        asyncio.run(report(store,ledger,pilot,monkeypatch))
        assert state(store)==("requested","fastt-pilot-999999",1)
    finally:store.close()
