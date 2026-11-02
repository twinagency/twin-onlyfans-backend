"""Bounded synthetic Moxy load, not a live-provider latency claim."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_moxy_integrated_pg import setup,model,decision
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT
from db.models import Fan,OfConversationState,Message
from db.onlyfans_moxy_models import MoxyEvent
from db.onlyfans_delivery_models import OfDeliveryPolicy,OfTextIntent
from onlyfans_runtime.text_delivery import SyntheticTextTransport


@pytest.mark.parametrize('creator',['amelia','cristina'])
def test_one_hundred_fans_keep_distinct_decisions_and_receipts(database,monkeypatch,creator):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch,creator)
    from twin_bridge.store import ensure_runtime_schema
    ensure_runtime_schema(database[0])
    # Match the existing production inbox worker's eight-connection pool.
    store.close()
    from onlyfans_runtime.store import Store
    from moxy_replacement.runtime import MoxyService
    store=Store(database[0],pool_size=8)
    service=MoxyService(store);authority=service.authority
    fans=list(range(201,301))
    with Session(engine) as s,s.begin():
        policy=s.get(OfDeliveryPolicy,ACCOUNT)
        # Explicit fixture settings. No production/provider capacity inference.
        policy.limit_units=200;policy.delivery_mode='concurrent'
        policy.provider_requests_per_second=20;policy.provider_burst=4
        policy.provider_max_concurrent=4
        for fan in fans:
            s.add(Fan(account_id=ACCOUNT,fan_id=fan));s.flush()
            s.add(OfConversationState(account_id=ACCOUNT,fan_id=fan,mode='automated',
                revision=0,input_revision=0,takeover_state='none'))
    ids=[service.add_event(TENANT,ACCOUNT,fan,'scheduled','load:'+str(fan),{}) for fan in fans]
    generator=model(decision(text='How has your day been?'))
    start=time.monotonic()
    for _ in range(25):assert service.step(TENANT,ACCOUNT,generator,limit=4)==4
    prepared=time.monotonic()-start
    assert len(generator.calls)==100
    with Session(engine) as s:
        rows=s.scalars(select(MoxyEvent).where(MoxyEvent.event_id.in_(ids))).all()
        assert len(rows)==100 and all(r.status=='queued' for r in rows)
        intents=[r.intent_id for r in rows];assert len(set(intents))==100
    provider_ids={identity:19000+n for n,identity in enumerate(intents)}
    deadline=time.monotonic()+120
    def send(identity):
        wire=SyntheticTextTransport(ACCOUNT,message_id=provider_ids[identity])
        while time.monotonic()<deadline:
            result=authority.dispatch(TENANT,ACCOUNT,identity,wire)
            if result=='accepted':
                assert len(wire.calls)==1
                return identity
            assert result=='queued',result
            time.sleep(.1)
        raise AssertionError('Synthetic load did not finish within its bounded test budget')
    with ThreadPoolExecutor(max_workers=4) as pool:assert set(pool.map(send,intents))==set(intents)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=='out'))==100
        assert all(s.get(MoxyEvent,(ACCOUNT,i)).status=='done' for i in ids)
        assert all(s.get(OfTextIntent,(ACCOUNT,i)).status=='accepted' for i in intents)
    print(json.dumps({'creator':creator,'fans':100,'synthetic_prepare_seconds':round(prepared,3),
        'synthetic_total_seconds':round(time.monotonic()-start,3),'real_model':False,'real_provider':False}))
    store.close()
