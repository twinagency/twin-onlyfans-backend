"""FASTT runner on real PostgreSQL: real ai_chatter.run, stub model, synthetic wire; no provider or model I/O.

The runner owns ONE fan. FASTT decides and paces; every bubble is a durable TextAuthority intent
(origin 'fastt'); a stop, takeover, newer input or unknown send ends the turn without a second send.
"""
import asyncio
import json
from datetime import timedelta
import threading

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, due, event, seed
from test_onlyfans_text_delivery_pg import current
from db.models import (AccountAiConfig, CatalogItem, ContentOffer, Fan, LadderQuote, Message,
                       OfConversationState, OfMessageDelivery, VaultItem, VaultSend)
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_models import OfControlAudit, OfWork
from db.onlyfans_fastt_models import OfRuntimeOwner
from llm_client import LLMResult
import llm_client
from onlyfans_runtime import fastt_owner
from onlyfans_runtime.fastt_capabilities import Scope
from onlyfans_runtime.fastt_errors import FasttHalt, FasttTurnAbort
from onlyfans_runtime.fastt_gate import FasttSendGate, reconcile_stop
from onlyfans_runtime.fastt_loop import FasttRunner
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport, TextAuthority

ACCOUNT, FAN, TENANT = "of-176774528", 318022380, "twin-internal"
PHOTO, VIDEO = 3271069131, 3308786386     # catalogue item #1: free preview photo + paid video, fixed US$3


class StubModel:
    """Registered as the process model gateway; replaces the network. Replies come from a script."""

    def __init__(self, replies, on_call=None):
        self.replies, self.calls, self.on_call = list(replies), [], on_call

    async def chat(self, **kw):
        self.calls.append(kw)
        if self.on_call:
            self.on_call(len(self.calls))
        text_ = self.replies.pop(0) if self.replies else "ok"
        return LLMResult(call_id=None, model=kw["model"], provider="stub", content=text_, parsed=None,
                         reasoning_content=None, tokens_in=10, tokens_out=5, cache_hit_tokens=0,
                         cost_cents=1, latency_ms=1)


class World:
    """One seeded conversation plus the objects the runner needs."""

    def __init__(self, database, monkeypatch, *, config=None, items=True, quiet=0.0):
        self.url, self.engine = database
        monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
        monkeypatch.setenv("DATABASE_URL", self.url)
        self.monkeypatch = monkeypatch
        self.store = seed(self.url, self.engine, account=ACCOUNT, tenant=TENANT, fan=FAN)
        current(self.engine, mode="automated", account=ACCOUNT, fan=FAN)
        self.authority = TextAuthority(self.store)
        self.authority.configure(TENANT, ACCOUNT, expected_revision=0, enabled=True, media_enabled=True,
                                 limit_units=100, throttle_seconds=1, actor="synthetic-operator", reason="runner test")
        cfg = {"prompt_sell_catalog": True, **(config or {})}
        with Session(self.engine) as s, s.begin():
            s.add(AccountAiConfig(account_id=ACCOUNT, model="grok-4-1-fast-non-reasoning", daily_cost_cap_cents=100,
                                  persona="Synthetic Creator enjoys hiking and coffee.", ai_chatter_config_json=json.dumps(cfg)))
            if items:
                s.add(VaultItem(account_id=ACCOUNT, media_id=PHOTO, kind="image", created_at=utcnow(), content_hash="photo"))
                s.add(VaultItem(account_id=ACCOUNT, media_id=VIDEO, kind="video", created_at=utcnow(), content_hash="video"))
                s.flush()
                item = CatalogItem(account_id=ACCOUNT, kind="video", label="Item one", enabled=True, pricing_mode="fixed",
                                   description_for_ai="A short private video with a free preview photo.",
                                   media_ids=json.dumps([PHOTO, VIDEO]), preview_media_ids=json.dumps([PHOTO]),
                                   price_cents=300)
                s.add(item)
                s.flush()
                self.item = item.id
        fastt_owner.claim(self.store, TENANT, ACCOUNT, FAN, actor="synthetic-operator")
        self.wire = SyntheticTextTransport(ACCOUNT)
        counter = iter(range(9001, 9100))
        self.wire.on_send = lambda dispatch: setattr(self.wire, "message_id", next(counter))
        self.stop = threading.Event()
        self.gate = FasttSendGate(self.store, self.authority, self.wire, tenant_id=TENANT, account_id=ACCOUNT,
                                  fan_id=FAN, stop_event=self.stop, throttle_wait_seconds=30)
        self.scope = Scope(TENANT, ACCOUNT, FAN)
        self.quiet = quiet
        self.inbound = 0
        # ai_chatter's pacing wait (typing / rhythm / bubble gaps) is replaced by hooks, so a test can
        # change the world exactly while FASTT is "waiting" before bubble N.
        from automations import ai_chatter
        self.holds, self.hold_hooks = 0, []

        async def hold(account_id, fan_id, total, **kw):
            self.holds += 1
            for hook in list(self.hold_hooks):
                hook(self.holds)
        monkeypatch.setattr(ai_chatter, "hold_with_typing", hold)

    def runner(self, **kw):
        return FasttRunner(self.store, self.authority, self.gate, self.scope, quiet_seconds=self.quiet, poll_seconds=.05,
                           allow_synthetic=True, **kw)

    def fan_says(self, body, age=30):
        self.inbound += 1
        self.store.record(TENANT, ACCOUNT, event(mid=100 + self.inbound, fan=FAN, body=body))
        self.store.drain(TENANT, ACCOUNT)
        with self.engine.begin() as c:   # the quiet window has long elapsed
            c.execute(text("UPDATE of_work SET last_input_at = CURRENT_TIMESTAMP - make_interval(secs => :age)"), {"age": age})

    def rows(self, model, *where):
        with Session(self.engine) as s:
            return s.scalars(select(model).where(*where)).all()

    def out_messages(self):
        return [m.body for m in self.rows(Message, Message.direction == "out")]

    def state(self):
        with Session(self.engine) as s:
            return s.get(OfConversationState, (ACCOUNT, FAN))

    def close(self):
        self.store.close()


def drive(world, coro_factory, *, model=None):
    """Run async work against this test database on one event loop (db.engine is process-global)."""
    async def main():
        engine = create_async_engine(world.url.replace("postgresql+psycopg2", "postgresql+asyncpg").replace("postgresql://", "postgresql+asyncpg://"))
        import importlib
        db_engine = importlib.import_module("db.engine")
        world.monkeypatch.setattr(db_engine, "async_session_factory", async_sessionmaker(engine, expire_on_commit=False))
        try:
            return await coro_factory()
        finally:
            await engine.dispose()
    if model is not None:
        llm_client.register_scoped_gateway(model)
    try:
        return asyncio.run(main())
    finally:
        llm_client.clear_scoped_gateway()


@pytest.fixture
def world(database, monkeypatch):
    holder = []
    def make(**kw):
        w = World(database, monkeypatch, **kw)
        holder.append(w)
        return w
    yield make
    for w in holder:
        w.close()


def test_inbound_to_ai_chatter_reply_to_accepted_receipt(world):
    w = world()
    w.fan_says("hey, how is your day going?")
    model = StubModel(["hey you! my day is going really well, just got back from a hike"])
    runner = w.runner()
    action = drive(w, runner.tick, model=model)
    assert len(model.calls) == 1
    assert action == "replied", (action, runner.results)
    bubbles = w.out_messages()
    assert " ".join(bubbles) == "hey you! my day is going really well, just got back from a hike"
    assert len(w.wire.calls) == len(bubbles) >= 1 and {c.origin for c in w.wire.calls} == {"fastt"}
    intents = w.rows(OfTextIntent)
    assert {i.origin for i in intents} == {"fastt"} and {i.status for i in intents} == {"accepted"}
    assert all(i.projected_at is not None for i in intents)
    receipts = w.rows(OfMessageDelivery)
    assert {(r.outcome, r.origin) for r in receipts} == {("sent", "ai")} and len(receipts) == len(bubbles)
    for msg in w.rows(Message, Message.direction == "out"):
        assert msg.automation_kind == "ai_chatter" and msg.sent_by_employee_id is None
    with Session(w.engine) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        assert policy.used_units == len(bubbles) and policy.active_intent_id is None


def test_free_preview_plus_paid_card_then_durable_stop(world):
    w = world(config={"min_fan_msgs_between_offers": 0, "max_offers_per_fan_per_day": 5})
    w.fan_says("do you have any videos you could send me?")
    model = StubModel([f"yes babe i have one for you\n>>OFFER {w.item}"])
    runner = w.runner()
    action = drive(w, runner.tick, model=model)
    assert action == "stopped_after_paid_card", (action, runner.results, w.out_messages())
    card = [m for m in w.rows(Message, Message.direction == "out") if m.price_cents]
    assert len(card) == 1 and card[0].price_cents == 300
    assert json.loads(card[0].media_ids) == [PHOTO, VIDEO] and card[0].automation_kind == "ai_upseller"
    [call] = [c for c in w.wire.calls if c.price_cents]
    assert (call.media_ids, call.preview_ids, call.price_cents) == ((PHOTO, VIDEO), (PHOTO,), 300)
    # Bookkeeping has exactly one writer: materialize/project, never ai_chatter's own.
    [offer] = w.rows(ContentOffer)
    assert (offer.item_id, offer.mode, offer.status, offer.price_cents, offer.offer_message_id) == (w.item, "ppv", "open", 300, card[0].message_id)
    assert sorted(v.media_id for v in w.rows(VaultSend)) == [PHOTO, VIDEO]
    [quote] = w.rows(LadderQuote)
    assert (quote.kind, quote.price_cents, quote.message_id) == ("fixed", 300, card[0].message_id)
    assert {r.outcome for r in w.rows(OfMessageDelivery)} == {"sent"}
    # Durable stop through the control path, attributed to the runner.
    state = w.state()
    assert state.mode == "paused" and state.unresolved_attempt_id is None
    [audit] = w.rows(OfControlAudit, OfControlAudit.action == "pause_automation")
    assert audit.actor == "fastt-runner:stop-after-paid-card" and audit.command_key.startswith("fastt-stop:")
    # No further automated action: a fresh inbound does not wake the model or the wire.
    sent, calls = len(w.wire.calls), len(model.calls)
    w.fan_says("how much is it?")
    assert drive(w, w.runner().tick, model=model) == "idle_not_automated"
    assert (len(w.wire.calls), len(model.calls)) == (sent, calls)
