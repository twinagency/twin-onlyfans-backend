"""Bounded R5 transitions and real durable conversation state; synthetic data only."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from db.models import Base, Account, Fan, Message, OfConversationState
from onlyfans_runtime import commercial_progress as p, commercial_controls as cc, quote

CFG = {"fastt_commercial_initiative": "active", "fastt_first_offer_window": [2, 6], "fastt_repeat_offer_window": [3, 6]}

def attempt(**kw):
    return p.start(100, "I like portraits", **kw)

def act(a, action="continue", source=100, **kw):
    return p.transition(a, action=action, source_id=source, cfg=CFG, **kw)

def msg(mid, direction, body="A complete response.", *, kind="ai_chatter", price=0, paid=False, unsent=False):
    return SimpleNamespace(message_id=mid, direction=direction, body=body, automation_kind=kind,
                           price_cents=price, is_paid=paid, is_unsent=unsent)

def test_first_counts_bubbles_repeat_counts_grouped_replies():
    a = attempt(); a.update(bubbles=5, replies=2)
    assert p.boundary(a, CFG, 2) == {"lower": 2, "upper": 6, "units": "bubbles", "completed": 5, "projected": 7, "due": True}
    a["phase"] = "repeat"
    assert p.boundary(a, CFG, 8)["projected"] == 3 and not p.boundary(a, CFG, 8)["due"]

def test_filler_and_invitations_consume_allowance_campaigns_do_not():
    a = attempt()
    p.count_messages(a, [msg(100, "in"), msg(101, "out", "👍"), msg(102, "out", "ok"),
        msg(103, "in"), msg(104, "out", "Want this portrait?"), msg(105, "out", kind="onlyfans_campaign")])
    assert (a["bubbles"], a["replies"], a["status"]) == (3, 2, "active")

@pytest.mark.parametrize("n", [5, 6, 7, 20, 100])
def test_continue_cannot_pass_boundary_and_never_becomes_card(n):
    a = attempt(); a["bubbles"] = n
    with pytest.raises(p.ProgressRefused, match="boundary-needs-decision"): act(a)
    assert "card_message_id" not in a

def test_direct_verified_request_offers_before_reference():
    a = attempt()
    act(a, "offer", evidence={"verified_match": True, "fan_interest": True}, item_ids=[10])
    assert a["status"] == "offer_planned" and a["bubbles"] == 0 and "card_message_id" not in a

def test_count_or_creator_suggestion_is_not_offer_authority():
    for n in (0, 6, 100):
        a = attempt(); a["bubbles"] = n
        with pytest.raises(p.ProgressRefused, match="verified-interest"):
            act(a, "offer", evidence={"verified_match": True}, item_ids=[10])

def test_invitation_does_not_reset_counter_or_count_as_card():
    a = attempt(); a.update(bubbles=6, replies=3)
    act(a, "invite", item_ids=[10], invitation_id="inv", evidence={"verified_match": True})
    assert (a["status"], a["bubbles"], a["replies"]) == ("awaiting_answer", 6, 3)
    assert "card_message_id" not in a

def test_one_extension_is_idempotent_and_next_answer_must_resolve():
    a = attempt(); a["bubbles"] = 5
    data = dict(item_ids=[10], clarification="product_choice", missing="Portrait or landscape?",
                evidence={"supporting_message_id": 100, "fan_text": "Which format can I choose?", "verified_match": True})
    act(a, "clarify", **data); act(a, "clarify", **data)
    assert a["extension_used"] and len(a["decisions"]) == 1
    for action in ("continue", "clarify", "invite"):
        with pytest.raises(p.ProgressRefused, match="extension"):
            act(a, action, source=110, invitation_id="another", **data)
    act(a, "offer", source=110, item_ids=[10], evidence={"verified_match": True, "fan_interest": True})
    assert a["status"] == "offer_planned" and a["extension_used"]

@pytest.mark.parametrize("reason", ["rapport", "keep_teasing", "not_ready", "try_different_wording", None])
def test_only_material_clarifications_qualify(reason):
    a = attempt(); a["bubbles"] = 6
    with pytest.raises(p.ProgressRefused, match="not-necessary"):
        act(a, "clarify", clarification=reason, missing="Anything", item_ids=[10], evidence={"supporting_message_id": 100})

@pytest.mark.parametrize("reason", sorted(p.DISPOSITIONS))
def test_dispositions_record_evidence_and_resumption(reason):
    a = attempt(); a["bubbles"] = 10
    act(a, "hold" if reason in ("distress", "takeover", "delivery_uncertain") else "disposition",
        disposition=reason, evidence={"source_message_id": 100, "verified_condition": reason}, resume_when="condition_verified")
    assert a["reason"] == reason and a["bubbles"] == 10 and a["resume_when"] == "condition_verified"

def test_unsupported_not_ready_cannot_hide_direct_request():
    with pytest.raises(p.ProgressRefused, match="disposition-needs-evidence"):
        act(attempt(), "disposition", disposition="not ready", evidence={"model_said": True}, resume_when="later")

def test_confirmed_delivery_closes_attempt_payment_does_not_reset_again():
    a = attempt()
    rows = [msg(100, "in"), msg(101, "out"), msg(102, "out", "Paid card", price=300)]
    p.count_messages(a, rows); p.count_messages(a, rows)
    assert (a["status"], a["card_message_id"], a["bubbles"], a["replies"]) == ("offered", 102, 1, 1)
    rows[-1].is_paid = True; p.count_messages(a, rows)
    assert a["status"] == "offered" and a["bubbles"] == 1

def test_config_windows_are_independent_and_legacy_scalars_not_converted():
    assert cc.controls({}).first_window == (2, 6) and cc.controls({}).repeat_window == (3, 6)
    assert cc.controls({"fastt_first_offer_reference": 12}).first_window == (2, 6)
    assert cc.validate_patch_fields(cc.ACTIVE_PILOT_PROFILE, ValueError) == cc.ACTIVE_PILOT_PROFILE
    for value in ([0, 6], [6, 2], [True, 6], [2, "6"], [2, 51], [2]):
        with pytest.raises(ValueError): cc.validate_patch_fields({cc.FIRST_WINDOW_FIELD: value}, ValueError)

@pytest.fixture
def persistence(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "progress.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    @asynccontextmanager
    async def session():
        async with factory() as s: yield s
    monkeypatch.setattr(p, "get_session", session)
    async def setup():
        async with engine.begin() as c: await c.run_sync(Base.metadata.create_all)
        async with factory.begin() as s:
            s.add(Account(id="900111", nickname="Synthetic")); await s.flush()
            s.add(Fan(account_id="900111", fan_id=910111)); await s.flush()
            s.add(OfConversationState(account_id="900111", fan_id=910111, mode="automated"))
            s.add(Message(account_id="900111", fan_id=910111, message_id=100, direction="in", body="Hello", created_at=datetime.utcnow()))
    asyncio.run(setup())
    yield factory
    asyncio.run(engine.dispose())

def test_restarts_topic_changes_settings_do_not_reset_and_generic_input_does_not_reopen(persistence):
    async def run():
        a = await p.load("900111", 910111, 100, "Hello", cfg=CFG)
        async with persistence.begin() as s:
            s.add(Message(account_id="900111", fan_id=910111, message_id=101, direction="out", body="ok", automation_kind="ai_chatter", created_at=datetime.utcnow()))
            s.add(Message(account_id="900111", fan_id=910111, message_id=102, direction="in", body="A different topic", created_at=datetime.utcnow()))
        b = await p.load("900111", 910111, 102, "A different topic", cfg={**CFG, "fastt_first_offer_window": [1, 2]})
        assert b["id"] == a["id"] and b["started_at"] == a["started_at"] and b["bubbles"] == 1
        await p.save_decision("900111", 910111, 102, "A different topic", cfg=CFG, action="disposition",
            disposition="no_current_interest", evidence={"source_message_id": 102}, resume_when="fresh_verified_interest")
        c = await p.load("900111", 910111, 103, "Another generic input", cfg=CFG)
        assert c["id"] == a["id"] and c["status"] == "closed_no_offer"
        d = await p.load("900111", 910111, 104, "A verified request", cfg=CFG, new_opportunity=True)
        assert d["id"] != a["id"]
    asyncio.run(run())

def test_edit_and_takeover_cannot_release_selling(persistence):
    async def run():
        await p.load("900111", 910111, 100, "Hello", cfg=CFG)
        async with persistence.begin() as s:
            row = await s.get(Message, ("900111", 910111, 100)); row.body = "Edited"
        with pytest.raises(p.ProgressRefused, match="input-revised"):
            await p.save_decision("900111", 910111, 100, "Hello", cfg=CFG, action="offer", item_ids=[1],
                                  evidence={"verified_match": True, "fan_interest": True})
        async with persistence.begin() as s:
            row = await s.get(OfConversationState, ("900111", 910111)); row.mode = "human_controlled"
        with pytest.raises(p.ProgressRefused, match="ownership-changed"):
            await p.save_decision("900111", 910111, 100, "Edited", cfg=CFG, action="offer", item_ids=[1],
                                  evidence={"verified_match": True, "fan_interest": True})
    asyncio.run(run())

def test_total_budget_uses_confirmed_purchases_only():
    rows = [msg(100, "in", "My budget is $5 total"), msg(101, "out", price=300), msg(102, "out", price=300, paid=True)]
    assert quote.budget_from_messages(rows) == {"budget_cents": 200, "budget_scope": "lifetime"}
    rows.append(msg(103, "in", "I can spend $4 each"))
    assert quote.budget_from_messages(rows) == {"budget_cents": 400, "budget_scope": "per_item"}

def test_migration_is_additive_idempotent_and_preserves_attempt_evidence(tmp_path):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import create_engine, inspect, text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[1]/"db/migrations/versions/20261102_0100_commercial_progress.py"
    spec = importlib.util.spec_from_file_location("commercial_migration", path)
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///"+str(tmp_path/"migration.db"))
    with engine.begin() as conn:
        for name in ("of_conversation_state", "of_sales_invitation", "of_text_intent"):
            conn.execute(text(f"CREATE TABLE {name}(id INTEGER PRIMARY KEY, legacy TEXT)"))
            conn.execute(text(f"INSERT INTO {name}(id,legacy) VALUES(1,'preserve')"))
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade(); migration.upgrade()
        assert "commercial_state_json" in {c["name"] for c in inspect(conn).get_columns("of_conversation_state")}
        assert "delivery_message_id" in {c["name"] for c in inspect(conn).get_columns("of_sales_invitation")}
        assert "quote_lifecycle" in {c["name"] for c in inspect(conn).get_columns("of_text_intent")}
        conn.execute(text("UPDATE of_conversation_state SET commercial_state_json='retained evidence'"))
        migration.downgrade()
        assert conn.execute(text("SELECT legacy,commercial_state_json FROM of_conversation_state")).one() == ("preserve", "retained evidence")
    engine.dispose()

@pytest.mark.parametrize("job", ["nudge", "discount", "post_buy"])
def test_live_policy_disables_legacy_timer_or_payment_sales_jobs(job):
    from native_pilot.engine import NativeEngine
    async def run():
        native = NativeEngine("900111", 910111, chat=None, send=None, content=SimpleNamespace(vault_mode=True), contextual=object())
        native._job = job
        async def no_unlinked(): return False
        native._fan_unlinked_paid = no_unlinked
        cfg = await native.load_config("900111", {"_native_on_request_sales_enabled": True,
            "fastt_contextual_sales_mode": "live", "enabled": True,
            "fastt_post_buy_upsell": True, "fastt_discount_resend": True})
        assert cfg["enabled"] is False
    asyncio.run(run())

def test_shadow_does_not_persist_or_reset_live_attempt(persistence):
    async def run():
        a = await p.load("900111", 910111, 100, "Hello", cfg=CFG)
        async with persistence() as s:
            before = (await s.get(OfConversationState, ("900111", 910111))).commercial_state_json
        projected = await p.load("900111", 910111, 100, "Hello", cfg={**CFG, "fastt_contextual_sales_mode": "shadow"})
        assert projected["id"] == a["id"]
        async with persistence() as s:
            assert (await s.get(OfConversationState, ("900111", 910111))).commercial_state_json == before
    asyncio.run(run())


def test_explicit_budget_is_not_a_blanket_stop_but_hardship_remains():
    from automations import sell_lane
    from datetime import datetime
    plain=sell_lane.Turn("show me photos, my budget is $50")
    assert sell_lane.fan_brake(None,plain,None,datetime.utcnow()).reason == "spend_regret"
    assert sell_lane.fan_brake(None,plain,None,datetime.utcnow(),scoped_budget=True) is None
    hardship=sell_lane.Turn("I can only afford $5 and I have bills to pay")
    assert sell_lane.fan_brake(None,hardship,None,datetime.utcnow(),scoped_budget=True).reason == "spend_regret"

def test_delivery_pointer_needs_both_scoped_positive_owners_and_is_not_cleared(persistence):
    from sqlalchemy import text
    async def run():
        a=await p.load("900111",910111,100,"Hello",cfg=CFG)
        async with persistence.begin() as s:
            state=await s.get(OfConversationState,("900111",910111));state.unresolved_attempt_id="fastt-pilot-7"
            await s.execute(text("CREATE TABLE twin_bridge_send_attempts(id INTEGER,account_id TEXT,fan_id INTEGER,state TEXT)"))
            await s.execute(text("CREATE TABLE native_pilot_receipts(attempt_id INTEGER,state TEXT)"))
            await s.execute(text("INSERT INTO twin_bridge_send_attempts VALUES(7,'900111',910111,'dispatching')"))
            await s.execute(text("INSERT INTO native_pilot_receipts VALUES(7,'dispatching')"))
        b=await p.load("900111",910111,100,"Hello",cfg=CFG)
        assert b["status"]=="held" and b["reason"]=="delivery_uncertain"
        async with persistence.begin() as s:
            await s.execute(text("UPDATE native_pilot_receipts SET state='sent'"))
        async with persistence() as s:
            assert await p.delivery_unresolved(s,await s.get(OfConversationState,("900111",910111)))
        async with persistence.begin() as s:
            await s.execute(text("UPDATE twin_bridge_send_attempts SET state='sent'"))
        c=await p.load("900111",910111,100,"Hello",cfg=CFG,new_opportunity=True)
        assert c["id"]==a["id"] and c["status"]=="active"
        async with persistence.begin() as s:
            state=await s.get(OfConversationState,("900111",910111))
            assert state.unresolved_attempt_id=="fastt-pilot-7" and not await p.delivery_unresolved(s,state)
            await s.execute(text("UPDATE twin_bridge_send_attempts SET fan_id=999999"))
        async with persistence() as s:
            assert await p.delivery_unresolved(s,await s.get(OfConversationState,("900111",910111)))
    asyncio.run(run())
