"""Event-fed cancellation of proactive engagement actions (INTEGRATION.md item 5).

Real writers (Store.drain/control, ledger.apply_observation, update_content) cancel
only the not-yet-sent action of the right fan/item/account. Dispatch-time validation
stays authoritative when a notification is skipped. Disposable PostgreSQL only.
"""
from datetime import timedelta
import uuid

import pytest
from sqlalchemy import event as sa_event, select
from sqlalchemy.orm import Session

from test_onlyfans_launch_engagement_pg import (  # noqa: F401  (fixtures + helpers)
    schema_template, database, rig, schedule, prepare, current)
from test_onlyfans_foundation_pg import seed, event as inbound_event
from test_onlyfans_ledger_pg import transaction, parsed
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from db.models import Fan, OfConversationState
from db.onlyfans_delivery_models import OfTextIntent
from onlyfans_runtime.content_management import get_content, update_content
from onlyfans_runtime.launch_engagement import events
from onlyfans_runtime.launch_engagement.contracts import digest
from onlyfans_runtime.launch_engagement.models import Action
from onlyfans_runtime.launch_engagement.service import EngagementService
from onlyfans_runtime.ledger import apply_observation
from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.store import Event, utcnow

OTHER_FAN, OTHER_ACCOUNT, OTHER_TENANT, OTHER_ACCOUNT_FAN = 102, "acc-b", "tenant-b", 201
ACTOR = "synthetic-operator"
CONTENT_ACTOR = "op-session:" + "a" * 64


def plant(engine, account, fan, item, *, anchor, status="scheduled", kind="offer", key=None, binding_id=None):
    """A bystander action row (only status/fan/item matter to the hooks)."""
    now = utcnow()
    snapshot = canonical({"version": 1, "account": account, "fan": fan, "kind": kind, "anchor": anchor,
                          "terms": {"item": {"id": item}} if kind == "offer" else None,
                          "source": {"item": item} if kind == "follow_up" else None})
    action = str(uuid.uuid4())
    with Session(engine) as s, s.begin():
        s.add(Action(account_id=account, action_id=action, binding_id=binding_id or "binding-" + account, fan_id=fan,
            kind=kind, anchor_message_id=anchor, command_key=key or digest(action), request_hash=digest("r"),
            snapshot_json=snapshot, snapshot_hash=digest(snapshot), status=status,
            due_at=now, expires_at=now + timedelta(hours=24), created_at=now, updated_at=now))
    return action


@pytest.fixture
def bystanders(rig):
    """Other fan on the same account and another account holding the SAME catalogue item id."""
    service, engine, item, _ = rig
    with Session(engine) as s, s.begin():
        s.add(Fan(account_id=ACCOUNT, fan_id=OTHER_FAN))
        s.flush()
        s.add(OfConversationState(account_id=ACCOUNT, fan_id=OTHER_FAN, mode="automated", revision=0,
                                  takeover_state="none", requires_attention=False))
    seed(rig[1].url.render_as_string(hide_password=False), engine, account=OTHER_ACCOUNT,
         tenant=OTHER_TENANT, fan=OTHER_ACCOUNT_FAN).close()
    return {"fan": plant(engine, ACCOUNT, OTHER_FAN, item, anchor=900),
            "account": plant(engine, OTHER_ACCOUNT, OTHER_ACCOUNT_FAN, item, anchor=901)}


def row(engine, account, action):
    with Session(engine) as s:
        r = s.get(Action, (account, action))
        s.expunge(r)
        return r


def untouched(engine, bystanders):
    for account, key in ((ACCOUNT, "fan"), (OTHER_ACCOUNT, "account")):
        r = row(engine, account, bystanders[key])
        assert r.status == "scheduled" and r.cancel_requested_at is None and r.reason is None


def drain(store, *evs):
    for e in evs:
        assert store.record(TENANT, ACCOUNT, e) == "accepted"
    store.drain(TENANT, ACCOUNT)


def test_inbound_reply_cancels_only_that_fan(rig, bystanders):
    service, engine, item, _ = rig
    action = schedule(rig)
    drain(service.store, inbound_event(mid=3, fan=FAN, body="hello again"))
    r = row(engine, ACCOUNT, action)
    assert (r.status, r.reason, r.cancelled_by) == ("cancelled", "reply", "runtime-event") and r.cancel_requested_at
    untouched(engine, bystanders)
    assert service.due(TENANT, ACCOUNT) is None


def test_outgoing_message_event_does_not_cancel(rig):
    service, engine, _, _ = rig
    action = schedule(rig)
    drain(service.store, Event(FAN, 3, utcnow().isoformat() + "+00:00", "out", "typed by the creator"))
    assert row(engine, ACCOUNT, action).cancel_requested_at is None


def test_queued_action_is_cancelled_before_wire_and_validation_refuses(rig, bystanders):
    service, engine, _, _ = rig
    action = schedule(rig)
    intent = prepare(rig, action)
    assert current(rig, intent)
    drain(service.store, inbound_event(mid=3, fan=FAN, body="hi"))
    r = row(engine, ACCOUNT, action)
    assert r.status == "queued" and r.reason == "reply" and r.cancel_requested_at is not None
    with Session(engine) as s:
        assert s.get(OfTextIntent, (ACCOUNT, intent)).status == "queued"  # TextAuthority settles it, not the hook
    assert not current(rig, intent)
    untouched(engine, bystanders)


@pytest.mark.parametrize("state", ["dispatching", "unknown", "accepted"])
def test_intents_past_queued_are_left_alone(rig, state):
    service, engine, _, now = rig
    action = schedule(rig)
    intent = prepare(rig, action)
    with Session(engine) as s, s.begin():
        i = s.get(OfTextIntent, (ACCOUNT, intent))
        i.status = state
        if state != "accepted":
            i.attempt_id, i.wire_started_at, i.reservation_held = str(uuid.uuid4()), now, True
    drain(service.store, inbound_event(mid=3, fan=FAN, body="hi"))
    r = row(engine, ACCOUNT, action)
    assert r.cancel_requested_at is None and r.reason is None and r.cancelled_by is None
    with Session(engine) as s:
        assert s.get(OfTextIntent, (ACCOUNT, intent)).status == state


@pytest.mark.parametrize("mode,verb,reason", [("paused", "pause_automation", "pause"),
    ("no_contact", "mark_no_contact", "no_contact"), ("human_controlled", "request_takeover", "takeover")])
def test_operator_control_cancels_only_that_fan(rig, bystanders, mode, verb, reason):
    service, engine, _, _ = rig
    action = schedule(rig)
    service.store.control(TENANT, ACCOUNT, FAN, mode=mode, expected_revision=0, command_key=digest(verb),
                          actor=ACTOR, reason="synthetic", action=verb)
    r = row(engine, ACCOUNT, action)
    assert r.status == "cancelled" and r.reason == reason
    untouched(engine, bystanders)


def apply(store, observation):
    with Session(store.engine) as s, s.begin():
        store._runtime(s, TENANT, ACCOUNT)
        return apply_observation(s, ACCOUNT, observation, now=utcnow())


def test_purchase_and_refund_cancel_only_that_fan_and_unchanged_state_does_not(rig, bystanders):
    service, engine, item, _ = rig
    action = schedule(rig)
    paid = parsed(transaction(id="evt-tx-1", status="done"))
    assert apply(service.store, paid) == "applied"
    r = row(engine, ACCOUNT, action)
    assert (r.status, r.reason) == ("cancelled", "purchase")
    untouched(engine, bystanders)
    # Same purchase state re-observed with different metadata is not a purchase event.
    fresh = plant(engine, ACCOUNT, FAN, item, anchor=950)
    assert apply(service.store, {**paid, "description": paid["description"] + " (re-read)"}) == "applied"
    assert row(engine, ACCOUNT, fresh).cancel_requested_at is None
    # A reversal is an event.
    refunded = {**paid, "status": "refunded"}
    assert apply(service.store, refunded) == "applied"
    r = row(engine, ACCOUNT, fresh)
    assert (r.status, r.reason) == ("cancelled", "purchase")
    untouched(engine, bystanders)


@pytest.mark.parametrize("patch", [{"enabled": False}, {"label": "Renamed set"}, {"description": "Edited."}])
def test_catalogue_edit_cancels_only_actions_using_that_item(rig, patch):
    service, engine, item, _ = rig
    action = schedule(rig)  # offer of `item`
    # Bystanders: same item on another account, another item on this account, terminal action for this item.
    with Session(engine) as s, s.begin():
        s.add(Fan(account_id=ACCOUNT, fan_id=OTHER_FAN))
        s.flush()
    seed(engine.url.render_as_string(hide_password=False), engine, account=OTHER_ACCOUNT,
         tenant=OTHER_TENANT, fan=OTHER_ACCOUNT_FAN).close()
    elsewhere = plant(engine, OTHER_ACCOUNT, OTHER_ACCOUNT_FAN, item, anchor=901)
    other_item = plant(engine, ACCOUNT, OTHER_FAN, item + 999, anchor=902)
    follow = plant(engine, ACCOUNT, OTHER_FAN, item, anchor=903, kind="follow_up", status="accepted")
    data = get_content(service.store, TENANT, ACCOUNT, item)
    update_content(service.store, TENANT, ACCOUNT, item, patch=patch, expected_revision=data["revision"],
                   expected_fingerprint=data["fingerprint"], command_key="c" * 64, actor=CONTENT_ACTOR)
    r = row(engine, ACCOUNT, action)
    assert (r.status, r.reason) == ("cancelled", "catalogue_changed")
    for account, a in ((OTHER_ACCOUNT, elsewhere), (ACCOUNT, other_item), (ACCOUNT, follow)):
        x = row(engine, account, a)
        assert x.cancel_requested_at is None and x.reason is None


def test_catalogue_edit_cancels_follow_up_source_item(rig):
    service, engine, item, _ = rig
    with Session(engine) as s, s.begin():
        s.add(Fan(account_id=ACCOUNT, fan_id=OTHER_FAN))
    follow = plant(engine, ACCOUNT, OTHER_FAN, item, anchor=903, kind="follow_up")
    data = get_content(service.store, TENANT, ACCOUNT, item)
    update_content(service.store, TENANT, ACCOUNT, item, patch={"label": "Renamed"}, expected_revision=data["revision"],
                   expected_fingerprint=data["fingerprint"], command_key="d" * 64, actor=CONTENT_ACTOR)
    assert row(engine, ACCOUNT, follow).status == "cancelled"


def test_settings_revision_already_cancels_actions(rig):
    service, engine, _, _ = rig
    action = schedule(rig)
    service.configure(TENANT, ACCOUNT, expected_revision=1, command_key=digest("off"), actor=ACTOR, settings={})
    assert row(engine, ACCOUNT, action).reason == "settings_changed"


def test_idle_path_is_one_indexed_read_and_no_writes(rig):
    service, engine, item, _ = rig
    terminal = plant(engine, ACCOUNT, FAN, item, anchor=777, status="accepted")  # never reopened
    statements = []
    sa_event.listen(engine, "before_cursor_execute", lambda c, cur, stmt, *a: statements.append(stmt))
    with Session(engine) as s, s.begin():
        assert events.cancel_for_fan(s, ACCOUNT, FAN, "reply") == 0
        assert events.cancel_for_fan(s, ACCOUNT, 4242, "purchase") == 0
        assert events.cancel_for_item(s, ACCOUNT, item) == 0
    assert len(statements) == 3 and all(x.lstrip().upper().startswith("SELECT") for x in statements)
    assert all("of_engagement_action" in x for x in statements)
    assert row(engine, ACCOUNT, terminal).status == "accepted"


def test_hook_failure_never_reaches_the_writer(rig, monkeypatch):
    service, engine, _, _ = rig
    action = schedule(rig)

    def boom(*a, **k):
        raise ValueError("synthetic hook failure")
    monkeypatch.setattr(EngagementService, "_cancel", staticmethod(boom))
    assert service.store.control(TENANT, ACCOUNT, FAN, mode="paused", expected_revision=0, command_key=digest("p"),
                                 actor=ACTOR, reason="synthetic", action="pause_automation") == 1
    assert row(engine, ACCOUNT, action).cancel_requested_at is None
    with Session(engine) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == "paused"


def test_validation_still_protects_when_the_event_hook_is_skipped(rig, monkeypatch):
    service, engine, _, _ = rig
    monkeypatch.setattr(events, "cancel_for_fan", lambda *a, **k: 0)
    waiting = schedule(rig)
    drain(service.store, inbound_event(mid=3, fan=FAN, body="a reply the hook never reported"))
    assert row(engine, ACCOUNT, waiting).status == "scheduled"
    assert service.due(TENANT, ACCOUNT) is None  # dispatch-time validation obsoleted it anyway
    assert service.detail(TENANT, ACCOUNT, waiting)["status"] == "cancelled"


def test_queued_validation_still_refuses_when_the_event_hook_is_skipped(rig, monkeypatch):
    service, engine, _, _ = rig
    monkeypatch.setattr(events, "cancel_for_fan", lambda *a, **k: 0)
    intent = prepare(rig, schedule(rig))
    service.store.control(TENANT, ACCOUNT, FAN, mode="paused", expected_revision=0, command_key=digest("q"),
                          actor=ACTOR, reason="synthetic", action="pause_automation")
    assert not current(rig, intent)
