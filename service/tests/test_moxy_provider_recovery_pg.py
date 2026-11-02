"""Provider fault recovery through the real authority, with simulated wires."""
from datetime import timedelta
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_moxy_integrated_pg import setup,run,decision,model
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT,unthrottle
from db.onlyfans_delivery_models import OfTextIntent,OfDeliveryPolicy
from db.onlyfans_moxy_models import MoxyEvent
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport,ProviderThrottled


def expire_cooldown(engine):
    with Session(engine) as s,s.begin():
        s.get(OfDeliveryPolicy,ACCOUNT).provider_cooldown_until=utcnow()-timedelta(seconds=1)
    unthrottle(engine)


@pytest.mark.parametrize('action',['reply','offer'])
def test_definite_rate_limit_retries_new_attempt_without_repeating_model(database,monkeypatch,action):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity,generator=run(service,engine,decision(action,'Here is the outfit photo.' if action=='offer' else 'How was your day?',
        'first' if action=='offer' else None,sequence='fixture_level' if action=='offer' else None))
    first=service.queue(TENANT,ACCOUNT,identity)
    original=SyntheticTextTransport.send
    def throttled(*args,**kwargs):raise ProviderThrottled(retry_after=30)
    monkeypatch.setattr(SyntheticTextTransport,'send',throttled)
    assert authority.dispatch(TENANT,ACCOUNT,first,SyntheticTextTransport(ACCOUNT))=='not_accepted'
    fresh=model(decision())
    assert service.step(TENANT,ACCOUNT,fresh)==0
    assert fresh.calls==[]
    with Session(engine) as s:
        old=s.get(OfTextIntent,(ACCOUNT,first));proof=old.receipt_json
        assert s.get(MoxyEvent,(ACCOUNT,identity)).reason=='provider_rate_limited'
    expire_cooldown(engine)
    assert service.step(TENANT,ACCOUNT,fresh)==1
    with Session(engine) as s:
        second=s.get(MoxyEvent,(ACCOUNT,identity)).intent_id
        assert second!=first
        assert s.get(OfTextIntent,(ACCOUNT,first)).receipt_json==proof
    monkeypatch.setattr(SyntheticTextTransport,'send',original)
    assert authority.dispatch(TENANT,ACCOUNT,second,SyntheticTextTransport(ACCOUNT,message_id=19001))=='accepted'
    assert fresh.calls==[] and len(generator.calls)==1
    with Session(engine) as s:assert s.get(MoxyEvent,(ACCOUNT,identity)).status=='done'
    store.close()


def test_repeated_rate_limits_stop_after_three_proved_rejections(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity,_=run(service,engine,decision())
    intent=service.queue(TENANT,ACCOUNT,identity)
    def throttled(*args,**kwargs):raise ProviderThrottled(retry_after=1)
    monkeypatch.setattr(SyntheticTextTransport,'send',throttled)
    fresh=model(decision())
    for n in range(3):
        assert authority.dispatch(TENANT,ACCOUNT,intent,SyntheticTextTransport(ACCOUNT))=='not_accepted'
        expire_cooldown(engine)
        assert service.step(TENANT,ACCOUNT,fresh)==(1 if n<2 else 0)
        with Session(engine) as s:intent=s.get(MoxyEvent,(ACCOUNT,identity)).intent_id
    with Session(engine) as s:
        assert s.get(MoxyEvent,(ACCOUNT,identity)).reason=='provider_retry_limit_reached'
        assert len(s.scalars(select(OfTextIntent).where(OfTextIntent.moxy_event_id==identity)).all())==3
    assert fresh.calls==[]
    store.close()


def test_authentication_rejection_is_owned_hold_and_never_retried(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity,_=run(service,engine,decision())
    intent=service.queue(TENANT,ACCOUNT,identity)
    class AuthenticationFailure(Exception):status_code=401
    def rejected(*args,**kwargs):raise AuthenticationFailure()
    monkeypatch.setattr(SyntheticTextTransport,'send',rejected)
    assert authority.dispatch(TENANT,ACCOUNT,intent,SyntheticTextTransport(ACCOUNT))=='not_accepted'
    expire_cooldown(engine)
    fresh=model(decision())
    assert service.step(TENANT,ACCOUNT,fresh)==0
    with Session(engine) as s:
        row=s.get(MoxyEvent,(ACCOUNT,identity))
        assert row.status=='held' and row.reason=='provider_authentication_required'
        assert s.get(OfTextIntent,(ACCOUNT,intent)).status=='not_accepted'
    assert fresh.calls==[]
    store.close()


@pytest.mark.parametrize('mutation',['price','fan_constraint','off'])
def test_rate_limit_recovery_revalidates_before_new_attempt(database,monkeypatch,mutation):
    import json
    from db.models import CatalogItem
    from db.onlyfans_moxy_models import MoxyFan
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    identity,_=run(service,engine,decision('offer','Here is the outfit photo.','first',sequence='fixture_level'))
    intent=service.queue(TENANT,ACCOUNT,identity)
    def throttled(*args,**kwargs):raise ProviderThrottled(retry_after=1)
    monkeypatch.setattr(SyntheticTextTransport,'send',throttled)
    assert authority.dispatch(TENANT,ACCOUNT,intent,SyntheticTextTransport(ACCOUNT))=='not_accepted'
    expire_cooldown(engine)
    if mutation=='off':
        service.configure(TENANT,ACCOUNT,expected_revision=1,mode='off',settings=settings,actor='fixture-operator')
    else:
        with Session(engine) as s,s.begin():
            if mutation=='price':s.get(CatalogItem,settings['catalogue_bindings']['first']).price_cents+=100
            else:
                row=s.get(MoxyFan,(ACCOUNT,101));row.state_json=json.dumps({'sales_refused':True});row.revision+=1
    fresh=model(decision())
    assert service.step(TENANT,ACCOUNT,fresh)==0
    with Session(engine) as s:
        row=s.get(MoxyEvent,(ACCOUNT,identity))
        assert row.status in {'stale','held'}
        assert row.intent_id==intent
        assert len(s.scalars(select(OfTextIntent).where(OfTextIntent.moxy_event_id==identity)).all())==1
    assert fresh.calls==[]
    store.close()
