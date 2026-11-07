"""Orchestration tests (plan -> queue -> dispatch, synthetic provider) for the engine defect fixes."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta, timezone, datetime
import hashlib
import hmac
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, event  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured, unthrottle
from test_engine_memory_pg import setup, correct
from test_engine_simulated_journey_pg import incoming
from test_engine_offer_pg import setup_offer
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from conversation_engine.model_contract import StructuredFixtureModel
from db.models import Message
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineOwner, OfEngineRequest
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import TextAuthority


def wire_for(store, provider):
    return FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)


def run(store, authority, engine, mid, provider=None, wire=None):
    decision = engine.plan(TENANT, ACCOUNT, FAN, mid)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    return decision, intent, authority.dispatch(TENANT, ACCOUNT, intent, wire or wire_for(store, provider))


def decisions(database, request_id=None):
    with Session(database[1]) as s:
        query = select(OfEngineDecision).order_by(OfEngineDecision.generation, OfEngineDecision.part_index)
        return [(d.generation, d.part_index, d.status, d.decision_id) for d in s.scalars(query)]


# ---- R1 -------------------------------------------------------------------
def test_r1_paused_plain_replies_cannot_pitch_price_or_claim_purchase(database):
    store, authority, engine, model, memory = setup(database)
    engine.model = SyntheticModel({
        'no thanks': SyntheticProposal('Understood.', sales_signal='decline'),
        'q1': SyntheticProposal('Buy the set for USD 1.00. You already purchased it.'),
        'q2': SyntheticProposal('Happy to help with anything else.', pitch=True),
        'q3': SyntheticProposal('It is a short video.', price_claims=(100,)),
        'q4': SyntheticProposal('It is a short video.', purchase_claim=True),
        'q5': SyntheticProposal('You can unlock it any time you like.'),
        'q6': SyntheticProposal('The video is about two minutes long.')})
    provider = SyntheticFansProvider(('accepted', 'accepted'), message_id=(9001, 9002))
    wire = wire_for(store, provider)
    incoming(store, 1, 'no thanks')
    assert run(store, authority, engine, 1, wire=wire)[2] == 'accepted'
    with Session(database[1]) as s:
        assert s.get(OfEngineOwner, (ACCOUNT, FAN)).sales_mode == 'paused'
    for mid, body in enumerate(['q1', 'q2', 'q3', 'q4', 'q5'], start=2):
        incoming(store, mid, body)
        with pytest.raises(Refused, match='sales are paused|unsupported|verified offer'):
            engine.plan(TENANT, ACCOUNT, FAN, mid)
    assert len(provider.calls) == 1  # nothing commercial reached the wire
    with Session(database[1]) as s:
        assert len(s.scalars(select(OfEngineDecision)).all()) == 1  # no decision for a refused proposal
    unthrottle(database[1]); incoming(store, 7, 'q6')
    assert run(store, authority, engine, 7, wire=wire)[2] == 'accepted'
    assert provider.calls[-1]['body']['text'] == 'The video is about two minutes long.'
    store.close()


def test_r1_structured_contract_carries_commercial_declarations(database):
    store, authority, engine, model, memory = setup(database)
    engine.model = StructuredFixtureModel({
        'a': json.dumps({'body': 'Here you go.', 'pitch': True}),
        'b': json.dumps({'body': 'Fine.', 'price_claims': [500]}),
        'c': json.dumps({'body': 'Fine.', 'purchase_claim': True}),
        'd': json.dumps({'body': 'Fine.', 'price_claims': ['5']})})
    for mid, body in enumerate('abc', start=1):
        incoming(store, mid, body)
        with pytest.raises(Refused):
            engine.plan(TENANT, ACCOUNT, FAN, mid)
    incoming(store, 4, 'd')
    with pytest.raises(Refused, match='contract'):
        engine.plan(TENANT, ACCOUNT, FAN, 4)
    store.close()


# ---- R2 -------------------------------------------------------------------
def test_r2_cancelled_decision_gets_fresh_generation_and_sends_once(database):
    store, authority, engine, model, memory = setup(database)
    incoming(store, 1, 'hello')
    first = engine.plan(TENANT, ACCOUNT, FAN, 1)
    intent = engine.queue(TENANT, ACCOUNT, first)
    correct(memory, value='Perth', key='audit-correction')
    provider = SyntheticFansProvider(message_id=9001)
    wire = wire_for(store, provider)
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'cancelled' and provider.calls == []
    second = engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert second != first and len(model.calls) == 2
    assert engine.plan(TENANT, ACCOUNT, FAN, 1) == second  # idempotent, no third generation
    assert len(model.calls) == 2
    second_intent = engine.queue(TENANT, ACCOUNT, second)
    assert second_intent != intent
    assert authority.dispatch(TENANT, ACCOUNT, second_intent, wire) == 'accepted'
    assert len(provider.calls) == 1
    assert [row[:3] for row in decisions(database)] == [(0, 0, 'cancelled'), (1, 0, 'completed')]
    with Session(database[1]) as s:
        request = s.scalar(select(OfEngineRequest))
        assert request.status == 'completed'
        assert s.get(OfTextIntent, (ACCOUNT, intent)).status == 'cancelled'  # old evidence untouched
    assert engine.plan(TENANT, ACCOUNT, FAN, 1) == second  # completed work is never regenerated
    store.close()


def test_r2_unknown_send_is_never_regenerated(database):
    store, authority, engine, model, memory = setup(database)
    incoming(store, 1, 'hello')
    provider = SyntheticFansProvider(('unknown',))
    decision, intent, outcome = run(store, authority, engine, 1, provider)
    assert outcome == 'unknown'
    assert engine.plan(TENANT, ACCOUNT, FAN, 1) == decision
    assert len(model.calls) == 1 and len(provider.calls) == 1
    assert [row[2] for row in decisions(database)] == ['unknown']
    store.close()


def test_r2_concurrent_regenerators_create_one_generation(database):
    store, authority, engine, model, memory = setup(database)
    incoming(store, 1, 'hello')
    first = engine.plan(TENANT, ACCOUNT, FAN, 1)
    intent = engine.queue(TENANT, ACCOUNT, first)
    correct(memory, value='Perth', key='c1')
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire_for(store, SyntheticFansProvider())) == 'cancelled'
    original = model.generate
    winners = []
    def racing(snapshot):
        result = original(snapshot)
        if not winners:
            model.generate = original
            winners.append(engine.plan(TENANT, ACCOUNT, FAN, 1))  # a competing regenerator finishes first
        return result
    model.generate = racing
    final = engine.plan(TENANT, ACCOUNT, FAN, 1)
    assert final == winners[0]
    assert [row[:3] for row in decisions(database)] == [(0, 0, 'cancelled'), (1, 0, 'ready')]
    store.close()


def test_r2_multipart_generation_scopes_parts(database):
    store, authority, engine, model, memory = setup(database)
    engine.model = SyntheticModel({'hello': SyntheticProposal('One.', parts=('Two.',))})
    incoming(store, 1, 'hello')
    first = engine.plan(TENANT, ACCOUNT, FAN, 1)
    parts = engine.sequence(TENANT, ACCOUNT, first)
    assert len(parts) == 2
    intent = engine.queue(TENANT, ACCOUNT, parts[0])
    correct(memory, value='Perth', key='mp1')
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire_for(store, SyntheticFansProvider())) == 'cancelled'
    assert [row[2] for row in decisions(database)] == ['cancelled', 'cancelled']
    second = engine.plan(TENANT, ACCOUNT, FAN, 1)
    new_parts = engine.sequence(TENANT, ACCOUNT, second)
    assert len(new_parts) == 2 and not set(new_parts) & set(parts)
    provider = SyntheticFansProvider(('accepted', 'accepted'), message_id=(9001, 9002))
    wire = wire_for(store, provider)
    for number, part in enumerate(new_parts):
        if number:
            unthrottle(database[1])
        assert authority.dispatch(TENANT, ACCOUNT, engine.queue(TENANT, ACCOUNT, part), wire) == 'accepted'
    assert [call['body']['text'] for call in provider.calls] == ['One.', 'Two.']
    store.close()


# ---- R6 -------------------------------------------------------------------
def test_r6_answer_and_offer_are_both_kept(database):
    store, authority, engine, decision, item, grant = setup_offer(database)
    with Session(database[1]) as s:
        row = s.get(OfEngineDecision, (ACCOUNT, decision))
        assert row.action == 'offer' and row.status == 'ready'
        assert row.body.startswith('untrusted model text\n\n') and row.body.endswith('USD 10.00.')
        assert 'scenic video' in row.body
    intent = engine.queue(TENANT, ACCOUNT, decision)
    provider = SyntheticFansProvider(message_id=9100)
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire_for(store, provider)) == 'accepted'
    body = provider.calls[0]['body']
    assert body['text'].startswith('untrusted model text') and 'USD 10.00' in body['text']
    assert str(body['price']) == '10' and body['mediaFiles'] == ['11', '12', '13']
    store.close()


def test_r6_model_text_cannot_restate_the_price(database):
    store, authority, engine, decision, item, grant = setup_offer(database)
    engine.model = SyntheticModel({'second': SyntheticProposal('It costs USD 1.00.', action='offer', offer_grant_id=grant)})
    incoming(store, 2, 'second')
    with pytest.raises(Refused, match='price'):
        engine.plan(TENANT, ACCOUNT, FAN, 2)
    store.close()


# ---- R8 -------------------------------------------------------------------
def test_r8_scope_changes_from_any_sales_state(database):
    store, authority, engine, model, memory = setup(database)
    engine.model = SyntheticModel({'decline': SyntheticProposal('Understood.', sales_signal='decline'),
        'buy one': SyntheticProposal('Item one.', sales_signal='reopen', item_scope=('1',)),
        'buy two': SyntheticProposal('Item two.', sales_signal='reopen', item_scope=('2',))})
    provider = SyntheticFansProvider(('accepted',) * 3, message_id=(9001, 9002, 9003))
    wire = wire_for(store, provider)
    seen = []
    for mid, body in [(1, 'decline'), (2, 'buy one'), (3, 'buy two')]:
        if mid > 1:
            unthrottle(database[1])
        incoming(store, mid, body)
        assert run(store, authority, engine, mid, wire=wire)[2] == 'accepted'
        with Session(database[1]) as s:
            owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
            seen.append((owner.sales_mode, owner.sales_scope_json))
    assert seen == [('paused', '[]'), ('reopened_scoped', '["1"]'), ('reopened_scoped', '["2"]')]
    store.close()


def test_r8_reopen_while_sales_open_does_not_raise_or_narrow(database):
    store, authority, engine, model, memory = setup(database)
    engine.model = SyntheticModel({'buy one': SyntheticProposal('Item one.', sales_signal='reopen', item_scope=('1',))})
    incoming(store, 1, 'buy one')
    provider = SyntheticFansProvider()
    assert run(store, authority, engine, 1, provider)[2] == 'accepted'
    with Session(database[1]) as s:
        owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
        assert (owner.sales_mode, owner.sales_scope_json) == ('open', '[]')
    store.close()


# ---- R10 + history fields --------------------------------------------------
def test_r10_unsent_messages_are_not_history_and_history_is_richer(database):
    store, authority, engine, model, memory = setup(database)
    engine.model.replies['next'] = SyntheticProposal('Understood.')
    incoming(store, 1, 'hello')
    incoming(store, 2, 'next')
    with Session(database[1]) as s, s.begin():
        s.get(Message, (ACCOUNT, FAN, 1)).is_unsent = True
        s.get(Message, (ACCOUNT, FAN, 2)).price_cents = 0
    provider = SyntheticFansProvider()
    assert run(store, authority, engine, 2, provider)[2] == 'accepted'
    history = model.calls[-1]['history']
    assert [row['id'] for row in history] == [2]
    row = history[0]
    assert set(row) == {'id', 'direction', 'body', 'timestamp', 'price_cents', 'media_count', 'paid'}
    assert row['timestamp'] and row['price_cents'] == 0 and row['media_count'] == 0 and row['paid'] is None
    store.close()


# ---- newer input closes the request ---------------------------------------
def test_dropped_request_for_newer_input_is_closed(database):
    store, authority, engine, model, memory = setup(database)
    incoming(store, 1, 'hello')
    incoming(store, 2, 'next')
    with pytest.raises(Refused, match='newer incoming'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    with Session(database[1]) as s:
        request = s.scalar(select(OfEngineRequest).where(OfEngineRequest.source_message_id == 1))
        assert (request.status, request.next_step) == ('cancelled', 'superseded_by_newer_input')
    assert run(store, authority, engine, 2, SyntheticFansProvider())[2] == 'accepted'
    store.close()


# ---- handback --------------------------------------------------------------
def test_handback_revives_engine_ownership(database):
    store, authority, engine, model, memory = setup(database)
    store.control(TENANT, ACCOUNT, FAN, mode='human_controlled', expected_revision=0,
        command_key='hb-take', actor='op-session:' + 'a' * 64, reason='Take over', action='request_takeover')
    with Session(database[1]) as s:
        assert s.get(OfEngineOwner, (ACCOUNT, FAN)).mode == 'human'
    incoming(store, 1, 'hello')
    with pytest.raises(Refused, match='unavailable'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)
    store.control(TENANT, ACCOUNT, FAN, mode='automated', expected_revision=1,
        command_key='hb-release', actor='op-session:' + 'a' * 64, reason='Hand back', action='release_takeover')
    with Session(database[1]) as s:
        owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
        assert owner.mode == 'active' and owner.epoch == 3
    assert engine.select_simulated(TENANT, ACCOUNT, FAN) == 3
    assert run(store, authority, engine, 1, SyntheticFansProvider())[2] == 'accepted'
    store.close()


# ---- R12 -------------------------------------------------------------------
def webhook(adapter, mid, text, key, *, secret=b'fixture-secret', provider='acct_memory_fixture'):
    payload = {'event': 'messages.received', 'account_id': provider,
        'payload': {'id': mid, 'fromUser': {'id': FAN}, 'text': text,
                    'createdAt': datetime.now(timezone.utc).isoformat(), 'media': [], 'price': 0}}
    raw = json.dumps(payload, separators=(',', ':')).encode()
    return adapter.accept(raw, hmac.new(secret, raw, hashlib.sha256).hexdigest(), secret, key,
                          expected_tenant=TENANT, expected_account=ACCOUNT)


def test_r12_conflict_after_queue_cancels_before_wire_and_blocks_regeneration(database):
    store, authority, engine, model, memory = setup(database)
    adapter = FansWebhookAdapter(store)
    assert webhook(adapter, 1, 'hello', 'evt-1') == 'accepted'
    store.drain(TENANT, ACCOUNT)
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    assert webhook(adapter, 1, 'different text', 'evt-1') == 'quarantined'
    provider = SyntheticFansProvider()
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire_for(store, provider)) == 'cancelled'
    assert provider.calls == []
    with Session(database[1]) as s:
        assert s.get(OfEngineDecision, (ACCOUNT, decision)).status == 'cancelled'
        request = s.scalar(select(OfEngineRequest))
        assert (request.status, request.next_step) == ('open', 'review_webhook_conflict')
    with pytest.raises(Refused, match='conflicting webhook'):
        engine.plan(TENANT, ACCOUNT, FAN, 1)  # no silent resolution, no second send
    store.close()


def test_r12_fence_cancels_ready_and_queued_but_not_other_fans_work(database):
    store, authority, engine, model, memory = setup(database)
    adapter = FansWebhookAdapter(store)
    assert webhook(adapter, 1, 'hello', 'evt-1') == 'accepted'
    store.drain(TENANT, ACCOUNT)
    ready = engine.plan(TENANT, ACCOUNT, FAN, 1)  # not queued yet
    assert webhook(adapter, 1, 'other bytes', 'evt-1') == 'quarantined'
    assert len(engine.fence_conflicted_sources(TENANT, ACCOUNT)) == 1
    with Session(database[1]) as s:
        assert s.get(OfEngineDecision, (ACCOUNT, ready)).status == 'cancelled'
    with pytest.raises(Refused):
        engine.queue(TENANT, ACCOUNT, ready)
    # queued, undispatched work is cancelled with its queue accounting
    assert webhook(adapter, 2, 'next', 'evt-2') == 'accepted'
    store.drain(TENANT, ACCOUNT)
    second = engine.plan(TENANT, ACCOUNT, FAN, 2)
    intent = engine.queue(TENANT, ACCOUNT, second)
    assert engine.fence_conflicted_sources(TENANT, ACCOUNT) == []  # event 2 is not in conflict
    assert webhook(adapter, 2, 'next but changed', 'evt-2') == 'quarantined'
    assert len(engine.fence_conflicted_sources(TENANT, ACCOUNT)) == 1
    with Session(database[1]) as s:
        assert s.get(OfTextIntent, (ACCOUNT, intent)).status == 'cancelled'
        assert s.get(OfEngineDecision, (ACCOUNT, second)).status == 'cancelled'
    provider = SyntheticFansProvider()
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire_for(store, provider)) != 'accepted'
    assert provider.calls == []
    store.close()


# ---- R13 -------------------------------------------------------------------
def test_r13_unresolved_first_paginated_request_read(database, monkeypatch):
    from onlyfans_read import repo
    store, authority, engine, model, memory = setup(database)
    now = utcnow()
    with Session(database[1]) as s, s.begin():
        owner = s.get(OfEngineOwner, (ACCOUNT, FAN))
        for index in range(25):  # one old unresolved request, then 24 newer finished ones
            s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=100 + index, direction='in', body='x', created_at=now))
            s.flush()
            s.add(OfEngineRequest(account_id=ACCOUNT, request_id=f'{index:08d}-0000-0000-0000-000000000000', fan_id=FAN,
                binding_id=owner.binding_id, source_message_id=100 + index,
                status='waiting_provider' if index == 0 else 'completed', owner='engine',
                next_step='reconcile_existing_action' if index == 0 else 'none', revision=0,
                created_at=now + timedelta(seconds=index), updated_at=now))
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
    async def scope(session, account): pass
    monkeypatch.setattr(repo, 'get_session', sessions)
    monkeypatch.setattr(repo, 'lock_read_scope', scope)
    read = repo.SqlOnlyFansReadRepo()
    first = asyncio.run(read.engine_status(account_id=ACCOUNT, fan_id=FAN))
    assert len(first['requests']) == 20 and first['truncated'] is True and first['nextCursor']
    assert first['requests'][0]['status'] == 'waiting_provider'  # old unresolved work is not hidden
    seen = [row['requestId'] for row in first['requests']]
    page = first
    while page['nextCursor']:
        page = asyncio.run(read.engine_status(account_id=ACCOUNT, fan_id=FAN, cursor=page['nextCursor']))
        seen += [row['requestId'] for row in page['requests']]
    assert len(seen) == 25 == len(set(seen))
    with pytest.raises(ValueError):
        asyncio.run(read.engine_status(account_id=ACCOUNT, fan_id=FAN, cursor='garbage'))
    store.close()
