"""Connected offline engine path over the canonical inbox and one text authority."""
import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, event
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured, unthrottle
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from conversation_engine.worker import SyntheticConversationWorker
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
from db.models import Message, OfMessageDelivery
from db.onlyfans_engine_models import OfEngineAttempt, OfEngineDecision, OfEngineOwner, OfEngineRequest, OfFansConnection, OfFansWebhookConflict
from onlyfans_read import projections, repo
from onlyfans_read import api as read_api
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import SyntheticTextTransport


def incoming(store, message_id, text):
    assert store.record(TENANT, ACCOUNT, event(mid=message_id, fan=FAN, body=text)) in {'accepted', 'duplicate'}
    store.drain(TENANT, ACCOUNT)


def test_incoming_to_decision_single_authority_receipt_and_dashboard_projection(database, monkeypatch):
    store, authority = configured(database, mode='automated')
    model = SyntheticModel({'What is two plus two?': SyntheticProposal('Two plus two is four.')})
    engine = EngineService(store, model)
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    adapter = FansWebhookAdapter(store)
    assert adapter.bind_simulated(TENANT, ACCOUNT, 'acct_fixture_a', 'creator-fixture') == 1
    payload = {'event': 'messages.received', 'account_id': 'acct_fixture_a',
        'payload': {'id': 1, 'fromUser': {'id': FAN}, 'text': 'What is two plus two?',
                    'createdAt': datetime.now(timezone.utc).isoformat(), 'media': [], 'price': 0}}
    raw = json.dumps(payload, separators=(',', ':')).encode()
    signature = hmac.new(b'fixture-secret', raw, hashlib.sha256).hexdigest()
    provider = SyntheticFansProvider(message_id=9001)
    transport = FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)
    worker = SyntheticConversationWorker(engine, adapter, transport)
    with pytest.raises(Refused, match='scope'):
        worker.handle('other-tenant', ACCOUNT, raw_body=raw, signature=signature,
                      secret=b'fixture-secret', event_key='evt_fixture_1')
    with pytest.raises(Refused, match='scope'):
        adapter.accept(raw, signature, b'fixture-secret', 'evt_fixture_1',
                       expected_tenant=TENANT, expected_account='other-account')
    result = worker.handle(TENANT, ACCOUNT, raw_body=raw, signature=signature,
                           secret=b'fixture-secret', event_key='evt_fixture_1')
    assert result['intake'] == 'accepted' and result['outcome'] == 'accepted'
    decision, intent = result['decisionId'], result['intentId']
    replay = worker.handle(TENANT, ACCOUNT, raw_body=raw, signature=signature,
                           secret=b'fixture-secret', event_key='evt_fixture_1')
    assert replay == {'intake': 'duplicate', 'decisionId': decision, 'intentId': intent, 'outcome': 'accepted'}
    # A new process/store sees the durable decision and does not call a fresh
    # provider or model when the already-completed event is delivered again.
    from onlyfans_runtime.store import Store
    restarted_store = Store(database[0])
    restarted_model = SyntheticModel({})
    restarted_provider = SyntheticFansProvider(())
    restarted_worker = SyntheticConversationWorker(EngineService(restarted_store, restarted_model),
        FansWebhookAdapter(restarted_store), FansApiSyntheticTransport(restarted_store, TENANT, ACCOUNT, restarted_provider))
    assert restarted_worker.handle(TENANT, ACCOUNT, raw_body=raw, signature=signature,
        secret=b'fixture-secret', event_key='evt_fixture_1') == replay
    assert restarted_model.calls == [] and restarted_provider.calls == []
    restarted_store.close()
    assert adapter.accept(raw, signature, b'fixture-secret', 'evt_fixture_redelivery') == 'duplicate'
    altered = json.loads(raw); altered['payload']['text'] = 'different text'
    altered_raw = json.dumps(altered, separators=(',', ':')).encode()
    altered_signature = hmac.new(b'fixture-secret', altered_raw, hashlib.sha256).hexdigest()
    assert adapter.accept(altered_raw, altered_signature, b'fixture-secret', 'evt_fixture_1') == 'quarantined'
    with pytest.raises(Refused):
        adapter.accept(raw, '0' * 64, b'fixture-secret', 'evt_bad')
    with Session(database[1]) as s:
        assert len(list(s.scalars(select(OfFansWebhookConflict)))) == 1
    assert engine.plan(TENANT, ACCOUNT, FAN, 1) == decision
    assert len(model.calls) == 1 and model.calls[0]['history_truncated'] is False
    assert engine.queue(TENANT, ACCOUNT, decision) == intent
    assert len(provider.calls) == 1
    assert provider.calls[0]['idempotency_key'] == intent
    assert provider.calls[0]['account_id'] == 'acct_fixture_a'
    with Session(database[1]) as s:
        d = s.get(OfEngineDecision, (ACCOUNT, decision))
        request = s.get(OfEngineRequest, (ACCOUNT, d.request_id))
        assert d.status == 'completed' and d.intent_id == intent
        assert request.status == 'completed' and request.next_step == 'none'
        assert s.get(Message, (ACCOUNT, FAN, 9001)).body == 'Two plus two is four.'
        receipt = s.get(OfMessageDelivery, (ACCOUNT, FAN, 9001))
        assert receipt.outcome == 'sent' and receipt.origin == 'ai'
        assert 'not a purchase or read receipt' in receipt.detail
    class Adapter:
        def __init__(self, session): self.session = session
        async def execute(self, query): return self.session.execute(query)
        async def get(self, model, key): return self.session.get(model, key)
        async def scalar(self, query): return self.session.scalar(query)
        async def scalars(self, query): return self.session.scalars(query)
        async def run_sync(self, fn): return fn(self.session)
    @asynccontextmanager
    async def sessions():
        with Session(database[1]) as session:
            yield Adapter(session)
    async def scope(session, account):
        assert account == ACCOUNT
    monkeypatch.setattr(repo, 'get_session', sessions)
    monkeypatch.setattr(repo, 'lock_read_scope', scope)
    async def read_dashboard():
        reader = repo.SqlOnlyFansReadRepo()
        rows = await reader.messages(account_id=ACCOUNT, fan_id=FAN, limit=100)
        receipts = await reader.deliveries(account_id=ACCOUNT, fan_id=FAN)
        projected = [projections.message_model(conversation_id=f'{ACCOUNT}:{FAN}', message=row,
                                               recorded=receipts.get(row.message_id)) for row in rows]
        return projected, await reader.engine_status(account_id=ACCOUNT, fan_id=FAN)
    projected, engine_status = asyncio.run(read_dashboard())
    assert [entry['body'] for entry in projected] == ['What is two plus two?', 'Two plus two is four.']
    assert projected[-1]['delivery'] == 'sent' and projected[-1]['origin'] == 'ai'
    assert projected[-1]['attachments']['purchaseStatus'] == 'not_applicable'
    assert engine_status['schemaVersion'] == 'of-engine.v1' and engine_status['state'] == 'current'
    assert engine_status['requests'][0]['status'] == 'completed'
    assert engine_status['requests'][0]['actionOutcome'] == 'accepted'
    # Exercise the actual scoped API envelope consumed by the dashboard.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    reader = repo.SqlOnlyFansReadRepo()
    reader.fan_financials = None
    reader.fan_spend = None
    monkeypatch.setattr(read_api, '_repo', lambda: reader)
    monkeypatch.setenv('ONLYFANS_API_TOKEN', 'fixture-read-token')
    monkeypatch.setenv('ONLYFANS_API_TENANT', TENANT)
    app = FastAPI(); app.include_router(read_api.router)
    headers = {'Authorization': 'Bearer fixture-read-token', 'X-OnlyFans-Schema-Version': 'of-preview.v2'}
    with TestClient(app) as client:
        route = f'/onlyfans/api/v1/accounts/{ACCOUNT}/conversations/{ACCOUNT}:{FAN}'
        response = client.get(route, headers=headers)
        assert response.status_code == 200, response.text
        assert client.get(route, headers={**headers, 'Authorization': 'Bearer wrong'}).status_code == 401
        assert client.get(f'/onlyfans/api/v1/accounts/{ACCOUNT}/conversations/other:{FAN}', headers=headers).status_code == 404
    envelope = response.json()
    assert envelope['conversation']['engineStatus']['requests'][0]['status'] == 'completed'
    assert envelope['conversation']['messages'][-1]['body'] == 'Two plus two is four.'
    artifact = os.environ.get('ENGINE_JOURNEY_ARTIFACT')
    if artifact:
        Path(artifact).write_text(json.dumps(envelope), encoding='utf-8')
    store.close()


def test_correction_owner_change_and_unknown_never_make_a_duplicate_send(database):
    store, authority = configured(database, mode='automated')
    model = SyntheticModel({'first': SyntheticProposal('first reply'), 'correction': SyntheticProposal('corrected reply'),
                            'later': SyntheticProposal('later reply')})
    engine = EngineService(store, model)
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    incoming(store, 1, 'first')
    old = engine.plan(TENANT, ACCOUNT, FAN, 1)
    incoming(store, 2, 'correction')
    with pytest.raises(Refused):
        engine.queue(TENANT, ACCOUNT, old)
    corrected = engine.plan(TENANT, ACCOUNT, FAN, 2)
    intent = engine.queue(TENANT, ACCOUNT, corrected)
    wire = SyntheticTextTransport(ACCOUNT, outcome='timeout')
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'unknown'
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'unknown'
    assert len(wire.calls) == 1
    with pytest.raises(Refused):
        engine.retry_not_sent(TENANT, ACCOUNT, corrected, max_attempts=2)
    with Session(database[1]) as s:
        d = s.get(OfEngineDecision, (ACCOUNT, corrected))
        r = s.get(OfEngineRequest, (ACCOUNT, d.request_id))
        assert d.status == 'unknown' and r.status == 'waiting_provider'
        assert r.next_step == 'reconcile_existing_action'
    incoming(store, 3, 'later')
    later = engine.plan(TENANT, ACCOUNT, FAN, 3)
    pending = model.calls[-1]['pending_actions']
    assert any(row['decision_id'] == corrected and row['outcome'] == 'unknown' for row in pending)
    assert model.calls[-1]['pending_actions_truncated'] is False
    with pytest.raises(Refused):
        engine.queue(TENANT, ACCOUNT, later)
    with Session(database[1]) as s:
        request = s.get(OfEngineRequest, (ACCOUNT, s.get(OfEngineDecision, (ACCOUNT, later)).request_id))
        assert request.status == 'open' and request.next_step == 'review_delivery_dependency'
    store.close()


def test_engine_owner_fences_old_automation_and_takeover_invalidates_queued_reply(database):
    store, authority = configured(database, mode='automated')
    engine = EngineService(store, SyntheticModel({'hello': SyntheticProposal('hello back')}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    incoming(store, 1, 'hello')
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    with pytest.raises(Refused, match='owned by the new engine'):
        authority.prepare(TENANT, ACCOUNT, FAN, command_key='legacy', expected_revision=0,
                          body='legacy reply', origin='automated', actor='legacy')
    intent = engine.queue(TENANT, ACCOUNT, decision)
    store.control(TENANT, ACCOUNT, FAN, mode='human_controlled', expected_revision=0,
                  command_key='takeover', actor='operator', reason='Take responsibility', action='request_takeover')
    wire = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'cancelled'
    assert wire.calls == []
    with Session(database[1]) as s:
        owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
        assert owner.mode == 'human' and owner.epoch == 2
    store.close()


def test_proven_not_sent_retries_same_intent_and_preserves_both_attempts(database):
    store, authority = configured(database, mode='automated')
    adapter = FansWebhookAdapter(store)
    adapter.bind_simulated(TENANT, ACCOUNT, 'acct_fixture_retry', 'creator-fixture')
    engine = EngineService(store, SyntheticModel({'retry': SyntheticProposal('a single reply')}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    incoming(store, 1, 'retry')
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    provider = SyntheticFansProvider(('not_sent', 'accepted'), message_id=9002)
    wire = FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'not_accepted'
    assert engine.retry_not_sent(TENANT, ACCOUNT, decision, max_attempts=2) == intent
    unthrottle(database[1])
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'accepted'
    assert len(provider.calls) == 2
    assert provider.calls[0] == provider.calls[1]
    assert provider.calls[0]['idempotency_key'] == intent
    with Session(database[1]) as s:
        attempts = list(s.scalars(select(OfEngineAttempt).where(OfEngineAttempt.account_id == ACCOUNT,
                                                                 OfEngineAttempt.intent_id == intent)))
        assert {a.outcome for a in attempts} == {'not_accepted', 'accepted'}
        assert len({a.attempt_id for a in attempts}) == 2
        assert all(a.evidence_json for a in attempts)
    store.close()


def test_sales_pause_persists_and_handover_requires_acceptance(database):
    store, authority = configured(database, mode='automated')
    model = SyntheticModel({'no thanks': SyntheticProposal('Of course.', 'decline'),
                            'ordinary chat': SyntheticProposal('Good to hear.'),
                            'show item a': SyntheticProposal('I can help with that.', 'reopen', ('item-a',))})
    engine = EngineService(store, model)
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    incoming(store, 1, 'no thanks'); engine.plan(TENANT, ACCOUNT, FAN, 1)
    incoming(store, 2, 'ordinary chat'); engine.plan(TENANT, ACCOUNT, FAN, 2)
    with Session(database[1]) as s:
        owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
        assert owner.sales_mode == 'paused' and owner.sales_source_message_id == 1
    incoming(store, 3, 'show item a'); decision = engine.plan(TENANT, ACCOUNT, FAN, 3)
    with Session(database[1]) as s:
        owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
        request_id = s.get(OfEngineDecision, (ACCOUNT, decision)).request_id
        assert owner.sales_mode == 'reopened_scoped' and json.loads(owner.sales_scope_json) == ['item-a']
    assert engine.request_handover(TENANT, ACCOUNT, request_id, target='operator') == 'handover_pending'
    with pytest.raises(Refused):
        engine.accept_handover(TENANT, ACCOUNT, request_id, actor='operator')
    store.control(TENANT, ACCOUNT, FAN, mode='human_controlled', expected_revision=0,
                  command_key='takeover-accept', actor='operator', reason='Take responsibility', action='request_takeover')
    assert engine.accept_handover(TENANT, ACCOUNT, request_id, actor='operator') == 'handover_accepted'
    with Session(database[1]) as s:
        request = s.get(OfEngineRequest, (ACCOUNT, request_id))
        assert request.owner == 'operator' and request.status == 'handover_accepted'
        assert request.next_step == 'human_review'
    store.close()


def test_connection_epochs_and_unknown_attempt_block_reconnect(database):
    store, authority = configured(database, mode='automated')
    adapter = FansWebhookAdapter(store)
    assert adapter.bind_simulated(TENANT, ACCOUNT, 'acct_same', 'creator-one') == 1
    assert adapter.bind_simulated(TENANT, ACCOUNT, 'acct_same', 'creator-two') == 2
    with Session(database[1]) as s:
        assert s.get(OfFansConnection, (ACCOUNT, 1)).status == 'retired'
        assert s.get(OfFansConnection, (ACCOUNT, 2)).status == 'active'
    engine = EngineService(store, SyntheticModel({'hello': SyntheticProposal('hello back')}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    incoming(store, 1, 'hello')
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    provider = SyntheticFansProvider(('unknown',))
    wire = FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'unknown'
    with pytest.raises(Refused, match='reconcile'):
        adapter.bind_simulated(TENANT, ACCOUNT, 'acct_next', 'creator-three')
    with Session(database[1]) as s:
        assert s.get(OfFansConnection, (ACCOUNT, 2)).status == 'active'
        assert s.get(OfEngineDecision, (ACCOUNT, decision)).status == 'unknown'
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy import text
    with pytest.raises(DBAPIError, match='connection identity is immutable'):
        with database[1].begin() as c:
            c.execute(text("UPDATE of_fans_connection SET provider_creator_id='changed' WHERE account_id=:account AND epoch=2"), {'account': ACCOUNT})
    from alembic import command
    from db.schema import _config, check_schema
    with pytest.raises(RuntimeError, match='Preserve engine'):
        with database[1].begin() as c:
            command.downgrade(_config(c), '0112_vault_recent_window')
    with database[1].connect() as c:
        check_schema(c)
    store.close()


def test_schema_check_rejects_missing_engine_guards_and_row_security(database):
    from db.schema import check_schema, SchemaMismatch
    from sqlalchemy import text
    guards = [('of_engine_offer_grant', 'of_engine_offer_grant_guard'),
              ('of_engine_access_evidence', 'of_engine_access_immutable'),
              ('of_fans_connection', 'of_fans_connection_guard'),
              ('of_fans_webhook_conflict', 'of_fans_webhook_conflict_immutable'),
              ('of_fans_webhook', 'of_fans_webhook_immutable'),
              ('of_engine_attempt', 'of_engine_attempt_guard'),
              ('of_engine_decision', 'of_engine_decision_guard')]
    for table, trigger in guards:
        with database[1].connect() as c:
            transaction = c.begin()
            c.exec_driver_sql(f'DROP TRIGGER {trigger} ON {table}')
            with pytest.raises(SchemaMismatch, match='engine evidence protection'):
                check_schema(c)
            transaction.rollback()
    with database[1].connect() as c:
        transaction = c.begin()
        c.execute(text('ALTER TABLE of_fans_webhook DISABLE ROW LEVEL SECURITY'))
        with pytest.raises(SchemaMismatch, match='engine row security'):
            check_schema(c)
        transaction.rollback()
    with database[1].connect() as c:
        transaction = c.begin()
        c.execute(text('DROP INDEX uq_fans_provider_account_active'))
        c.execute(text("CREATE UNIQUE INDEX uq_fans_provider_account_active ON of_fans_connection(provider_account_id) WHERE status = 'retired'"))
        with pytest.raises(SchemaMismatch, match='incompatible predicate'):
            check_schema(c)
        transaction.rollback()
    with database[1].connect() as c:
        check_schema(c)


def test_lost_acceptance_is_reconciled_from_exact_fixture_proof_after_restart(database):
    from copy import deepcopy
    from onlyfans_runtime.store import Store
    store, authority = configured(database, mode='automated')
    adapter = FansWebhookAdapter(store)
    adapter.bind_simulated(TENANT, ACCOUNT, 'acct_fixture_lost', 'creator-fixture')
    engine = EngineService(store, SyntheticModel({'question': SyntheticProposal('A factual reply.')}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    incoming(store, 1, 'question')
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    provider = SyntheticFansProvider(('accepted_lost',), message_id=9020)
    wire = FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'unknown'
    assert len(provider.calls) == 1
    store.close()
    restarted = Store(database[0])
    wire = FansApiSyntheticTransport(restarted, TENANT, ACCOUNT, provider)
    original = deepcopy(provider.accepted.pop(intent))
    assert wire.reconcile_accepted(intent) == 'unknown'  # absence proves nothing
    provider.accepted[intent] = deepcopy(original)
    provider.accepted[intent]['_fixture_request']['body']['text'] = 'different payload'
    assert wire.reconcile_accepted(intent) == 'unknown'
    provider.accepted[intent] = original
    assert wire.reconcile_accepted(intent) == 'accepted'
    assert wire.reconcile_accepted(intent) == 'accepted'
    assert len(provider.calls) == 1
    with Session(database[1]) as s:
        d = s.get(OfEngineDecision, (ACCOUNT, decision))
        request = s.get(OfEngineRequest, (ACCOUNT, d.request_id))
        assert d.status == 'completed' and request.status == 'completed'
        assert s.get(Message, (ACCOUNT, FAN, 9020)).body == 'A factual reply.'
        attempts = list(s.scalars(select(OfEngineAttempt).where(OfEngineAttempt.intent_id == intent)))
        assert len(attempts) == 1 and attempts[0].outcome == 'accepted' and attempts[0].evidence_json
    restarted.close()


def test_bounded_history_snapshot_discloses_older_messages(database):
    from onlyfans_runtime.store import utcnow
    store, _ = configured(database, mode='automated')
    model = SyntheticModel({'current': SyntheticProposal('An answer.')})
    engine = EngineService(store, model)
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    incoming(store, 1, 'current')
    with Session(database[1]) as s, s.begin():
        s.add_all(Message(account_id=ACCOUNT, fan_id=FAN, message_id=1000+n, direction='out',
                          body='Historical fixture', created_at=utcnow()-timedelta(days=1, seconds=n))
                  for n in range(101))
    engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert len(model.calls[0]['history']) == 100
    assert model.calls[0]['history_truncated'] is True
    store.close()


def test_concurrent_mode_allows_another_fan_while_first_outcome_is_unknown(database):
    from db.models import Fan, OfConversationState
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    from twin_bridge import store as native_storage
    store, authority = configured(database, mode='automated', limit=5)
    # Install the existing concurrency schema in this disposable fixture DB only.
    native_storage.ensure_runtime_schema(database[0])
    other_fan = FAN + 1
    with Session(database[1]) as s, s.begin():
        s.get(OfDeliveryPolicy, ACCOUNT).delivery_mode = 'concurrent'
        s.add(Fan(account_id=ACCOUNT, fan_id=other_fan))
        s.flush()
        s.add(OfConversationState(account_id=ACCOUNT, fan_id=other_fan, mode='automated',
                                   revision=0, takeover_state='none', requires_attention=False))
    adapter = FansWebhookAdapter(store)
    adapter.bind_simulated(TENANT, ACCOUNT, 'acct_fixture_concurrent', 'creator-fixture')
    engine = EngineService(store, SyntheticModel({'first': SyntheticProposal('first answer'),
                                                   'second': SyntheticProposal('second answer')}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    engine.select_simulated(TENANT, ACCOUNT, other_fan)
    incoming(store, 1, 'first')
    first = engine.queue(TENANT, ACCOUNT, engine.plan(TENANT, ACCOUNT, FAN, 1))
    provider = SyntheticFansProvider(('unknown', 'accepted'), message_id=9030)
    wire = FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)
    assert authority.dispatch(TENANT, ACCOUNT, first, wire) == 'unknown'
    assert store.record(TENANT, ACCOUNT, event(mid=2, fan=other_fan, body='second')) == 'accepted'
    store.drain(TENANT, ACCOUNT)
    second = engine.queue(TENANT, ACCOUNT, engine.plan(TENANT, ACCOUNT, other_fan, 2))
    assert authority.dispatch(TENANT, ACCOUNT, second, wire) == 'accepted'
    with Session(database[1]) as s:
        first_state = s.get(OfConversationState, (ACCOUNT, FAN))
        second_state = s.get(OfConversationState, (ACCOUNT, other_fan))
        assert first_state.unresolved_attempt_id is not None
        assert second_state.unresolved_attempt_id is None
        assert s.get(Message, (ACCOUNT, other_fan, 9030)).body == 'second answer'
    assert len(provider.calls) == 2
    native_storage.dispose_pools()
    store.close()
