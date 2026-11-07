"""Ordered parts use one request and separate, revision-checked intents."""
import pytest
import asyncio
import os
import json
from pathlib import Path
from contextlib import asynccontextmanager
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured, unthrottle
from test_engine_simulated_journey_pg import incoming
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from db.models import Message
from db.onlyfans_engine_models import OfEngineDecision, OfEngineRequest
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
from onlyfans_runtime.store import Refused


def setup(database):
    store, authority = configured(database, mode='automated', limit=8)
    FansWebhookAdapter(store).bind_simulated(TENANT, ACCOUNT, 'acct_parts', 'creator-fixture')
    model = SyntheticModel({'hello':SyntheticProposal('First part.', parts=('Second part.', 'Third part.')),
                            'correction':SyntheticProposal('Updated answer.')})
    engine = EngineService(store, model)
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    incoming(store, 1, 'hello')
    first = engine.plan(TENANT, ACCOUNT, FAN, 1)
    return store, authority, engine, engine.sequence(TENANT, ACCOUNT, first)


def test_sequence_preserves_order_and_distinct_intent_identity(database, monkeypatch):
    store, authority, engine, parts = setup(database)
    from onlyfans_read import repo
    class Adapter:
        def __init__(self, session): self.session = session
        async def execute(self, query): return self.session.execute(query)
        async def get(self, model, key): return self.session.get(model, key)
        async def scalar(self, query): return self.session.scalar(query)
        async def scalars(self, query): return self.session.scalars(query)
        async def run_sync(self, fn): return fn(self.session)
    @asynccontextmanager
    async def sessions():
        with Session(database[1]) as session: yield Adapter(session)
    async def scope(session, account): assert account == ACCOUNT
    monkeypatch.setattr(repo, 'get_session', sessions)
    monkeypatch.setattr(repo, 'lock_read_scope', scope)
    def view(): return asyncio.run(repo.SqlOnlyFansReadRepo().engine_status(account_id=ACCOUNT, fan_id=FAN))
    assert len(parts) == 3
    with pytest.raises(Refused, match='predecessor incomplete'):
        engine.queue(TENANT, ACCOUNT, parts[1])
    provider = SyntheticFansProvider(('accepted','accepted','accepted'), message_id=(9801,9802,9803))
    wire = FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)
    intents = []
    for index, part in enumerate(parts):
        intent = engine.queue(TENANT, ACCOUNT, part); intents.append(intent)
        unthrottle(database[1])
        assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'accepted'
        with Session(database[1]) as s:
            decision = s.get(OfEngineDecision, (ACCOUNT, part))
            request = s.get(OfEngineRequest, (ACCOUNT, decision.request_id))
            assert request.status == ('completed' if index == 2 else 'open')
            assert request.next_step == ('none' if index == 2 else 'send_next_part')
        projected = view()['requests']
        assert len(projected) == 1 and projected[0]['partsTotal'] == 3
        assert projected[0]['partsCompleted'] == index + 1
        if index == 0 and os.environ.get('ENGINE_SEQUENCE_ARTIFACT'):
            Path(os.environ['ENGINE_SEQUENCE_ARTIFACT']).write_text(json.dumps(view()), encoding='utf-8')
    assert len(set(intents)) == 3 and len(provider.calls) == 3
    with Session(database[1]) as s:
        assert [s.get(Message, (ACCOUNT, FAN, mid)).body for mid in (9801,9802,9803)] == ['First part.','Second part.','Third part.']
    store.close()


def test_correction_between_parts_cancels_remaining_unsent_actions(database):
    store, authority, engine, parts = setup(database)
    provider = SyntheticFansProvider(message_id=9801)
    wire = FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)
    first = engine.queue(TENANT, ACCOUNT, parts[0])
    assert authority.dispatch(TENANT, ACCOUNT, first, wire) == 'accepted'
    incoming(store, 2, 'correction')
    with pytest.raises(Refused, match='conversation changed'):
        engine.queue(TENANT, ACCOUNT, parts[1])
    with Session(database[1]) as s:
        assert [s.get(OfEngineDecision, (ACCOUNT, part)).status for part in parts] == ['completed','cancelled','cancelled']
        request = s.get(OfEngineRequest, (ACCOUNT, s.get(OfEngineDecision, (ACCOUNT, parts[0])).request_id))
        assert request.status == 'open' and request.next_step == 'review_changed_conversation'
    assert len(provider.calls) == 1
    store.close()


def test_unknown_first_part_does_not_send_later_parts(database):
    store, authority, engine, parts = setup(database)
    provider = SyntheticFansProvider(('unknown',))
    wire = FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)
    first = engine.queue(TENANT, ACCOUNT, parts[0])
    assert authority.dispatch(TENANT, ACCOUNT, first, wire) == 'unknown'
    with pytest.raises(Refused):
        engine.queue(TENANT, ACCOUNT, parts[1])
    assert len(provider.calls) == 1
    store.close()


def test_proven_not_sent_later_part_retries_same_intent_without_restarting_sequence(database):
    store, authority, engine, parts = setup(database)
    provider = SyntheticFansProvider(('accepted', 'not_sent', 'accepted', 'accepted'), message_id=(9811,9812,9813))
    wire = FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)
    first = engine.queue(TENANT, ACCOUNT, parts[0])
    assert authority.dispatch(TENANT, ACCOUNT, first, wire) == 'accepted'
    second = engine.queue(TENANT, ACCOUNT, parts[1])
    unthrottle(database[1])
    assert authority.dispatch(TENANT, ACCOUNT, second, wire) == 'not_accepted'
    assert engine.retry_not_sent(TENANT, ACCOUNT, parts[1], max_attempts=2) == second
    unthrottle(database[1])
    assert authority.dispatch(TENANT, ACCOUNT, second, wire) == 'accepted'
    third = engine.queue(TENANT, ACCOUNT, parts[2])
    unthrottle(database[1])
    assert authority.dispatch(TENANT, ACCOUNT, third, wire) == 'accepted'
    assert [call['idempotency_key'] for call in provider.calls] == [first, second, second, third]
    with Session(database[1]) as s:
        assert [s.get(Message, (ACCOUNT, FAN, mid)).body for mid in (9811,9812,9813)] == ['First part.','Second part.','Third part.']
    store.close()
