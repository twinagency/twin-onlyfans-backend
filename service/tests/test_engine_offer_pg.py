"""Synthetic commercial journey using existing catalogue, intent and ledger owners."""
from dataclasses import replace
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from datetime import timedelta, timezone
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, event
from test_onlyfans_media_pg import shelf, media_policy
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured
from conversation_engine.commerce import SyntheticAccessObservation, SyntheticCoverage, approve_simulated_offer, record_simulated_access
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from db.models import CatalogItem, ContentOffer, Message, Transaction, VaultSend
from db.onlyfans_engine_models import OfEngineDecision, OfEngineRequest
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
from onlyfans_read.projections import message_model
from onlyfans_read.repo import MessageRow
from onlyfans_runtime.commerce import CommerceIntake, SyntheticCommerceEvent
from onlyfans_runtime.store import Refused, utcnow


def setup_offer(database):
    store, authority = configured(database, mode='automated')
    item_id = shelf(database[1]); media_policy(authority)
    adapter = FansWebhookAdapter(store)
    adapter.bind_simulated(TENANT, ACCOUNT, 'acct_fixture_offer', 'creator-fixture')
    engine = EngineService(store, SyntheticModel({}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    coverage = SyntheticCoverage(ACCOUNT, FAN, item_id, (11, 12, 13), 'synthetic:complete-ownership',
                                 utcnow() + timedelta(minutes=5))
    grant = approve_simulated_offer(store, TENANT, ACCOUNT, FAN, item_id, coverage)
    model = SyntheticModel({'show me the set': SyntheticProposal('untrusted model text', action='offer', offer_grant_id=grant)})
    engine.model = model
    store.record(TENANT, ACCOUNT, event(mid=1, fan=FAN, body='show me the set'))
    store.drain(TENANT, ACCOUNT)
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    return store, authority, engine, decision, item_id, grant


def test_locked_offer_receipt_is_not_purchase_then_exact_purchase_is_recorded(database, monkeypatch):
    store, authority, engine, decision, item_id, grant = setup_offer(database)
    # Read through the same scoped SQL projection used by the dashboard API.
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
        with Session(database[1]) as session:
            yield Adapter(session)
    async def scope(session, account):
        assert account == ACCOUNT
    monkeypatch.setattr(repo, 'get_session', sessions)
    monkeypatch.setattr(repo, 'lock_read_scope', scope)
    def engine_view():
        return asyncio.run(repo.SqlOnlyFansReadRepo().engine_status(account_id=ACCOUNT, fan_id=FAN))
    views = {}
    intent = engine.queue(TENANT, ACCOUNT, decision)
    provider = SyntheticFansProvider(message_id=9100)
    wire = FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'accepted'
    assert len(provider.calls) == 1
    body = provider.calls[0]['body']
    assert body['mediaFiles'] == ['11', '12', '13'] and body['previews'] == ['11']
    assert str(body['price']) == '10'
    with Session(database[1]) as s:
        msg = s.get(Message, (ACCOUNT, FAN, 9100))
        offer = s.scalar(select(ContentOffer).where(ContentOffer.account_id == ACCOUNT, ContentOffer.fan_id == FAN))
        assert msg.is_paid is None and msg.purchased_at is None and msg.price_cents == 1000
        request_id = s.get(OfEngineDecision, (ACCOUNT, decision)).request_id
        request = s.get(OfEngineRequest, (ACCOUNT, request_id))
        assert request.status == 'waiting_fan' and request.next_step == 'await_purchase_or_question'
        assert offer.status == 'open' and offer.price_cents == 1000
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
        assert all(not x.was_purchased for x in s.scalars(select(VaultSend)))
    views['offer'] = engine_view()
    assert views['offer']['requests'][0]['actionType'] == 'offer'
    assert views['offer']['requests'][0]['nextStep'] == 'await_purchase_or_question'
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
    purchase = SyntheticCommerceEvent(account_id=ACCOUNT, fan_id=FAN, message_id=9100,
        event_key='fixture-purchase-1', provider_transaction_id='fixture-tx-1', provider_revision=1,
        kind='purchase', amount_cents=1000, currency='USD',
        occurred_at=(utcnow() + timedelta(seconds=1)).replace(tzinfo=timezone.utc).isoformat(),
        evidence_ref='synthetic:purchase-proof')
    intake = CommerceIntake(store)
    assert intake.record(TENANT, ACCOUNT, purchase) == 'applied'
    assert intake.record(TENANT, ACCOUNT, purchase) == 'applied'
    with Session(database[1]) as s:
        msg = s.get(Message, (ACCOUNT, FAN, 9100))
        assert msg.is_paid is True and msg.purchased_at is not None
        request = s.get(OfEngineRequest, (ACCOUNT, request_id))
        assert request.status == 'waiting_provider' and request.next_step == 'verify_access'
        assert s.scalar(select(func.count()).select_from(Transaction)) == 1
        assert all(x.was_purchased for x in s.scalars(select(VaultSend)))
    views['purchase'] = engine_view()
    assert views['purchase']['requests'][0]['nextStep'] == 'verify_access'
    access = SyntheticAccessObservation(ACCOUNT, FAN, intent, 9100, (11, 12, 13), 'access-1',
        'synthetic:access-proof', 'confirmed', (utcnow() + timedelta(seconds=2)).replace(tzinfo=timezone.utc))
    with pytest.raises(Refused):
        record_simulated_access(store, TENANT, ACCOUNT, replace(access, media_ids=(11, 12)))
    assert record_simulated_access(store, TENANT, ACCOUNT, access) == 'confirmed'
    assert record_simulated_access(store, TENANT, ACCOUNT, access) == 'confirmed'
    with Session(database[1]) as s:
        assert s.get(OfEngineRequest, (ACCOUNT, request_id)).status == 'completed'
    views['access'] = engine_view()
    assert views['access']['requests'][0]['status'] == 'completed'
    revoked = replace(access, evidence_key='access-2', status='revoked', occurred_at=access.occurred_at + timedelta(seconds=1))
    assert record_simulated_access(store, TENANT, ACCOUNT, revoked) == 'revoked'
    older = replace(access, evidence_key='access-older')
    assert record_simulated_access(store, TENANT, ACCOUNT, older) == 'stale'
    with Session(database[1]) as s:
        request = s.get(OfEngineRequest, (ACCOUNT, request_id))
        assert request.status == 'open' and request.next_step == 'review_access_revocation'
    views['revoked'] = engine_view()
    assert views['revoked']['requests'][0]['nextStep'] == 'review_access_revocation'
    artifact = os.environ.get('ENGINE_OFFER_ARTIFACT')
    if artifact:
        Path(artifact).write_text(json.dumps(views), encoding='utf-8')
    store.close()


def test_changed_terms_cancel_before_wire_and_do_not_silently_reprice(database):
    store, authority, engine, decision, item_id, grant = setup_offer(database)
    intent = engine.queue(TENANT, ACCOUNT, decision)
    with Session(database[1]) as s, s.begin():
        s.get(CatalogItem, item_id).price_cents = 1200
    provider = SyntheticFansProvider(message_id=9100)
    wire = FansApiSyntheticTransport(store, TENANT, ACCOUNT, provider)
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'cancelled'
    assert provider.calls == []
    with Session(database[1]) as s:
        assert s.get(OfEngineDecision, (ACCOUNT, decision)).status == 'cancelled'
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
    store.close()


def test_bundle_overlap_after_selection_blocks_the_offer(database):
    store, authority, engine, decision, item_id, grant = setup_offer(database)
    with Session(database[1]) as s, s.begin():
        s.add(VaultSend(account_id=ACCOUNT, fan_id=FAN, media_id=11, was_purchased=True))
    with pytest.raises(Refused):
        engine.queue(TENANT, ACCOUNT, decision)
    store.close()
