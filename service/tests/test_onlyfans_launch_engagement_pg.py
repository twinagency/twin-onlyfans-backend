"""Disposable PostgreSQL, synthetic history, no provider or production access.

The test-only origin constraint change rehearses Claude's integration migration;
no shared source file, migration or startup registration is changed here.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import os
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError

from test_onlyfans_foundation_pg import database as foundation_database
from test_onlyfans_text_delivery_pg import configured, ACCOUNT, FAN, TENANT
from test_onlyfans_media_pg import shelf, media_policy
from db.models import (AccountAiConfig, CatalogItem, ContentOffer, Fan, LadderState,
                       Message, OfConversationState, OfCreatorBinding, Transaction, VaultItem, VaultSend)
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfTextIntent, OfDeliveryPolicy
from onlyfans_runtime.launch_engagement.contracts import digest, intent_key, load_object
from onlyfans_runtime.launch_engagement.models import EngagementBase, Action
from onlyfans_runtime.launch_engagement.service import EngagementService
from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.store import Refused, Store, utcnow
from onlyfans_runtime.text_delivery import TextAuthority


@pytest.fixture(scope="module")
def schema_template():
    # Bootstrap once, then clone ONLY this suite's unique disposable template.
    lifecycle = foundation_database.__wrapped__()
    template = next(lifecycle)
    template[1].dispose()
    yield template[1].url.database
    try:
        next(lifecycle)
    except StopIteration:
        pass


@pytest.fixture
def database(schema_template):
    admin = create_engine(os.environ["ONLYFANS_TEST_PG_URL"], isolation_level="AUTOCOMMIT")
    assert schema_template.startswith("of_test_")
    name = "of_test_engagement_" + uuid.uuid4().hex
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE "{schema_template}"')
    url = admin.url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url, connect_args={"options": "-c timezone=UTC"})
    try:
        yield url, engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


@pytest.fixture
def rig(database, monkeypatch):
    _, engine = database
    store, authority = configured(database, mode="automated")
    item = shelf(engine)
    media_policy(authority)
    now = utcnow()
    monkeypatch.setattr(TextAuthority, "_db_now", staticmethod(lambda s: now))
    EngagementBase.metadata.create_all(engine)
    with engine.begin() as c:
        c.exec_driver_sql((Path(__file__).resolve().parents[1] / "onlyfans_runtime/launch_engagement/schema_contract.sql").read_text())
        # ONLY this throwaway database; the branch ships no migration.
        checks = c.execute(text("SELECT conname FROM pg_constraint WHERE conrelid='of_text_intent'::regclass AND contype='c' AND pg_get_constraintdef(oid) LIKE '%origin%'" )).scalars().all()
        for name in checks:
            c.exec_driver_sql('ALTER TABLE of_text_intent DROP CONSTRAINT "' + name + '"')
            c.exec_driver_sql('ALTER TABLE of_text_intent ADD CONSTRAINT "' + name + '" CHECK (origin IN (\'automated\',\'manual\',\'campaign\',\'engagement\'))')
    with Session(engine) as s, s.begin():
        s.get(CatalogItem, item).pricing_mode = "fixed"
        s.get(CatalogItem, item).tags = '["scenic"]'
        s.get(Fan, (ACCOUNT, FAN)).fetishes = "scenic"
        s.add(AccountAiConfig(account_id=ACCOUNT, ai_chatter_config_json=canonical({
            "pack_send_enabled": True, "pack_on_ask_enabled": True,
            "qualification_gate_enabled": True, "min_fan_msgs_between_offers": 0})))
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=1, direction="in",
                      body="I enjoy scenic photos and videos", created_at=now-timedelta(hours=26)))
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=2, direction="out",
                      body="Good to hear from you", created_at=now-timedelta(hours=25)))
    service = EngagementService(store)
    service.configure(TENANT, ACCOUNT, expected_revision=0, command_key=digest("config"),
        actor="synthetic-operator", settings={"enabled": True, "offers": True, "follow_ups": True,
                                               "reengagement": True})
    yield service, engine, item, now
    store.close()


def schedule(rig, *, kind="offer", key="schedule", tenant=TENANT):
    return rig[0].schedule(tenant, ACCOUNT, FAN, kind=kind, command_key=digest(key), actor="synthetic-planner")


def prepare(rig, identity, *, crash=False):
    service, engine, _, now = rig
    with Session(engine) as s, s.begin():
        runtime = service.store._runtime(s, TENANT, ACCOUNT)
        row = s.get(Action, (ACCOUNT, identity))
        data = load_object(row.snapshot_json)
        service.validate_prepare(s, runtime, action_id=identity, fan_id=FAN,
            command_key=intent_key(identity), body=data["body"], terms=data["terms"], now=now)
        terms = data["terms"]
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        intent = OfTextIntent(account_id=ACCOUNT, intent_id=str(uuid.uuid4()), binding_id=runtime.binding_id,
            fan_id=FAN, command_key=intent_key(identity), actor="synthetic-planner", origin="engagement",
            body=data["body"], command_hash=digest("request"), payload_hash=digest("wire"),
            control_revision=state.revision, input_revision=state.input_revision, policy_revision=policy.revision,
            status="queued", created_at=now, updated_at=now, expires_at=now+timedelta(minutes=5),
            catalog_item_id=terms["item"]["id"] if terms else None,
            catalog_snapshot_json=canonical(terms) if terms else None,
            media_json=canonical(terms["media_ids"]) if terms else "[]",
            preview_json=canonical(terms["preview_ids"]) if terms else "[]",
            price_cents=terms["price_cents"] if terms else 0, wire_version=1)
        s.add(intent)
        service.attach(s, runtime, row, intent, now)
        if crash:
            raise RuntimeError("synthetic crash before commit")
        return intent.intent_id


def current(rig, intent_id):
    service, engine, _, now = rig
    with Session(engine) as s, s.begin():
        runtime = service.store._runtime(s, TENANT, ACCOUNT)
        return service.current(s, s.get(OfTextIntent, (ACCOUNT, intent_id)), runtime, now)


def test_frozen_fixed_terms_duplicate_and_restart(rig, database):
    service, engine, item, _ = rig
    action = schedule(rig)
    assert schedule(rig) == schedule(rig, key="second-tick") == action
    proposal = service.due(TENANT, ACCOUNT)
    assert proposal["terms"]["media_ids"] == [11, 12, 13]
    assert proposal["terms"]["preview_ids"] == [11]
    assert proposal["terms"]["price_cents"] == 1000
    reopened = Store(database[0])
    try:
        assert EngagementService(reopened).due(TENANT, ACCOUNT) == proposal
    finally:
        reopened.close()
    intent = prepare(rig, action)
    assert current(rig, intent)
    assert service.due(TENANT, ACCOUNT) is None
    with pytest.raises(Refused, match="consumed"):
        prepare(rig, action)


def test_rollback_has_no_intent_or_consumed_schedule(rig):
    action = schedule(rig)
    with pytest.raises(RuntimeError):
        prepare(rig, action, crash=True)
    with Session(rig[1]) as s:
        assert s.get(Action, (ACCOUNT, action)).status == "scheduled"
        assert s.scalar(select(OfTextIntent)) is None
    assert current(rig, prepare(rig, action))


@pytest.mark.parametrize("change", ["reply", "purchase", "pause", "no_contact", "takeover", "mute",
    "fan_pause", "disable", "media_disable", "price", "previews", "order", "file", "owned", "preferences", "config", "stale_history"])
def test_final_permission_hook_refuses_changed_state(rig, change):
    service, engine, item, now = rig
    intent = prepare(rig, schedule(rig))
    assert current(rig, intent)
    with Session(engine) as s, s.begin():
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        fan = s.get(Fan, (ACCOUNT, FAN))
        if change == "reply":
            state.input_revision += 1
            s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=3, direction="in", body="Hello", created_at=now))
        elif change == "purchase":
            s.add(Transaction(account_id=ACCOUNT, fan_id=FAN, kind="tip", amount_cents=500, occurred_at=now))
        elif change in {"pause", "no_contact"}: state.mode = "paused" if change == "pause" else "no_contact"
        elif change == "takeover": state.takeover_state = "requested"
        elif change == "mute": fan.is_muted = True
        elif change == "fan_pause": fan.automation_paused_until = now+timedelta(days=1)
        elif change == "disable": s.get(OfRuntime, ACCOUNT).enabled = False
        elif change == "media_disable": s.get(OfDeliveryPolicy, ACCOUNT).media_enabled = False
        elif change == "price": s.get(CatalogItem, item).price_cents = 1100
        elif change == "previews": s.get(CatalogItem, item).preview_media_ids = "[]"
        elif change == "order": s.get(CatalogItem, item).media_ids = "[13,12,11]"
        elif change == "file": s.get(VaultItem, (ACCOUNT, 12)).content_hash = "changed"
        elif change == "owned":
            s.add(VaultSend(account_id=ACCOUNT, fan_id=FAN, media_id=12, message_id=5,
                            price_cents=1000, was_purchased=True, sent_at=now))
        elif change == "preferences": fan.fetishes = "different"
        elif change == "config": s.get(AccountAiConfig, ACCOUNT).ai_chatter_config_json = "{}"
        elif change == "stale_history": s.get(OfRuntime, ACCOUNT).last_sync_at = now-timedelta(hours=2)
    assert not current(rig, intent)


def test_cancel_after_grant_preserves_unknown_and_never_retries(rig):
    service, engine, _, now = rig
    action = schedule(rig)
    intent_id = prepare(rig, action)
    with Session(engine) as s, s.begin():
        intent = s.get(OfTextIntent, (ACCOUNT, intent_id))
        intent.status, intent.attempt_id = "dispatching", str(uuid.uuid4())
        intent.reservation_held, intent.wire_started_at = True, now
        s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id = intent.attempt_id
    result = service.cancel(TENANT, ACCOUNT, action, actor="synthetic-operator")
    assert result["status"] == "dispatching" and result["cancel_requested"]
    assert not current(rig, intent_id)
    with Session(engine) as s, s.begin():
        s.get(OfTextIntent, (ACCOUNT, intent_id)).status = "unknown"
    assert service.detail(TENANT, ACCOUNT, action)["status"] == "unknown"
    assert service.due(TENANT, ACCOUNT) is None
    with Session(engine) as s:
        assert s.get(OfTextIntent, (ACCOUNT, intent_id)).reservation_held
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id


def test_settings_revision_fences_queued_work_and_is_idempotent(rig):
    service, _, _, _ = rig
    intent = prepare(rig, schedule(rig))
    args = dict(expected_revision=1, command_key=digest("disable"), actor="synthetic-operator", settings={})
    assert service.configure(TENANT, ACCOUNT, **args) == 2
    assert service.configure(TENANT, ACCOUNT, **args) == 2
    assert not current(rig, intent)
    with pytest.raises(Refused):
        service.configure(TENANT, ACCOUNT, **{**args, "settings": {"offers": True}})


def test_scope_and_command_conflicts(rig):
    service = rig[0]
    action = schedule(rig)
    with pytest.raises(Refused): schedule(rig, tenant="foreign-tenant")
    with pytest.raises(Refused): service.cancel("foreign-tenant", ACCOUNT, action, actor="operator")
    with pytest.raises(Refused): schedule(rig, kind="reengage")


def test_competing_workers_reserve_one_action_and_one_intent(rig):
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda n: schedule(rig, key="tick-"+str(n)), range(2)))
    assert ids[0] == ids[1]
    def attempt(_):
        try: return prepare(rig, ids[0])
        except Refused: return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(x is not None for x in results) == 1


def test_expiration_is_durable_not_extended_by_restart(rig, monkeypatch):
    service, engine, _, now = rig
    action = schedule(rig)
    monkeypatch.setattr(TextAuthority, "_db_now", staticmethod(lambda s: now+timedelta(days=2)))
    assert service.due(TENANT, ACCOUNT) is None
    assert service.detail(TENANT, ACCOUNT, action)["status"] == "expired"
    assert schedule(rig) == action


def test_frequency_counts_other_lanes_and_unprojected_acceptance(rig):
    service, engine, _, now = rig
    action = schedule(rig)
    with Session(engine) as s, s.begin():
        # Another fan's human/campaign messages reach the configured account cap.
        s.add(Fan(account_id=ACCOUNT, fan_id=102))
        s.flush()
        for i in range(10):
            s.add(Message(account_id=ACCOUNT, fan_id=102, message_id=i+50,
                          direction="out", body="Synthetic", created_at=now-timedelta(minutes=5)))
    assert service.due(TENANT, ACCOUNT) is None
    assert service.detail(TENANT, ACCOUNT, action)["status"] == "scheduled"


def old_unpaid(rig, age):
    _, engine, item, now = rig
    with Session(engine) as s, s.begin():
        s.add(ContentOffer(account_id=ACCOUNT, fan_id=FAN, item_id=item, mode="ppv", price_cents=1000,
                           offer_message_id=2, status="open", offered_at=now-timedelta(hours=age)))


def test_unpaid_offer_cooldown_ends_without_rewriting_ownership(rig):
    old_unpaid(rig, 73)
    with Session(rig[1]) as s, s.begin():
        s.add(LadderState(account_id=ACCOUNT, fan_id=FAN, status="tapped", last_ask_at=rig[3]-timedelta(hours=73)))
    assert schedule(rig)
    with Session(rig[1]) as s:
        assert s.scalar(select(ContentOffer)).status == "open"
        assert s.get(LadderState, (ACCOUNT, FAN)).status == "tapped"


def test_recent_unpaid_offer_blocks_new_offer(rig):
    old_unpaid(rig, 30)
    with pytest.raises(Refused, match="unpaid_offer_cooldown"):
        schedule(rig)


def test_followup_is_text_only_and_purchase_cancels(rig):
    service, engine, _, now = rig
    with Session(engine) as s, s.begin():
        s.get(Message, (ACCOUNT, FAN, 1)).created_at = now-timedelta(hours=50)
        msg = s.get(Message, (ACCOUNT, FAN, 2))
        msg.created_at, msg.price_cents = now-timedelta(hours=49), 1000
    old_unpaid(rig, 49)
    action = schedule(rig, kind="follow_up")
    assert service.due(TENANT, ACCOUNT)["terms"] is None
    intent = prepare(rig, action)
    assert current(rig, intent)
    with Session(engine) as s, s.begin():
        s.get(Message, (ACCOUNT, FAN, 2)).is_paid = True
    assert not current(rig, intent)


def test_reengagement_unaffected_by_unpaid_selling_brake(rig):
    service, engine, _, now = rig
    with Session(engine) as s, s.begin():
        s.get(Message, (ACCOUNT, FAN, 1)).created_at = now-timedelta(hours=170)
        s.get(Message, (ACCOUNT, FAN, 2)).created_at = now-timedelta(hours=169)
        s.add(LadderState(account_id=ACCOUNT, fan_id=FAN, status="tapped", last_ask_at=now))
    old_unpaid(rig, 12)
    action = schedule(rig, kind="reengage")
    assert service.due(TENANT, ACCOUNT)["terms"] is None
    assert current(rig, prepare(rig, action))


def test_database_guards_preserve_definition_and_terminal_state(rig):
    service, engine, _, now = rig
    action = schedule(rig)
    with pytest.raises(DBAPIError, match="immutable"):
        with Session(engine) as s, s.begin():
            row = s.get(Action, (ACCOUNT, action))
            row.snapshot_json, row.snapshot_hash = "{}", digest({})
    service.cancel(TENANT, ACCOUNT, action, actor="operator")
    with pytest.raises(DBAPIError, match="reopened"):
        with Session(engine) as s, s.begin():
            s.get(Action, (ACCOUNT, action)).status = "scheduled"
    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as c:
            c.exec_driver_sql("DELETE FROM of_engagement_settings_change")


def test_schedule_waits_without_creating_intent(rig, monkeypatch):
    service, engine, _, now = rig
    with Session(engine) as s, s.begin():
        s.get(Message, (ACCOUNT, FAN, 2)).created_at = now-timedelta(hours=1)
    action = schedule(rig)
    assert service.due(TENANT, ACCOUNT) is None
    with Session(engine) as s:
        assert s.scalar(select(OfTextIntent)) is None
    later = now+timedelta(hours=23, seconds=1)
    monkeypatch.setattr(TextAuthority, "_db_now", staticmethod(lambda s: later))
    with Session(engine) as s, s.begin():
        s.get(OfRuntime, ACCOUNT).last_sync_at = later
    assert service.due(TENANT, ACCOUNT)["action_id"] == action


def test_preference_rank_keeps_fixed_price_and_skips_owned_content(rig):
    service, engine, first, now = rig
    with Session(engine) as s, s.begin():
        s.get(CatalogItem, first).tags = '["other"]'
        s.add(VaultItem(account_id=ACCOUNT, media_id=21, kind="image", created_at=now, content_hash="synthetic-21"))
        second = CatalogItem(account_id=ACCOUNT, description_for_ai="A scenic photo", media_ids="[21]",
            preview_media_ids="[]", pricing_mode="fixed", price_cents=1700, tags='["scenic"]')
        s.add(second); s.flush()
        second_id = second.id
    action = schedule(rig)
    assert service.due(TENANT, ACCOUNT)["terms"]["item"]["id"] == second_id
    assert service.due(TENANT, ACCOUNT)["terms"]["price_cents"] == 1700
    service.cancel(TENANT, ACCOUNT, action, actor="operator")
    with Session(engine) as s, s.begin():
        s.add(VaultSend(account_id=ACCOUNT, fan_id=FAN, media_id=21, message_id=10,
                        price_cents=1700, was_purchased=True, sent_at=now))
        fan = s.get(Fan, (ACCOUNT, FAN))
        from onlyfans_runtime.launch_engagement.readers import candidates
        from onlyfans_runtime.launch_engagement.contracts import Policy
        s.flush()
        assert [t["item"]["id"] for t in candidates(s, ACCOUNT, fan, Policy(), now)] == [first]


def test_recent_preview_filler_does_not_exclude_unowned_payoff(rig):
    with Session(rig[1]) as s, s.begin():
        s.add(VaultSend(account_id=ACCOUNT, fan_id=FAN, media_id=11, message_id=10,
                        price_cents=0, was_purchased=True, sent_at=rig[3]))
    assert schedule(rig)


@pytest.mark.parametrize("field,value", [("body", "Changed"), ("price_cents", 1100),
    ("media_json", "[13,12,11]"), ("preview_json", "[]"), ("actor", "other")])
def test_pre_dispatch_rejects_changed_payload(rig, field, value):
    identity = prepare(rig, schedule(rig))
    with Session(rig[1]) as s, s.begin():
        setattr(s.get(OfTextIntent, (ACCOUNT, identity)), field, value)
    assert not current(rig, identity)


def test_unprojected_accepted_intent_counts_towards_account_frequency(rig):
    service, engine, _, now = rig
    # One paid attempt accepted with no history projection yet.
    identity = prepare(rig, schedule(rig))
    with Session(engine) as s, s.begin():
        intent = s.get(OfTextIntent, (ACCOUNT, identity))
        intent.status = "accepted"
        from onlyfans_runtime.launch_engagement.readers import frequency
        from onlyfans_runtime.launch_engagement.contracts import Policy
        s.flush()
        with pytest.raises(Refused, match="frequency_limit"):
            frequency(s, ACCOUNT, FAN, Policy(), now)


def test_granted_intent_owns_its_unresolved_hold(rig):
    service, engine, _, now = rig
    identity = prepare(rig, schedule(rig))
    with Session(engine) as s, s.begin():
        intent = s.get(OfTextIntent, (ACCOUNT, identity))
        intent.status, intent.attempt_id = "dispatching", str(uuid.uuid4())
        s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id = intent.attempt_id
    assert current(rig, identity)


def test_revoked_binding_refuses_final_hook_even_with_historical_runtime(rig):
    service, engine, _, now = rig
    identity = prepare(rig, schedule(rig))
    with Session(engine) as s, s.begin():
        s.get(OfCreatorBinding, "binding-" + ACCOUNT).revoked_at = now
    with Session(engine) as s, s.begin():
        runtime = service.authority._historical_runtime(s, TENANT, ACCOUNT)
        assert not service.current(s, s.get(OfTextIntent, (ACCOUNT, identity)), runtime, now)


def test_future_restart_does_not_resurrect_cancelled_action(rig, database):
    service, _, _, _ = rig
    identity = schedule(rig)
    service.cancel(TENANT, ACCOUNT, identity, actor="operator")
    store = Store(database[0])
    try:
        restarted = EngagementService(store)
        assert restarted.due(TENANT, ACCOUNT) is None
        assert restarted.detail(TENANT, ACCOUNT, identity)["status"] == "cancelled"
    finally:
        store.close()
