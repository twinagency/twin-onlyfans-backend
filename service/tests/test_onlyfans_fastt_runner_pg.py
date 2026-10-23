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
        """Bubbles FASTT sent (a human's own out-row, if any, is not one of them)."""
        return [m.body for m in self.rows(Message, Message.direction == "out", Message.automation_kind.is_not(None))]

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


# ---- helpers for changing the world while FASTT is waiting -------------------------------------------

def pause(w, key="op-pause"):
    w.store.control(TENANT, ACCOUNT, FAN, mode="paused", expected_revision=w.state().revision, command_key=key,
                    actor="synthetic-operator", reason="Operator pause", action="pause_automation")


def takeover(w, key="op-takeover"):
    w.store.control(TENANT, ACCOUNT, FAN, mode="human_controlled", expected_revision=w.state().revision, command_key=key,
                    actor="synthetic-operator", reason="Operator takeover", action="request_takeover")


def human_reply(w):
    from onlyfans_runtime.store import Event
    w.store.record(TENANT, ACCOUNT, Event(FAN, 777, utcnow().isoformat() + "+00:00", "out", "hi its me, the real person"))
    w.store.drain(TENANT, ACCOUNT)


def newer_inbound(w):
    w.fan_says("wait, one more thing")


CHANGES = {"pause": pause, "takeover": takeover, "human_reply": human_reply, "inbound": newer_inbound}
REPLY = "hey you! my day is going really well, just got back from a hike"


@pytest.mark.parametrize("change", sorted(CHANGES))
@pytest.mark.parametrize("when", ["generation", "pacing_first", "pacing_later"])
def test_change_during_generation_or_pacing_sends_nothing_more(world, when, change):
    """Pause, takeover, a human reply or newer fan input: checked after every wait, before EACH bubble."""
    w = world()
    w.fan_says("hey, how is your day going?")
    model = StubModel([REPLY])
    fire = CHANGES[change]
    done = []

    def once(n):
        if not done:
            done.append(n)
            fire(w)
    if when == "generation":
        model.on_call = once
    else:
        target = 1 if when == "pacing_first" else 2
        w.hold_hooks.append(lambda n: once(n) if n == target else None)
    runner = w.runner()
    action = drive(w, runner.tick, model=model)
    assert done, "the change never fired"
    expected_sent = 1 if when == "pacing_later" else 0
    assert len(w.wire.calls) == expected_sent, (action, w.out_messages())
    assert len(w.out_messages()) == expected_sent
    with Session(w.engine) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        assert policy.used_units == expected_sent and policy.active_intent_id is None and policy.queued_count == 0
    assert w.state().unresolved_attempt_id is None
    if when != "generation":
        assert action.startswith("turn_aborted"), action


def test_new_input_during_pacing_is_answered_once_on_the_next_turn(world):
    w = world()
    w.fan_says("hey, how is your day going?")
    model = StubModel([REPLY, "sure, ask me anything you like"])
    w.hold_hooks.append(lambda n: newer_inbound(w) if n == 1 else None)
    runner = w.runner()
    assert drive(w, runner.tick, model=model).startswith("turn_aborted")
    assert w.wire.calls == []
    # Same input is never re-run; the newer input is, after its own quiet window.
    w.hold_hooks.clear()
    assert drive(w, runner.tick, model=model) == "replied"
    assert len(model.calls) == 2 and "ask me anything you like" in " ".join(w.out_messages())
    assert drive(w, runner.tick, model=model) == "idle_nothing_to_answer"
    assert len(model.calls) == 2


def test_duplicate_inbound_notifications_produce_one_reply(world):
    w = world()
    w.fan_says("hey, how is your day going?")
    message = event(mid=101, fan=FAN, body="hey, how is your day going?")
    assert w.store.record(TENANT, ACCOUNT, message) in {"duplicate", "accepted"}   # redelivery, any shape
    w.store.drain(TENANT, ACCOUNT)
    model = StubModel([REPLY, "unexpected second reply"])
    runner = w.runner()
    assert drive(w, runner.tick, model=model) == "replied"
    sent = len(w.wire.calls)
    # A late duplicate of the same provider message changes nothing.
    w.store.record(TENANT, ACCOUNT, event(mid=101, fan=FAN, body="hey, how is your day going?"))
    w.store.drain(TENANT, ACCOUNT)
    assert drive(w, runner.tick, model=model) == "idle_nothing_to_answer"
    assert drive(w, w.runner().tick, model=model).startswith("idle")        # a restarted runner agrees
    assert len(model.calls) == 1 and len(w.wire.calls) == sent
    assert w.rows(Message, Message.direction == "in", Message.message_id == 101).__len__() == 1


@pytest.mark.parametrize("outcome", ["timeout", "accepted_lost_response", "malformed"])
def test_unknown_send_is_a_durable_hold_and_is_never_replayed(world, outcome):
    w = world()
    w.wire.outcome = outcome
    w.fan_says("hey, how is your day going?")
    model = StubModel([REPLY, "second attempt must never happen"])
    runner = w.runner()
    assert drive(w, runner.tick, model=model) == "halt:unknown_send"
    assert runner.halted == "unknown_send" and len(w.wire.calls) == 1
    [intent] = w.rows(OfTextIntent)
    assert intent.status == "unknown" and w.state().unresolved_attempt_id == intent.attempt_id
    # The remaining bubble of the same reply was not attempted, and a restarted runner replays nothing.
    again = w.runner()
    assert drive(w, again.tick, model=model) == "halt:unresolved_delivery"
    assert drive(w, lambda: again.run(threading.Event(), max_ticks=3), model=model) == "unresolved_delivery"
    assert len(w.wire.calls) == 1 and len(model.calls) == 1 and w.out_messages() == []
    assert any("held" in p or "unresolved" in p for p in again.preflight(live=True))


def test_crash_between_receipt_and_projection_still_gets_the_durable_stop(world):
    w = world(config={"min_fan_msgs_between_offers": 0, "max_offers_per_fan_per_day": 5})
    w.fan_says("do you have any videos you could send me?")
    model = StubModel([f"yes babe i have one for you\n>>OFFER {w.item}"])
    real = w.authority.materialize

    def crash_on_card(tenant, account, intent_id):
        with Session(w.engine) as s:
            if s.get(OfTextIntent, (account, intent_id)).catalog_item_id is not None:
                raise RuntimeError("process died after the accepted receipt")
        return real(tenant, account, intent_id)
    w.authority.materialize = crash_on_card
    runner = w.runner()
    assert drive(w, runner.tick, model=model).startswith("halt:dispatch_error")
    card = [i for i in w.rows(OfTextIntent) if i.catalog_item_id is not None]
    assert len(card) == 1 and card[0].status == "accepted" and card[0].projected_at is None
    assert w.state().mode == "automated" and w.state().unresolved_attempt_id == card[0].attempt_id
    sent = len(w.wire.calls)
    # "Restart": the real materialize returns; the new runner repairs the receipt, then applies the stop.
    del w.authority.materialize
    fresh = w.runner()
    assert drive(w, lambda: fresh.run(threading.Event(), max_ticks=1), model=model) == "stopped"
    [offer] = w.rows(ContentOffer)
    assert offer.offer_message_id == card[0].provider_message_id
    assert w.state().mode == "paused" and w.state().unresolved_attempt_id is None
    assert len(w.wire.calls) == sent, "nothing is replayed by the repair"
    [audit] = w.rows(OfControlAudit, OfControlAudit.action == "pause_automation")
    assert audit.command_key == "fastt-stop:" + card[0].intent_id
    # Running the repair again is idempotent, and an explicit resume is not undone by it.
    assert reconcile_stop(w.store, TENANT, ACCOUNT, FAN) is False


def test_explicit_resume_after_the_stop_works_and_the_stop_does_not_reapply(world):
    w = world(config={"min_fan_msgs_between_offers": 0, "max_offers_per_fan_per_day": 5})
    w.fan_says("do you have any videos you could send me?")
    model = StubModel([f"yes babe i have one for you\n>>OFFER {w.item}", "back with you now, what are you up to?"])
    runner = w.runner()
    assert drive(w, runner.tick, model=model) == "stopped_after_paid_card"
    w.store.control(TENANT, ACCOUNT, FAN, mode="automated", expected_revision=w.state().revision, command_key="op-resume",
                    actor="synthetic-operator", reason="Operator resume", action="resume_automation")
    assert reconcile_stop(w.store, TENANT, ACCOUNT, FAN) is False and w.state().mode == "automated"
    w.fan_says("thanks, what are you up to?")
    with w.engine.begin() as c:   # FASTT's own post-reply rest (fans.automation_paused_until) has elapsed
        c.execute(text("UPDATE fans SET automation_paused_until = NULL"))
    assert drive(w, w.runner().tick, model=model) == "replied"
    assert "back with you now" in w.out_messages()[-1] and len(w.rows(ContentOffer)) == 1


# ---- single active sender -------------------------------------------------------------------------------

def test_native_planner_and_delivery_never_act_on_a_runner_owned_fan(world):
    from onlyfans_runtime.native_planning import SyntheticNativeModel
    from onlyfans_runtime.native_worker import CycleReport, NativeWorker, WorkerAccount
    w = world()
    w.fan_says("hey, how is your day going?")
    due(w.engine)
    assert w.store.claim(TENANT, account_ids=(ACCOUNT,)) is None, "the reactive planner must skip an owned fan"
    model = SyntheticNativeModel("The native planner must not answer this.")
    native = NativeWorker(w.store, TENANT, (WorkerAccount(ACCOUNT, model, w.wire),))
    native.step()
    assert model.calls == [] and w.wire.calls == []
    # A queued runner intent is not dispatched by the worker's generic delivery pickup either.
    w.gate.begin_turn()
    intent = w.authority.prepare(TENANT, ACCOUNT, FAN, command_key="queued-fastt", expected_revision=w.state().revision,
                                 expected_input_revision=w.state().input_revision, body="held for the runner",
                                 origin="fastt", actor="fastt-runner")
    native._deliver(WorkerAccount(ACCOUNT, model, w.wire), CycleReport())
    assert w.wire.calls == [] and w.rows(OfTextIntent)[0].status == "queued"
    # Authority-level defence in depth: automated origins are refused for an owned fan, 'fastt' for others.
    with Session(w.engine) as s:
        runtime, policy = s.get(__import__("db.onlyfans_models", fromlist=["OfRuntime"]).OfRuntime, ACCOUNT), s.get(OfDeliveryPolicy, ACCOUNT)
        state, fan = s.get(OfConversationState, (ACCOUNT, FAN)), s.get(Fan, (ACCOUNT, FAN))
        now = w.authority._db_now(s)
        for origin in ("automated", "campaign", "engagement"):
            assert w.authority._eligibility(s, runtime, policy, state, fan, origin, now) == "runtime_owned"
        assert w.authority._eligibility(s, runtime, policy, state, fan, "fastt", now) is None
    w.authority.cancel_queued(TENANT, ACCOUNT, intent, "test cleanup")
    fastt_owner.release(w.store, TENANT, ACCOUNT, FAN, actor="synthetic-operator")
    with Session(w.engine) as s:
        runtime, policy = s.get(__import__("db.onlyfans_models", fromlist=["OfRuntime"]).OfRuntime, ACCOUNT), s.get(OfDeliveryPolicy, ACCOUNT)
        state, fan = s.get(OfConversationState, (ACCOUNT, FAN)), s.get(Fan, (ACCOUNT, FAN))
        assert w.authority._eligibility(s, runtime, policy, state, fan, "fastt", now) == "not_runtime_owner"
    assert w.store.claim(TENANT, account_ids=(ACCOUNT,)) is not None, "released: the native planner may claim again"


def test_owner_claim_is_durable_and_never_resumes_a_stop(world):
    w = world()
    pause(w)
    fastt_owner.claim(w.store, TENANT, ACCOUNT, FAN, actor="another-process")   # idempotent re-claim
    assert w.state().mode == "paused"
    [row] = w.rows(OfRuntimeOwner)
    assert row.owner == "fastt" and row.released_at is None
    runner = w.runner()
    w.fan_says("anyone there?")
    model = StubModel(["should never be asked"])
    assert drive(w, runner.tick, model=model) == "idle_not_automated" and model.calls == [] and w.wire.calls == []


# ---- scope ------------------------------------------------------------------------------------------------

def test_scope_is_exactly_one_account_and_one_fan(world, monkeypatch):
    import fastt_runner
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setenv("ONLYFANS_WORKER_TENANT", TENANT)
    monkeypatch.setenv("ONLYFANS_CONNECTION_KEY", "ab" * 32)
    for argv in (["--fan", "1,2"], ["--fan", "*"], ["--fan", "0"], ["--fan", ""], ["--account", "x y"],
                 ["--dry-run", "--enable-send"]):
        with pytest.raises(SystemExit):
            fastt_runner._parse(argv)
    args, tenant = fastt_runner._parse([])
    assert (args.account, args.fan, tenant) == ("of-176774528", "318022380", TENANT)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    with pytest.raises(SystemExit):
        fastt_runner._parse([])
    w = world()
    w.gate.begin_turn()
    with pytest.raises(FasttHalt, match="scope_violation"):
        w.gate.send_message(FAN + 1, "wrong fan")
    from onlyfans_runtime.fastt_capabilities import ScopedCapabilities
    import automation_executor as ax
    with ScopedCapabilities(w.scope, w.gate):
        assert ax._make_client(ACCOUNT) is w.gate
        with pytest.raises(FasttHalt, match="scope_violation"):
            ax._make_client("of-999")
    assert w.wire.calls == []


def test_gate_refuses_every_form_the_pilot_does_not_grant(world):
    w = world()
    w.fan_says("hey")
    with pytest.raises(FasttHalt, match="send_outside_turn"):
        w.gate.send_message(FAN, "no turn yet")
    w.gate.begin_turn()
    cases = [(dict(giphy_id="g1"), "unsupported_send_form"), (dict(locked_text=True), "unsupported_send_form"),
             (dict(scheduled_at="x"), "unsupported_send_form"),
             (dict(media_files=[PHOTO, VIDEO], previews=[PHOTO], price=0), "free_media_not_granted"),
             (dict(media_files=[PHOTO, VIDEO], previews=[PHOTO], price=5), "card_not_in_approved_catalogue"),
             (dict(media_files=[VIDEO], previews=[], price=3), "card_not_in_approved_catalogue"),
             (dict(media_files=[PHOTO, VIDEO], previews=[], price=3), "card_not_in_approved_catalogue"),
             (dict(price=3), "price_without_media")]
    for kw, reason in cases:
        with pytest.raises(FasttTurnAbort, match=reason):
            w.gate.send_message(FAN, "text", **kw)
    with pytest.raises(FasttTurnAbort, match="empty_text"):
        w.gate.send_message(FAN, "  ")
    from onlyfans_runtime.fastt_gate import FasttCapabilityDisabled
    for name in ("unsend_message", "get_chat_messages", "schedule_message"):
        with pytest.raises(FasttCapabilityDisabled):
            getattr(w.gate, name)
    assert w.gate.mark_chat_read(FAN) == {}
    assert w.wire.calls == [] and w.rows(OfTextIntent) == []


def test_policy_cap_halts_and_used_units_are_preserved(world):
    w = world()
    w.authority.configure(TENANT, ACCOUNT, expected_revision=1, enabled=True, media_enabled=True, limit_units=1,
                          throttle_seconds=1, actor="synthetic-operator", reason="cap of one")
    w.fan_says("hey")
    w.gate.begin_turn()
    assert w.gate.send_message(FAN, "first")["id"]
    with pytest.raises(FasttHalt, match="cap_exhausted"):
        w.gate.send_message(FAN, "second")
    with Session(w.engine) as s:
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1
    w.authority.configure(TENANT, ACCOUNT, expected_revision=2, enabled=True, media_enabled=True, limit_units=100,
                          throttle_seconds=1, actor="synthetic-operator", reason="raise cap")
    with Session(w.engine) as s:
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 1, "a policy change never resets the spent allowance"
    assert len(w.wire.calls) == 1


def test_preflight_refuses_unsafe_policy_for_a_live_start(world):
    w = world()
    w.authority.configure(TENANT, ACCOUNT, expected_revision=1, enabled=True, media_enabled=False, limit_units=500,
                          throttle_seconds=5, actor="synthetic-operator", reason="unsafe on purpose")
    problems = FasttRunner(w.store, w.authority, w.gate, w.scope).preflight(live=True)
    joined = " | ".join(problems)
    assert "exceeds 100" in joined and "below 20s" in joined and "native" in joined and "media" in joined
    assert w.runner().preflight(live=False) == [] or "exceeds 100" in " ".join(w.runner().preflight(live=False))


# ---- dry run -------------------------------------------------------------------------------------------------

def test_dry_run_calls_the_model_but_disables_every_provider_write(world):
    w = world()
    w.gate.dry_run = True
    w.fan_says("hey, how is your day going?")
    before = w.state().revision, w.state().input_revision
    model = StubModel([REPLY])
    runner = w.runner(dry_run=True)
    action = drive(w, runner.tick, model=model)
    assert len(model.calls) == 1 and action in {"turn_done", "turn_aborted:dry_run_provider_writes_disabled"}, action
    assert w.wire.calls == [] and w.rows(OfTextIntent) == [] and w.out_messages() == []
    assert (w.state().revision, w.state().input_revision) == before
    with Session(w.engine) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        assert (policy.used_units, policy.queued_count, policy.active_intent_id) == (0, 0, None)
    assert w.rows(OfMessageDelivery) == [] and w.rows(ContentOffer) == []


def test_dry_run_gate_refuses_a_send_even_if_ai_chatter_tried(world):
    w = world()
    w.gate.dry_run = True
    w.fan_says("hey")
    w.gate.begin_turn()
    with pytest.raises(FasttTurnAbort, match="dry_run_provider_writes_disabled"):
        w.gate.send_message(FAN, "would be sent")
    assert w.gate.would_send[0]["text"] == "would be sent" and w.wire.calls == []


# ---- scoped model gateway (llm_client.chat seam) -----------------------------------------------------------

def completion(content="hello there", finish="stop", usage=(1000, 50)):
    return json.dumps({"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content}}],
                       "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}).encode()


class Gateway:
    """A real ScopedModelGateway over a sealed-credential object whose network hop is replaced."""

    def __init__(self, w, behaviour, *, model_id="grok-4-1-fast-non-reasoning", **kw):
        from datetime import datetime, timedelta, timezone
        from onlyfans_runtime.provider_model import NativeProviderModel
        from onlyfans_runtime.scoped_model import ScopedModelGateway
        self.posts = []
        cred = NativeProviderModel(account_id=ACCOUNT, model_id=model_id, api_key="synthetic-key",
                                   expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

        async def post_once(body):
            self.posts.append(json.loads(body))
            result = behaviour() if callable(behaviour) else behaviour
            if isinstance(result, Exception):
                raise result
            return result
        cred._post_once = post_once
        self.gateway = ScopedModelGateway(w.store, cred, **kw)


def call(gateway, **kw):
    args = dict(model="grok-4-1-fast-non-reasoning", messages=[{"role": "user", "content": "hi"}], purpose="ai_chatter",
                account_id=ACCOUNT, fan_id=FAN, temperature=.7)
    args.update(kw)
    return gateway.chat(**args)


def test_scoped_gateway_reserves_calls_once_and_settles_against_the_account_ledger(world):
    from db.models import GrokCall, GrokDailyCost
    w = world()
    g = Gateway(w, completion("hello there"))
    result = drive(w, lambda: call(g.gateway))
    assert result.content == "hello there" and result.provider and result.tokens_in == 1000
    assert len(g.posts) == 1 and g.posts[0]["max_tokens"] == 400 and g.posts[0]["stream"] is False
    [row] = w.rows(GrokCall)
    assert (row.status, row.account_id, row.fan_id, row.purpose, row.tokens_in, row.tokens_out) == ("done", ACCOUNT, FAN, "ai_chatter", 1000, 50)
    [ledger] = w.rows(GrokDailyCost)
    assert ledger.call_count == 1 and ledger.cost_cents == row.cost_cents == result.cost_cents > 0


def test_scoped_gateway_refuses_other_models_accounts_and_the_daily_cap_before_any_post(world):
    from db.models import GrokCall
    from llm_client import LLMCapExceeded, LLMConfigError
    w = world()
    g = Gateway(w, completion())
    with pytest.raises(LLMConfigError):
        drive(w, lambda: call(g.gateway, model="deepseek-v4-flash"))        # not the pinned model: no house fallback
    with pytest.raises(LLMConfigError):
        drive(w, lambda: call(g.gateway, account_id="of-999"))
    with Session(w.engine) as s, s.begin():
        s.get(AccountAiConfig, ACCOUNT).daily_cost_cap_cents = 0
    with pytest.raises(LLMCapExceeded):
        drive(w, lambda: call(g.gateway))
    assert g.posts == [] and w.rows(GrokCall) == []


@pytest.mark.parametrize("behaviour,failure", [
    (TimeoutError("synthetic ambiguous"), "unknown"),
    (completion("cut off", finish="length"), "done"),
    (completion("", finish="stop"), "done"),
    (b"not json at all", "unknown")])
def test_scoped_gateway_failure_is_one_attempt_no_retry_and_never_a_reply(world, behaviour, failure):
    from db.models import GrokCall
    from llm_client import LLMHTTPError
    w = world()
    g = Gateway(w, behaviour)
    with pytest.raises(LLMHTTPError):
        drive(w, lambda: call(g.gateway))
    assert len(g.posts) == 1, "a model call is never retried"
    [row] = w.rows(GrokCall)
    assert row.status == failure and g.gateway.failures == 1


def test_scoped_gateway_call_budget_and_registration(world):
    from llm_client import LLMHTTPError, LLMConfigError
    w = world()
    g = Gateway(w, completion(), max_calls=1)
    drive(w, lambda: call(g.gateway))
    with pytest.raises(LLMHTTPError):
        drive(w, lambda: call(g.gateway))
    assert len(g.posts) == 1
    llm_client.register_scoped_gateway(g.gateway)
    try:
        with pytest.raises(LLMConfigError):
            llm_client.register_scoped_gateway(StubModel([]))      # one gateway per process
    finally:
        llm_client.clear_scoped_gateway()


def test_llm_chat_behaviour_is_unchanged_without_a_registered_gateway(world):
    w = world()
    with pytest.raises(RuntimeError, match="unavailable in dedicated OnlyFans roles"):
        drive(w, lambda: llm_client.chat(model="grok-4-1-fast-non-reasoning", messages=[{"role": "user", "content": "x"}],
                                         purpose="ai_chatter", account_id=ACCOUNT))
    gateway = StubModel(["via gateway"])
    result = drive(w, lambda: llm_client.chat(model="m", messages=[{"role": "user", "content": "x"}], purpose="p",
                                              account_id=ACCOUNT), model=gateway)
    assert result.content == "via gateway" and gateway.calls[0]["account_id"] == ACCOUNT


def test_entrypoint_preflight_is_read_only_and_exits_without_claiming_or_calling_a_model(world, monkeypatch, capsys):
    import fastt_runner
    from onlyfans_runtime import fastt_credentials
    w = world()
    fastt_owner.release(w.store, TENANT, ACCOUNT, FAN, actor="synthetic-operator")
    w.authority.configure(TENANT, ACCOUNT, expected_revision=1, enabled=True, media_enabled=True, limit_units=100,
                          throttle_seconds=20, actor="synthetic-operator", reason="pilot throttle")
    monkeypatch.setenv("ONLYFANS_WORKER_TENANT", TENANT)
    monkeypatch.setenv("ONLYFANS_CONNECTION_KEY", "ab" * 32)
    monkeypatch.setattr(fastt_credentials, "load_credentials", lambda *a, **k: (w.wire, None))
    args, tenant = fastt_runner._parse([])
    registered = []
    monkeypatch.setattr(llm_client, "register_scoped_gateway", lambda g: registered.append(g))

    async def go():
        return await fastt_runner._amain(args, tenant, w.store, threading.Event())
    assert drive(w, go) == 0
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert out[-1]["event"] == "preflight_ok" and out[-1]["send"] is False and out[-1]["owned"] is False
    assert registered == [] and w.rows(OfRuntimeOwner)[0].released_at is not None and w.wire.calls == []
