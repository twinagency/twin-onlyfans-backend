"""Real loopback HTTP around synthetic-only Fans intake and delivery."""
from datetime import datetime, timezone
from decimal import Decimal
from http.client import HTTPConnection
import hashlib
import hmac
import json
import threading

from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured, unthrottle
from test_engine_offer_pg import setup_offer
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from conversation_engine.worker import SyntheticConversationWorker
from db.models import Message, OfMessageDelivery
from db.onlyfans_engine_models import OfEngineDecision, OfEngineRequest
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.local_http import LocalFansHttpLab, FansApiLocalHttpTransport
from fans_api_adapter.synthetic_transport import SyntheticFansProvider

SECRET = b'synthetic-webhook-secret'


def payload(text='What is two plus two?'):
    return json.dumps({'event':'messages.received', 'account_id':'acct_http_fixture',
        'payload':{'id':1, 'fromUser':{'id':FAN}, 'text':text,
                   'createdAt':datetime.now(timezone.utc).isoformat(), 'media':[], 'price':0}},
        separators=(',', ':')).encode()


def post(lab, raw, key, signature=None):
    signature = signature or hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    connection = HTTPConnection('127.0.0.1', lab.port, timeout=10)
    try:
        connection.request('POST', '/webhooks/fans', body=raw,
            headers={'Content-Type':'application/json', 'X-Webhook-Signature':signature, 'X-Event-Id':key})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def worker(database, provider):
    store, authority = configured(database, mode='automated')
    adapter = FansWebhookAdapter(store)
    adapter.bind_simulated(TENANT, ACCOUNT, 'acct_http_fixture', 'creator-fixture')
    engine = EngineService(store, SyntheticModel({'What is two plus two?': SyntheticProposal('Two plus two is four.')}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    lab = LocalFansHttpLab(provider)
    transport = FansApiLocalHttpTransport(store, TENANT, ACCOUNT, lab)
    lab.attach(SyntheticConversationWorker(engine, adapter, transport), tenant=TENANT, account=ACCOUNT, secret=SECRET)
    return store, authority, engine, lab, transport


def test_http_listener_to_authority_and_receipt_is_durable_and_idempotent(database):
    provider = SyntheticFansProvider(message_id=9401)
    store, authority, engine, lab, transport = worker(database, provider)
    try:
        raw = payload()
        status, result = post(lab, raw, 'http-event-1', signature='0'*64)
        assert status == 401 and provider.calls == []
        malformed = raw[:-1] + b',"invalid":NaN}'
        assert post(lab, malformed, 'http-malformed')[0] == 401
        assert provider.calls == []
        status, ack = post(lab, raw, 'http-event-1')
        assert status == 202 and ack['intake'] == 'accepted' and ack['outcome'] == 'queued'
        result = lab.wait(ack['receipt'])
        assert result['outcome'] == 'accepted'
        status, replay = post(lab, raw, 'http-event-1')
        assert status == 202 and replay['intake'] == 'duplicate'
        assert lab.wait(replay['receipt']) == result
        assert len(provider.calls) == 1
        request = provider.calls[0]
        assert request['account_id'] == 'acct_http_fixture' and request['chat_id'] == str(FAN)
        assert request['idempotency_key'] == result['intentId']
        assert request['body'] == {'text':'Two plus two is four.'}
        conflict = payload('A conflicting event')
        assert post(lab, conflict, 'http-event-1')[1]['outcome'] == 'quarantined'
        assert len(provider.calls) == 1
        with Session(database[1]) as s:
            assert s.get(Message, (ACCOUNT, FAN, 9401)).body == 'Two plus two is four.'
            assert s.get(OfMessageDelivery, (ACCOUNT, FAN, 9401)).outcome == 'sent'
            decision = s.get(OfEngineDecision, (ACCOUNT, result['decisionId']))
            assert s.get(OfEngineRequest, (ACCOUNT, decision.request_id)).status == 'completed'
    finally:
        lab.close(); store.close()


def test_http_lost_acceptance_stays_unknown_until_exact_synthetic_reconciliation(database):
    provider = SyntheticFansProvider(('accepted_lost',), message_id=9402)
    store, authority, engine, lab, transport = worker(database, provider)
    try:
        raw = payload()
        status, ack = post(lab, raw, 'http-lost')
        assert status == 202 and ack['intake'] == 'accepted'
        result = lab.wait(ack['receipt'])
        assert result['outcome'] == 'unknown'
        replay = post(lab, raw, 'http-lost')[1]
        assert lab.wait(replay['receipt'])['outcome'] == 'unknown'
        assert len(provider.calls) == 1
        assert transport.reconcile_accepted(result['intentId']) == 'accepted'
        assert len(provider.calls) == 1
        with Session(database[1]) as s:
            assert s.get(Message, (ACCOUNT, FAN, 9402)).body == 'Two plus two is four.'
            assert s.get(OfEngineDecision, (ACCOUNT, result['decisionId'])).status == 'completed'
    finally:
        lab.close(); store.close()


def test_http_paid_offer_preserves_price_and_media(database):
    store, authority, engine, decision, item_id, grant = setup_offer(database)
    provider = SyntheticFansProvider(message_id=9403)
    with LocalFansHttpLab(provider) as lab:
        transport = FansApiLocalHttpTransport(store, TENANT, ACCOUNT, lab)
        intent = engine.queue(TENANT, ACCOUNT, decision)
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'accepted'
        sent = provider.calls[0]
        assert sent['body']['price'] == Decimal('10.00')
        assert sent['body']['mediaFiles'] == ['11','12','13']
        assert sent['body']['previews'] == ['11']
        with Session(database[1]) as s:
            assert s.get(Message, (ACCOUNT, FAN, 9403)).is_paid is None
    store.close()


def test_http_worker_resumes_ordered_parts_without_resending_earlier_parts(database):
    provider = SyntheticFansProvider(('accepted','accepted','accepted'), message_id=(9411,9412,9413))
    store, authority = configured(database, mode='automated', limit=8)
    adapter = FansWebhookAdapter(store)
    adapter.bind_simulated(TENANT, ACCOUNT, 'acct_http_fixture', 'creator-fixture')
    engine = EngineService(store, SyntheticModel({'hello':SyntheticProposal('First.', parts=('Second.', 'Third.'))}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    with LocalFansHttpLab(provider) as lab:
        transport = FansApiLocalHttpTransport(store, TENANT, ACCOUNT, lab)
        lab.attach(SyntheticConversationWorker(engine, adapter, transport), tenant=TENANT, account=ACCOUNT, secret=SECRET)
        raw = payload('hello')
        status, ack = post(lab, raw, 'http-sequence')
        assert status == 202 and ack['intake'] == 'accepted'
        result = lab.wait(ack['receipt'])
        assert result['parts'][0]['outcome'] == 'accepted'
        assert result['parts'][-1]['outcome'] == 'queued'
        unthrottle(database[1])
        result = lab.wait(post(lab, raw, 'http-sequence')[1]['receipt'])
        assert result['parts'][1]['outcome'] == 'accepted' and result['parts'][-1]['outcome'] == 'queued'
        unthrottle(database[1])
        result = lab.wait(post(lab, raw, 'http-sequence')[1]['receipt'])
        assert result['outcome'] == 'accepted' and len(result['parts']) == 3
        assert len(provider.calls) == 3 and len({call['idempotency_key'] for call in provider.calls}) == 3
        with Session(database[1]) as s:
            assert [s.get(Message, (ACCOUNT, FAN, mid)).body for mid in (9411,9412,9413)] == ['First.','Second.','Third.']
    store.close()


def test_http_acknowledges_committed_inbox_while_model_is_still_blocked(database):
    from db.onlyfans_engine_models import OfFansWebhook
    provider = SyntheticFansProvider(message_id=9421)
    store, authority, engine, lab, transport = worker(database, provider)
    entered, release = threading.Event(), threading.Event()
    generate = engine.model.generate
    def slow(snapshot):
        entered.set()
        assert release.wait(10)
        return generate(snapshot)
    engine.model.generate = slow
    try:
        status, ack = post(lab, payload(), 'http-quick-ack')
        assert status == 202 and ack['intake'] == 'accepted' and entered.wait(3)
        assert not release.is_set() and provider.calls == []
        with Session(database[1]) as s:
            assert s.get(OfFansWebhook, (ACCOUNT, 1, 'http-quick-ack')).status == 'accepted'
        release.set()
        assert lab.wait(ack['receipt'])['outcome'] == 'accepted'
    finally:
        release.set(); lab.close(); store.close()


def test_restart_recovers_from_durable_authenticated_webhook_without_redelivery(database):
    provider = SyntheticFansProvider(message_id=9422)
    store, authority = configured(database, mode='automated')
    adapter = FansWebhookAdapter(store)
    adapter.bind_simulated(TENANT, ACCOUNT, 'acct_http_fixture', 'creator-fixture')
    engine = EngineService(store, SyntheticModel({'What is two plus two?': SyntheticProposal('Two plus two is four.')}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    raw = payload()
    signature = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    # Simulate process loss after the intake transaction, before scheduling.
    assert adapter.accept(raw, signature, SECRET, 'http-restart', expected_tenant=TENANT, expected_account=ACCOUNT) == 'accepted'
    with LocalFansHttpLab(provider) as restarted:
        transport = FansApiLocalHttpTransport(store, TENANT, ACCOUNT, restarted)
        restarted.attach(SyntheticConversationWorker(engine, adapter, transport),
                         tenant=TENANT, account=ACCOUNT, secret=SECRET)
        result = restarted.wait_recovery('http-restart')
        assert result['outcome'] == 'accepted' and len(provider.calls) == 1
        assert restarted.wait(restarted.recover('http-restart'))['outcome'] == 'accepted'
        assert len(provider.calls) == 1
        with Session(database[1]) as s:
            assert s.get(Message, (ACCOUNT, FAN, 9422)).body == 'Two plus two is four.'
    store.close()


def test_restart_maintenance_preserves_expired_wire_as_unknown(database):
    from sqlalchemy import text
    from db.onlyfans_delivery_models import OfTextIntent
    from test_engine_simulated_journey_pg import incoming
    provider = SyntheticFansProvider(message_id=9431)
    store, authority, engine, lab, transport = worker(database, provider)
    lab.close()
    incoming(store, 1, 'What is two plus two?')
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    grant = authority.admit(TENANT, ACCOUNT, intent)
    assert authority.consume_dispatch(TENANT, grant) is not None
    with database[1].begin() as connection:
        connection.execute(text("UPDATE of_text_intent SET deadline=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE intent_id=:id"), {'id':intent})
    with LocalFansHttpLab(provider) as restarted:
        new_wire = FansApiLocalHttpTransport(store, TENANT, ACCOUNT, restarted)
        restarted.attach(SyntheticConversationWorker(engine, FansWebhookAdapter(store), new_wire),
                         tenant=TENANT, account=ACCOUNT, secret=SECRET)
        restarted.wait_maintenance()
        with Session(database[1]) as s:
            assert s.get(OfTextIntent, (ACCOUNT, intent)).status == 'unknown'
            assert s.get(OfEngineDecision, (ACCOUNT, decision)).status == 'unknown'
        assert provider.calls == []
    store.close()


def test_restart_maintenance_projects_saved_acceptance_without_sending_again(database):
    from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport
    from db.onlyfans_delivery_models import OfTextIntent
    from test_engine_simulated_journey_pg import incoming
    provider = SyntheticFansProvider(message_id=9432)
    store, authority, engine, lab, transport = worker(database, provider)
    lab.close()
    incoming(store, 1, 'What is two plus two?')
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    grant = authority.admit(TENANT, ACCOUNT, intent)
    wire = FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)
    proof = wire.send(grant, authority=authority, tenant_id=TENANT)
    assert authority.record_accepted(TENANT, ACCOUNT, intent, grant.token, proof) == 'accepted'
    with Session(database[1]) as s:
        assert s.get(OfTextIntent, (ACCOUNT, intent)).projected_at is None
    with LocalFansHttpLab(provider) as restarted:
        new_wire = FansApiLocalHttpTransport(store, TENANT, ACCOUNT, restarted)
        restarted.attach(SyntheticConversationWorker(engine, FansWebhookAdapter(store), new_wire),
                         tenant=TENANT, account=ACCOUNT, secret=SECRET)
        restarted.wait_maintenance()
        with Session(database[1]) as s:
            assert s.get(Message, (ACCOUNT, FAN, 9432)).body == 'Two plus two is four.'
            assert s.get(OfTextIntent, (ACCOUNT, intent)).projected_at is not None
            assert s.get(OfEngineDecision, (ACCOUNT, decision)).status == 'completed'
        assert len(provider.calls) == 1
    store.close()


def test_saturated_local_queue_recovers_the_already_committed_event(database, monkeypatch):
    import queue
    provider = SyntheticFansProvider(message_id=9433)
    store, authority, engine, lab, transport = worker(database, provider)
    def full(_):
        raise queue.Full()
    monkeypatch.setattr(lab._jobs, 'put_nowait', full)
    try:
        status, ack = post(lab, payload(), 'http-backpressure')
        assert status == 202 and ack['intake'] == 'accepted' and ack['outcome'] == 'deferred'
        assert lab.wait_recovery('http-backpressure')['outcome'] == 'accepted'
        assert len(provider.calls) == 1
    finally:
        lab.close(); store.close()
