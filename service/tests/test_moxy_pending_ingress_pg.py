"""Real PostgreSQL delivery transitions; all model and wire work is synthetic."""
from datetime import timedelta

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_moxy_multicreator_load_pg import (
    POOL, CREATORS, Ctx, ROUND1, add_event, build_creator, make_stub,
)
from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from moxy_replacement.configuration import load
from moxy_replacement.runtime import MoxyService
from onlyfans_runtime.store import Store, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport, TextAuthority


@pytest.mark.parametrize('mode', ['gap', 'concurrent'])
@pytest.mark.parametrize('boundary', ['admit', 'prewire'])
@pytest.mark.parametrize('next_action', ['resume', 'new_input', 'takeover', 'expiry'])
def test_pending_other_fan_defers_then_revalidates(database, monkeypatch, mode, boundary, next_action):
    url, engine = database
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'worker')
    monkeypatch.setenv('DATABASE_URL', url)
    monkeypatch.setenv('FASTT_CONVERSATION_MIN_GAP_S', '0')
    from twin_bridge.store import ensure_runtime_schema
    ensure_runtime_schema(url)
    store = Store(url, pool_size=POOL)
    try:
        service = MoxyService(store)
        ctx = Ctx(engine, store, service, service.authority)
        c = CREATORS[0]
        build_creator(ctx, url, engine, c, {'amelia': load('amelia').creator})
        with Session(engine) as s, s.begin():
            p = s.get(OfDeliveryPolicy, c.account)
            p.delivery_mode = mode
            p.conversation_min_gap_seconds = 0
        a, b = c.fans[:2]
        ctx.inbound(c, a, ROUND1['chat'])
        store.drain(c.tenant, c.account)
        ev = add_event(ctx, c, a, 'pending-ingress-regression')
        service.prepare(c.tenant, c.account, ev, make_stub(ctx, c))
        intent = service.queue(c.tenant, c.account, ev)
        wire = SyntheticTextTransport(c.account, message_id=ctx.pid(c.account, intent))

        if boundary == 'admit':
            ctx.inbound(c, b, ROUND1['chat'])
        else:
            consume = TextAuthority.consume_dispatch
            injected = False
            def with_pending(self, tenant, grant, **kwargs):
                nonlocal injected
                if not injected:
                    injected = True
                    ctx.inbound(c, b, ROUND1['chat'])
                return consume(self, tenant, grant, **kwargs)
            monkeypatch.setattr(TextAuthority, 'consume_dispatch', with_pending)

        assert service.authority.dispatch(c.tenant, c.account, intent, wire) == 'queued'
        assert not wire.calls
        with Session(engine) as s:
            row = s.get(OfTextIntent, (c.account, intent))
            policy = s.get(OfDeliveryPolicy, c.account)
            state = s.get(OfConversationState, (c.account, a))
            assert row.reason == 'history_not_current' and row.wire_started_at is None
            assert not row.reservation_held and state.unresolved_attempt_id is None
            assert policy.queued_count == 1 and policy.used_units == 0
            assert policy.active_intent_id is None

        if next_action == 'new_input':
            ctx.inbound(c, a, 'Actually, I have another question')
        store.drain(c.tenant, c.account)
        if next_action == 'takeover':
            ctx.set_mode(c.account, a, 'human_controlled')
        with Session(engine) as s, s.begin():
            s.get(OfDeliveryPolicy, c.account).next_dispatch_at = utcnow() - timedelta(seconds=1)
        if next_action == 'expiry':
            monkeypatch.setattr(service.authority, '_db_now', lambda s: utcnow() + timedelta(minutes=11))

        result = service.authority.dispatch(c.tenant, c.account, intent, wire)
        # Takeover changes the Moxy snapshot and cancels the old decision too.
        assert result == ('accepted' if next_action == 'resume' else 'cancelled')
        assert len(wire.calls) == (1 if next_action == 'resume' else 0)
        with Session(engine) as s:
            policy = s.get(OfDeliveryPolicy, c.account)
            assert policy.queued_count == 0
            assert policy.used_units == (1 if next_action == 'resume' else 0)
            assert s.get(OfConversationState, (c.account, a)).unresolved_attempt_id is None
            outgoing = s.scalar(select(func.count()).select_from(Message).where(
                Message.account_id == c.account, Message.fan_id == a, Message.direction == 'out'))
            assert outgoing == (1 if next_action == 'resume' else 0)
        if next_action == 'resume':
            assert service.authority.dispatch(c.tenant, c.account, intent, wire) == 'accepted'
            assert len(wire.calls) == 1
    finally:
        store.close()
