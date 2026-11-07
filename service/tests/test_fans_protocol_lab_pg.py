"""Documented wire serialization is used by the existing synthetic authority."""
from datetime import datetime, timezone
import hashlib
import hmac
import json
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from conversation_engine.worker import SyntheticConversationWorker
from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import OfTextIntent
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.synthetic_transport import SyntheticFansProvider
from fans_api_adapter.protocol_lab import FansApiProtocolLabTransport


def test_webhook_protocol_mapping_authority_receipt_and_replay(database):
    store, authority = configured(database, mode='automated')
    adapter=FansWebhookAdapter(store)
    epoch=adapter.bind_simulated(TENANT, ACCOUNT, 'acct_protocol', '123')
    from conversation_engine.model_contract import StructuredFixtureModel
    engine=EngineService(store, StructuredFixtureModel({'hello': '{"body":"Hello."}'}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    provider=SyntheticFansProvider(message_id=9901)
    wire=FansApiProtocolLabTransport(store, TENANT, ACCOUNT, provider, provider_account='acct_protocol', creator_id='123', epoch=epoch)
    worker=SyntheticConversationWorker(engine, adapter, wire)
    raw=json.dumps({'event':'messages.received','account_id':'acct_protocol','payload':{'id':1,
        'fromUser':{'id':FAN},'text':'hello','createdAt':datetime.now(timezone.utc).isoformat(),'media':[],'price':0}}).encode()
    sig=hmac.new(b'fixture-secret',raw,hashlib.sha256).hexdigest()
    result=worker.handle(TENANT, ACCOUNT, raw_body=raw, signature=sig, secret=b'fixture-secret', event_key='evt-protocol')
    assert result['outcome'] == 'accepted'
    assert worker.handle(TENANT, ACCOUNT, raw_body=raw, signature=sig, secret=b'fixture-secret', event_key='evt-protocol')['outcome'] == 'accepted'
    assert len(provider.calls) == 1 and provider.calls[0]['idempotency_key'] == result['intentId']
    with Session(database[1]) as s:
        assert s.get(Message, (ACCOUNT, FAN, 9901)).body == 'Hello.'
        assert s.get(OfTextIntent, (ACCOUNT, result['intentId'])).projected_at is not None
    store.close()


def test_timeout_after_acceptance_stays_unknown_through_protocol_mapping(database):
    store, authority = configured(database, mode='automated')
    adapter=FansWebhookAdapter(store); epoch=adapter.bind_simulated(TENANT, ACCOUNT, 'acct_protocol', '123')
    engine=EngineService(store, SyntheticModel({'hello':SyntheticProposal('Hello.')})); engine.select_simulated(TENANT, ACCOUNT, FAN)
    provider=SyntheticFansProvider(('accepted_lost',),message_id=9902)
    wire=FansApiProtocolLabTransport(store, TENANT, ACCOUNT, provider, provider_account='acct_protocol', creator_id='123', epoch=epoch)
    worker=SyntheticConversationWorker(engine, adapter, wire)
    raw=json.dumps({'event':'messages.received','account_id':'acct_protocol','payload':{'id':1,
        'fromUser':{'id':FAN},'text':'hello','createdAt':datetime.now(timezone.utc).isoformat(),'media':[],'price':0}}).encode()
    sig=hmac.new(b'fixture-secret',raw,hashlib.sha256).hexdigest()
    result=worker.handle(TENANT, ACCOUNT, raw_body=raw, signature=sig, secret=b'fixture-secret', event_key='evt-lost')
    assert result['outcome'] == 'unknown'
    assert worker.handle(TENANT, ACCOUNT, raw_body=raw, signature=sig, secret=b'fixture-secret', event_key='evt-lost')['outcome'] == 'unknown'
    assert len(provider.calls) == 1
    with Session(database[1]) as s:
        assert s.get(OfTextIntent, (ACCOUNT, result['intentId'])).status == 'unknown'
        assert s.get(OfConversationState, (ACCOUNT,FAN)).unresolved_attempt_id is not None
    store.close()


def test_delayed_transaction_observation_is_durable_deduplicated_and_unlinked(database):
    from sqlalchemy import select, func
    from db.models import Transaction
    from db.onlyfans_engine_models import OfFansWebhook, OfFansWebhookConflict
    store, authority = configured(database, mode='automated')
    adapter=FansWebhookAdapter(store); epoch=adapter.bind_simulated(TENANT, ACCOUNT, 'acct_protocol', '123')
    wire=FansApiProtocolLabTransport(store,TENANT,ACCOUNT,SyntheticFansProvider(),provider_account='acct_protocol',creator_id='123',epoch=epoch)
    def payload(amount):
        return json.dumps({'event':'transactions.new','account_id':'acct_protocol','payload':{
            'id':'tx-delayed','amount':amount,'currency':'USD','fan':{'id':str(FAN)}}}).encode()
    def accept(raw):
        sig=hmac.new(b'fixture-secret',raw,hashlib.sha256).hexdigest()
        return adapter.accept_protocol_observation(wire.client,raw,sig,b'fixture-secret','evt-financial',tenant=TENANT,account=ACCOUNT)
    assert accept(payload(10)) == 'accepted'
    assert accept(payload(10)) == 'duplicate'
    assert accept(payload(11)) == 'quarantined'
    with Session(database[1]) as s:
        assert s.get(OfFansWebhook,(ACCOUNT,epoch,'evt-financial')).event_type == 'transactions.new'
        assert s.scalar(select(func.count()).select_from(OfFansWebhookConflict)) == 1
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
    store.close()


def test_retired_epoch_cannot_import_a_message_or_financial_event(database):
    import pytest
    from sqlalchemy import select, func
    from db.onlyfans_engine_models import OfFansWebhook
    from onlyfans_runtime.store import Refused
    store, authority = configured(database, mode='automated')
    adapter=FansWebhookAdapter(store); epoch=adapter.bind_simulated(TENANT, ACCOUNT, 'acct_protocol', '123')
    wire=FansApiProtocolLabTransport(store,TENANT,ACCOUNT,SyntheticFansProvider(),provider_account='acct_protocol',creator_id='123',epoch=epoch)
    assert adapter.bind_simulated(TENANT, ACCOUNT, 'acct_protocol', '456') == epoch + 1
    for event, payload in [('transactions.new', {'id':'tx1'}), ('messages.received', {'id':1,
        'fromUser':{'id':FAN},'text':'hello','createdAt':datetime.now(timezone.utc).isoformat(),'media':[],'price':0})]:
        raw=json.dumps({'event':event,'account_id':'acct_protocol','payload':payload}).encode()
        sig=hmac.new(b'fixture-secret',raw,hashlib.sha256).hexdigest()
        with pytest.raises(Refused):
            adapter.accept_protocol_observation(wire.client,raw,sig,b'fixture-secret','evt-stale-'+event,tenant=TENANT,account=ACCOUNT)
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfFansWebhook)) == 0
    store.close()
