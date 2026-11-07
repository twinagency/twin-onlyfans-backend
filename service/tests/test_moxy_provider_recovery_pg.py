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


def test_scoped_retirement_rearms_only_new_input_and_keeps_late_receipt(database, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from db.models import OfConversationState
    from db.onlyfans_models import OfWork
    from native_pilot.scope import AccountScope
    from onlyfans_runtime import native_protocol, send_recovery
    from onlyfans_runtime.store import Refused
    from onlyfans_runtime.text_delivery import Accepted
    from moxy_replacement.runtime import snapshot
    from test_onlyfans_foundation_pg import event

    engine, store, service, authority, _, settings = setup(database, monkeypatch)
    original, _ = run(service, engine, decision(), payload={'source_message_id': 1})
    intent_id = service.queue(TENANT, ACCOUNT, original)
    wire = SyntheticTextTransport(ACCOUNT, outcome='timeout')
    assert authority.dispatch(TENANT, ACCOUNT, intent_id, wire) == 'unknown'
    settings['pilot'] = {'fan_ids': [101], 'product_ids': [p['id'] for p in settings['products']],
                         'supervisor': settings['operational']['assigned_operator']}
    service.configure(TENANT, ACCOUNT, expected_revision=1, mode='off',
                      settings=settings, actor='fixture-operator')
    store.record(TENANT, ACCOUNT, event(mid=2, body='A new question'))
    store.drain(TENANT, ACCOUNT)
    with Session(engine) as session, session.begin():
        intent = session.get(OfTextIntent, (ACCOUNT, intent_id))
        intent.expires_at = intent.deadline = utcnow() - timedelta(seconds=1)
        policy = session.get(OfDeliveryPolicy, ACCOUNT)
        policy.transport_kind = 'native'
        policy.delivery_mode = 'gap'
        control = session.get(OfConversationState, (ACCOUNT, 101))
        work = session.get(OfWork, (ACCOUNT, 101))
        assert work.target_message_id == 2 and work.status == 'blocked'
        attempt, revision, used = intent.attempt_id, control.revision, policy.used_units

    monkeypatch.setattr(AccountScope, 'parse', staticmethod(lambda _: SimpleNamespace(provider_id='901')))
    monkeypatch.setattr(native_protocol, 'message', lambda raw, **_: SimpleNamespace(
        message_id=raw['id'], direction=raw['direction']))
    args = dict(expected_attempt_id=attempt, expected_revision=revision, next_input_id=2,
                complete=True, observed_at=utcnow(), command_key='b' * 64,
                actor='op-session:' + 'a' * 64, reason='Verified inbound after expired text attempt')
    with pytest.raises(Refused, match='positive reconciliation'):
        send_recovery.retire_unconfirmed_text(authority, TENANT, ACCOUNT, 101, intent_id,
            history=[{'id': 3, 'direction': 'out'}, {'id': 2, 'direction': 'in'}], **args)
    result = send_recovery.retire_unconfirmed_text(authority, TENANT, ACCOUNT, 101, intent_id,
        history=[{'id': 2, 'direction': 'in'}], **args)
    assert result == {'retired': True, 'deliveryOutcome': 'unconfirmed', 'nextInputId': 2}
    assert send_recovery.retire_unconfirmed_text(authority, TENANT, ACCOUNT, 101, intent_id,
        history=[{'id': 2, 'direction': 'in'}], **args) == result

    fresh = service.add_event(TENANT, ACCOUNT, 101, 'turn', 'input:2', {'source_message_id': 2})
    with Session(engine) as session, session.begin():
        intent = session.get(OfTextIntent, (ACCOUNT, intent_id))
        policy = session.get(OfDeliveryPolicy, ACCOUNT)
        assert intent.status == 'cancelled' and intent.receipt_json is None
        assert intent.reservation_held and policy.used_units == used and policy.active_intent_id is None
        assert session.get(OfConversationState, (ACCOUNT, 101)).unresolved_attempt_id is None
        assert session.get(OfWork, (ACCOUNT, 101)).status == 'waiting'
        new_snapshot = snapshot(session, ACCOUNT, 101, session.get(MoxyEvent, (ACCOUNT, fresh)))
        assert [m['id'] for m in new_snapshot['pending_fan_messages']] == [2]

    saved = send_recovery.snapshot(authority, TENANT, ACCOUNT, 101, intent_id)
    proof = Accepted(ACCOUNT, 101, intent_id, attempt, saved['payload_hash'], 18722,
                     datetime.now(timezone.utc).isoformat(), 'synthetic:late-provider-proof')
    assert authority.record_accepted(TENANT, ACCOUNT, intent_id, saved['token'], proof) == 'accepted'
    assert authority.materialize(TENANT, ACCOUNT, intent_id)
    with Session(engine) as session:
        old_event = session.get(MoxyEvent, (ACCOUNT, original))
        assert old_event.status == 'stale' and old_event.reason == send_recovery.RETIRED_UNCONFIRMED_TEXT
        assert session.get(OfTextIntent, (ACCOUNT, intent_id)).provider_message_id == 18722
        assert session.get(MoxyEvent, (ACCOUNT, fresh)).status == 'pending'
    assert len(wire.calls) == 1
    store.close()
