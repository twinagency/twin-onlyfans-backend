"""Administrator activation: status reasons, bulk enablement, future-conversation setting, limits, permissions.

Disposable PostgreSQL only. Synthetic accounts, fans and credentials; no provider call, no message, no production data.
"""
import hashlib
import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401 -- pytest fixture
from test_onlyfans_connections_pg import verified, change, LIMITS, ACTOR, TENANT, MODEL, settings  # noqa: F401
from db.models import AccountAiConfig, Fan, OfConversationState, SkipList
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfDeliveryPolicyChange
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_models import OfControlAudit, OfRuntime
from onlyfans_commands.launch_control_api import control_router, read_router
from onlyfans_read.agency_scope import AgencyScope, AgencyScopeMiddleware, current_scope
from onlyfans_read.projections import conversation_summary
from onlyfans_read.repo import ConversationRow, StateRow
from onlyfans_runtime import activation_commands as commands
from onlyfans_runtime.activation_status import activation_status, conversation_counts
from onlyfans_runtime.launch_personality_credentials import personality_credential_status
from onlyfans_runtime.connections import set_model_credential, check_model_credential, _open
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow

KEY = "synthetic-model-key-0123456789"
MODEL_CRED = {"modelId": MODEL, "apiKey": KEY}


def key(label):
    return hashlib.sha256(label.encode()).hexdigest()


def reader(s, account, binding):
    return personality_credential_status(s, account, binding)


def fingerprint(store, account):
    with Session(store.engine) as s:
        return commands.fingerprint(s, store._runtime(s, TENANT, account, lock=False))


def status(store, account, runner_path=None):
    with Session(store.engine) as s, s.begin():
        return activation_status(s, store._runtime(s, TENANT, account, lock=False), credential_reader=reader,
                                 runner_path=runner_path)


def runner_file(tmp_path, entry, name="supervisor.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"updated_at": utcnow().isoformat() + "Z", "interval_s": 30.0, "accounts": entry}))
    return str(path)


@pytest.fixture
def account(database, monkeypatch):
    """A connected, verified account with its OWN model credential, not yet activated."""
    store, acct, _ = verified(database, monkeypatch, model=MODEL_CRED)
    with Session(store.engine) as s, s.begin():
        cfg = s.get(AccountAiConfig, acct)
        cfg.model = MODEL
        s.execute(OfRuntime.__table__.update().where(OfRuntime.account_id == acct).values(recovery_needed=False))
    yield store, acct, database[1]
    store.close()


def add_fan(engine, acct, fan, mode="paused", revision=0, takeover="none", muted=False, unresolved=None):
    with Session(engine) as s, s.begin():
        s.add(Fan(account_id=acct, fan_id=fan, is_muted=muted))
        s.flush()
        s.add(OfConversationState(account_id=acct, fan_id=fan, mode=mode, revision=revision, takeover_state=takeover,
                                  requires_attention=True, unresolved_attempt_id=unresolved))


def activate(store, acct, limit=7):
    with Session(store.engine) as s:
        rev = s.get(OfProviderConnection, acct).revision
    change(store, acct, "activate", rev, key("activate"), {**LIMITS, "limitUnits": limit})


def stage(result, name):
    return result["stages"][name]


# ── 1/2: status with exact reasons ───────────────────────────────────────────────────────────────────────────

def test_each_stage_names_its_exact_reason_and_nothing_is_ready_before_activation(account, tmp_path):
    store, acct, _ = account
    result = status(store, acct)
    assert stage(result, "connection")["state"] == "ok" and stage(result, "model")["state"] == "ok"
    assert stage(result, "runtime")["state"] == "unknown" and stage(result, "runtime")["code"] == "sender_unknown"
    assert stage(result, "replies")["code"] == "account_not_activated" and result["ready"] is False
    assert result["futureConversations"]["enabled"] is False and result["activated"] is False
    # Held sender: the supervisor's reason is translated, not hidden.
    held = status(store, acct, runner_file(tmp_path, {acct: {"state": "held", "reason": "manifest:no-approved-items", "restarts": 0}}))
    assert stage(held, "runtime")["code"] == "sender_held_catalogue" and "approved" in stage(held, "runtime")["reason"]
    off = status(store, acct, runner_file(tmp_path, {acct: {"state": "held", "reason": "runtime-disabled", "restarts": 0}}, "b.json"))
    assert stage(off, "runtime")["code"] == "account_not_activated"
    absent = status(store, acct, runner_file(tmp_path, {}, "c.json"))
    assert stage(absent, "runtime")["code"] == "sender_not_supervised"


def test_missing_or_foreign_model_credential_is_not_usable(database, monkeypatch):
    store, acct, _ = verified(database, monkeypatch, model=None)
    try:
        with Session(store.engine) as s, s.begin():
            s.get(AccountAiConfig, acct).model = MODEL
        result = status(store, acct)
        assert stage(result, "model")["code"] == "model_credential_missing" and stage(result, "model")["action"] == "model_credential"
        # Another model than the selected one is flagged, never accepted.
        with Session(store.engine) as s:
            rev = s.get(OfProviderConnection, acct).revision
        set_model_credential(store, TENANT, acct, model={"modelId": "deepseek-v4-flash", "apiKey": KEY}, expected_revision=rev,
                             command_key=key("m1"), actor=ACTOR)
        assert stage(status(store, acct), "model")["code"] == "model_credential_model_mismatch"
    finally:
        store.close()


def test_model_credential_is_per_account_replay_safe_and_never_returned(account, monkeypatch):
    store, acct, engine = account
    with Session(engine) as s:
        rev = s.get(OfProviderConnection, acct).revision
    new = {"modelId": MODEL, "apiKey": "another-accounts-own-key-987654321"}
    revision = set_model_credential(store, TENANT, acct, model=new, expected_revision=rev, command_key=key("m"), actor=ACTOR)
    assert revision == rev + 1
    assert set_model_credential(store, TENANT, acct, model=new, expected_revision=rev, command_key=key("m"), actor=ACTOR) == revision
    with pytest.raises(Refused):
        set_model_credential(store, TENANT, acct, model=new, expected_revision=rev, command_key=key("m2"), actor=ACTOR)  # stale revision
    with pytest.raises(Refused):
        set_model_credential(store, TENANT, acct, model={"modelId": "no-such-model", "apiKey": KEY}, expected_revision=revision,
                             command_key=key("m3"), actor=ACTOR)
    with Session(engine) as s:
        row = s.get(OfProviderConnection, acct)
        assert _open(row, TENANT)["model"] == new and row.status == "verified"
        assert KEY not in json.dumps(status(store, acct)) and new["apiKey"] not in json.dumps(status(store, acct))


def test_credential_check_is_read_only_and_maps_provider_answers(account):
    import httpx
    store, acct, engine = account
    with Session(engine) as s:
        rev = s.get(OfProviderConnection, acct).revision
    assert check_model_credential(store, TENANT, acct)["state"] == "unsupported"      # xAI has no key endpoint: never claimed usable
    set_model_credential(store, TENANT, acct, model={"modelId": "openrouter/deepseek-v4-flash", "apiKey": KEY}, expected_revision=rev,
                         command_key=key("or"), actor=ACTOR)
    def run(code, body=None):
        return check_model_credential(store, TENANT, acct, transport=httpx.MockTransport(
            lambda request: httpx.Response(code, json=body or {})) )
    assert run(200, {"data": {"limit_remaining": 4.5}})["state"] == "usable"
    assert run(200, {"data": {"limit_remaining": 0}})["state"] == "no_credit"
    assert run(401)["state"] == "rejected" and run(402)["state"] == "no_credit" and run(500)["state"] == "unavailable"


def test_allowance_budget_and_future_setting_are_visible(account):
    store, acct, engine = account
    activate(store, acct, limit=7)
    with Session(engine) as s, s.begin():
        s.get(OfDeliveryPolicy, acct).used_units = 7
    result = status(store, acct)
    assert result["allowance"]["state"] == "exhausted" and stage(result, "replies")["code"] == "allowance_exhausted"
    assert result["allowance"]["usedUnits"] == 7 and result["budget"]["dailyCostCapCents"] == 100
    assert result["futureConversations"] == {"enabled": False, "startsAt": None, "default": False}


def test_budget_exhaustion_and_too_small_budget_are_flagged_without_changing_the_cap(account):
    from db.models import GrokDailyCost
    store, acct, engine = account
    activate(store, acct, limit=500)
    with Session(engine) as s, s.begin():
        s.add(GrokDailyCost(day=utcnow().date().isoformat(), account_id=acct, provider="xai", cost_cents=10000, call_count=9, is_capped=True,
                            updated_at=utcnow()))
    result = status(store, acct)
    assert result["budget"]["state"] == "exhausted" and stage(result, "model")["code"] == "budget_exhausted"
    with Session(engine) as s:
        assert s.get(AccountAiConfig, acct).daily_cost_cap_cents == 100


# ── 3: bulk enablement ───────────────────────────────────────────────────────────────────────────────────────

def seed_mix(engine, acct):
    add_fan(engine, acct, 1001)                                           # eligible
    add_fan(engine, acct, 1002)                                           # eligible
    add_fan(engine, acct, 1003, muted=True)                               # muted
    add_fan(engine, acct, 1004, mode="human_controlled", revision=2, takeover="effective")
    add_fan(engine, acct, 1005, mode="no_contact", revision=1)
    add_fan(engine, acct, 1006, revision=3)                               # paused by staff earlier
    add_fan(engine, acct, 1007, unresolved="attempt-x")                   # unresolved delivery holds
    add_fan(engine, acct, 1008, mode="automated", revision=1)             # already on
    add_fan(engine, acct, 1009, mode="automated")                         # engine will pause it for identity
    with Session(engine) as s:
        pass


def identity_pause(store, acct, fan):
    store.control(TENANT, acct, fan, mode="paused", expected_revision=0, command_key=key(f"idp{fan}"), actor="identity-guard",
                  reason="Fan asked whether he is talking to AI; a human must answer.", action="pause_automation")


def test_enable_existing_preserves_protected_states_and_reports_why(account):
    store, acct, engine = account
    seed_mix(engine, acct)
    identity_pause(store, acct, 1009)
    with Session(engine) as s, s.begin():
        s.add(SkipList(account_id=acct, fan_id=1001, reason="of_restricted"))
    # Not activated: the preview says why and the command refuses without changing a single conversation.
    pre = commands_preview(store, acct)
    assert "account_not_activated" in pre["blockers"]
    with pytest.raises(commands.Blocked):
        run_enable(store, acct, "k0")
    assert enabled_count(engine, acct) == 1
    activate(store, acct)
    pre = commands_preview(store, acct)
    assert pre["blockers"] == [] and pre["counts"]["wouldEnable"] == 1  # 1001 is hard-restricted, 1007 unresolved, 1003 muted
    assert pre["counts"]["muted"] == 1 and pre["counts"]["restricted"] == 1 and pre["counts"]["unresolvedDelivery"] == 1
    assert pre["counts"]["noContact"] == 1 and pre["counts"]["humanTakeover"] == 1 and pre["counts"]["askedForPerson"] == 1
    assert pre["counts"]["pausedByStaff"] == 1 and pre["counts"]["alreadyOn"] == 1
    result = run_enable(store, acct, "k1")
    assert result["enabled"] == 1 and result["done"] is True and result["skipped"]["restricted"] == 1
    assert result["skipped"]["muted"] == 1 and result["skipped"]["unresolved_delivery"] == 1
    with Session(engine) as s:
        modes = {fan: (m, rev, t) for fan, m, rev, t in s.execute(select(
            OfConversationState.fan_id, OfConversationState.mode, OfConversationState.revision, OfConversationState.takeover_state))}
        assert modes[1002] == ("automated", 1, "none")
        assert modes[1004] == ("human_controlled", 2, "effective") and modes[1005] == ("no_contact", 1, "none")
        assert modes[1006][0] == "paused" and modes[1003][0] == "paused" and modes[1007][0] == "paused"
        assert modes[1001][0] == "paused" and modes[1009][0] == "paused"   # restricted and escalated stay as they were
        audit = s.scalar(select(func.count()).select_from(OfControlAudit).where(OfControlAudit.fan_id == 1002, OfControlAudit.action == "resume_automation"))
        assert audit == 1
        row = s.get(OfLaunchCommand, (acct, key("k1")))
        assert row.kind == "bulk_enable" and json.loads(row.result_json)["enabled"] == 1


def commands_preview(store, acct):
    with Session(store.engine) as s:
        return commands.preview_existing(s, store._runtime(s, TENANT, acct, lock=False), credential_reader=reader)


def run_enable(store, acct, label, **kwargs):
    return commands.enable_existing(store, TENANT, acct, command_key=key(label), actor=ACTOR, reason="Activate existing chats",
                                    expected_fingerprint=fingerprint(store, acct), credential_reader=reader, **kwargs)


def enabled_count(engine, acct):
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(OfConversationState).where(
            OfConversationState.account_id == acct, OfConversationState.mode == "automated"))


def test_batches_resume_and_a_replayed_batch_changes_nothing(account):
    store, acct, engine = account
    activate(store, acct)
    for fan in range(2000, 2007):
        add_fan(engine, acct, fan)
    fp = fingerprint(store, acct)
    first = commands.enable_existing(store, TENANT, acct, command_key=key("b1"), actor=ACTOR, reason="Activate",
                                     expected_fingerprint=fp, batch=3, credential_reader=reader)
    assert first["enabled"] == 3 and first["done"] is False and first["nextAfterFan"] == "2002"
    again = commands.enable_existing(store, TENANT, acct, command_key=key("b1"), actor=ACTOR, reason="Activate",
                                     expected_fingerprint=fp, batch=3, credential_reader=reader)
    assert again == first and enabled_count(engine, acct) == 3
    total, after, n = first["enabled"], first["nextAfterFan"], 2
    while True:
        part = commands.enable_existing(store, TENANT, acct, command_key=key(f"b{n}"), actor=ACTOR, reason="Activate",
                                        expected_fingerprint=fp, after_fan=int(after), batch=3, credential_reader=reader)
        total += part["enabled"]
        if part["done"]:
            break
        after, n = part["nextAfterFan"], n + 1
    assert total == 7 and enabled_count(engine, acct) == 7
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfControlAudit).where(OfControlAudit.action == "resume_automation")) == 7


def test_scope_selection_limits_which_chats_are_enabled(account):
    import json as j
    store, acct, engine = account
    activate(store, acct)
    for fan in (3001, 3002, 3003):
        add_fan(engine, acct, fan)
    with Session(engine) as s, s.begin():
        s.get(AccountAiConfig, acct).ai_chatter_config_json = j.dumps({"fastt_scope": {"mode": "selected", "fans": [3002]}})
    result = run_enable(store, acct, "scope")
    assert result["enabled"] == 1 and result["skipped"]["outside_scope"] == 2
    assert enabled_count(engine, acct) == 1


def test_stale_fingerprint_and_invalid_arguments_are_refused(account):
    store, acct, _ = account
    activate(store, acct)
    old = fingerprint(store, acct)
    with Session(store.engine) as s:
        rev = s.get(OfProviderConnection, acct).revision
    set_model_credential(store, TENANT, acct, model={"modelId": MODEL, "apiKey": "fresh-key-for-this-account-1234"}, expected_revision=rev,
                         command_key=key("rot"), actor=ACTOR)
    # Changing the connection record moves the fingerprint; the earlier decision is refused.
    with pytest.raises(Refused):
        commands.enable_existing(store, TENANT, acct, command_key=key("s"), actor=ACTOR, reason="x", expected_fingerprint=old, credential_reader=reader)
    with pytest.raises(Refused):
        commands.enable_existing(store, TENANT, acct, command_key=key("s2"), actor=ACTOR, reason="x", expected_fingerprint=fingerprint(store, acct),
                                 batch=100000, credential_reader=reader)


# ── 4: future conversations, default off ─────────────────────────────────────────────────────────────────────

def test_future_conversations_default_off_explicit_on_and_cleared_by_reconnect(account):
    store, acct, engine = account
    with Session(engine) as s:
        assert s.get(OfProviderConnection, acct).automate_new_conversations is False
    with pytest.raises(commands.Blocked):    # not activated yet
        set_future(store, acct, True, "f0")
    activate(store, acct)
    with Session(engine) as s:
        assert s.get(OfProviderConnection, acct).automate_new_conversations is False   # activation never arms it
    # Default off: a brand new conversation is not enabled before the setting is turned on.
    with Session(engine) as s, s.begin():
        from db.models import Message as Msg
        s.add(Fan(account_id=acct, fan_id=4000)); s.flush()
        s.add(Msg(account_id=acct, fan_id=4000, message_id=1, direction="in", body="Synthetic early", created_at=utcnow() + timedelta(seconds=1)))
    assert store.admit_new_conversation(TENANT, acct, 4000) is False
    before = fingerprint(store, acct)
    out = set_future(store, acct, True, "f1", fp=before)
    assert out["enabled"] is True and status(store, acct)["futureConversations"]["enabled"] is True
    assert set_future(store, acct, True, "f1", fp=before) == out
    # A new inbound conversation that begins after the setting is enabled; an older one never is.
    with Session(engine) as s, s.begin():
        s.add(Fan(account_id=acct, fan_id=4001))
        s.flush()
        from db.models import Message
        s.add(Message(account_id=acct, fan_id=4001, message_id=1, direction="in", body="Synthetic hello", created_at=utcnow() + timedelta(seconds=1)))
        s.add(Fan(account_id=acct, fan_id=4002))
        s.flush()
        s.add(Message(account_id=acct, fan_id=4002, message_id=1, direction="in", body="Older", created_at=utcnow() - timedelta(days=1)))
    assert store.admit_new_conversation(TENANT, acct, 4001) is True
    assert store.admit_new_conversation(TENANT, acct, 4002) is False
    with Session(engine) as s:
        assert s.get(OfConversationState, (acct, 4001)).mode == "automated"
        assert s.get(OfConversationState, (acct, 4002)) is None
    # Reconnecting (pause here) clears the setting; re-arming is an explicit act.
    with Session(engine) as s:
        rev = s.get(OfProviderConnection, acct).revision
    change(store, acct, "pause", rev, key("pause"))
    with Session(engine) as s:
        row = s.get(OfProviderConnection, acct)
        assert row.automate_new_conversations is False and row.automation_starts_at is None
    assert set_future(store, acct, False, "f2")["enabled"] is False


def set_future(store, acct, enabled, label, fp=None):
    return commands.set_future_conversations(store, TENANT, acct, enabled=enabled, command_key=key(label), actor=ACTOR,
                                             reason="Future conversations", expected_fingerprint=fp or fingerprint(store, acct),
                                             credential_reader=reader)


def test_activation_refuses_to_arm_future_conversations(account):
    store, acct, _ = account
    with Session(store.engine) as s:
        rev = s.get(OfProviderConnection, acct).revision
    with pytest.raises(Refused):
        change(store, acct, "activate", rev, key("a"), {**LIMITS, "automateNewConversations": True})


# ── 5: operating limits keep consumption ─────────────────────────────────────────────────────────────────────

def test_operating_limits_never_reset_usage_and_are_audited(account):
    store, acct, engine = account
    activate(store, acct, limit=7)
    with Session(engine) as s, s.begin():
        s.get(OfDeliveryPolicy, acct).used_units = 5
    def limits(limit, label, throttle=30, fp=None):
        return commands.set_operating_limits(store, TENANT, acct, limit_units=limit, throttle_seconds=throttle, media_enabled=True,
                                             command_key=key(label), actor=ACTOR, reason="Commercial limit",
                                             expected_fingerprint=fp or fingerprint(store, acct))
    with pytest.raises(Refused, match="used allowance"):
        limits(4, "l0")
    before = fingerprint(store, acct)
    done = limits(900, "l1", fp=before)
    assert done["usedUnits"] == 5 and done["limitUnits"] == 900
    assert limits(900, "l1", fp=before) == done           # the same command (same decision) is replay safe
    with pytest.raises(Refused):                          # a stale decision cannot be replayed under a new key
        limits(800, "l1b", fp=before)
    with Session(engine) as s:
        policy = s.get(OfDeliveryPolicy, acct)
        assert (policy.used_units, policy.limit_units, policy.throttle_seconds, policy.media_enabled) == (5, 900, 30, True)
        assert s.scalar(select(func.count()).select_from(OfDeliveryPolicyChange).where(OfDeliveryPolicyChange.account_id == acct)) >= 2
    with pytest.raises(Refused):
        limits(10001, "l2")


# ── permissions ──────────────────────────────────────────────────────────────────────────────────────────────

AGENCY_ACTOR = "agency-user:11111111-1111-4111-8111-111111111111:22222222-2222-4222-8222-222222222222"


def test_read_only_agency_member_cannot_run_any_activation_command(account):
    store, acct, _ = account
    activate(store, acct)
    fp = fingerprint(store, acct)
    scope = AgencyScope(("creator-new",), AGENCY_ACTOR, can_manage=False)
    token = current_scope.set(scope)
    try:
        for call in (lambda: commands.enable_existing(store, TENANT, acct, command_key=key("p1"), actor=AGENCY_ACTOR, reason="x",
                                                      expected_fingerprint=fp, credential_reader=reader),
                     lambda: commands.set_future_conversations(store, TENANT, acct, enabled=True, command_key=key("p2"), actor=AGENCY_ACTOR,
                                                               reason="x", expected_fingerprint=fp, credential_reader=reader),
                     lambda: commands.set_operating_limits(store, TENANT, acct, limit_units=20, throttle_seconds=20, media_enabled=False,
                                                           command_key=key("p3"), actor=AGENCY_ACTOR, reason="x", expected_fingerprint=fp)):
            with pytest.raises(Refused):
                call()
    finally:
        current_scope.reset(token)
    with Session(store.engine) as s:
        assert s.get(OfProviderConnection, acct).automate_new_conversations is False
        assert s.get(OfDeliveryPolicy, acct).limit_units == 7


def test_manager_scoped_to_another_creator_cannot_touch_the_account(account):
    store, acct, _ = account
    activate(store, acct)
    scope = AgencyScope(("33333333-3333-4333-8333-333333333333",), AGENCY_ACTOR, can_manage=True)
    token = current_scope.set(scope)
    try:
        with pytest.raises(Refused):
            commands.set_future_conversations(store, TENANT, acct, enabled=True, command_key=key("o"), actor=AGENCY_ACTOR, reason="x",
                                              expected_fingerprint="0" * 64, credential_reader=reader)
    finally:
        current_scope.reset(token)


# ── 6: human escalation is visible ───────────────────────────────────────────────────────────────────────────

def test_fan_asking_for_a_person_is_counted_listed_and_flagged_in_the_summary(account):
    store, acct, engine = account
    add_fan(engine, acct, 5001, mode="automated")
    add_fan(engine, acct, 5002)
    identity_pause(store, acct, 5001)
    with Session(engine) as s:
        counts = conversation_counts(s, acct)
    assert counts["needsPerson"] == 1 and status(store, acct)["escalations"]["recent"][0]["fanId"] == "5001"
    row = ConversationRow(account_id=acct, fan_id=5001, last_message_id=1, last_message_at=None, last_message_preview="are you real?",
                          unread_count=1, is_priority=False, is_pinned=False, fan_username="u", fan_display_name="U", fan_avatar_url=None)
    paused = StateRow(mode="paused", revision=1, takeover_state="none", unresolved_attempt_id=None, requires_attention=True,
                      updated_at=None, asked_for_person=True)
    plain = StateRow(mode="paused", revision=0, takeover_state="none", unresolved_attempt_id=None, requires_attention=True, updated_at=None)
    flagged = conversation_summary(conversation=row, state=paused, last_delivery="none", anchor=None)
    assert "asked_for_person" in flagged["attentionReasons"] and flagged["requiresAttention"] is True
    assert "asked_for_person" not in conversation_summary(conversation=row, state=plain, last_delivery="none", anchor=None)["attentionReasons"]
    # Once a human takes over and hands back, the revision moves on and the flag clears by itself.
    store.control(TENANT, acct, 5001, mode="human_controlled", expected_revision=1, command_key=key("tk"), actor=ACTOR,
                  reason="Human answers", action="request_takeover")
    with Session(engine) as s:
        assert conversation_counts(s, acct)["needsPerson"] == 0


# ── HTTP: the dashboard-facing contract ──────────────────────────────────────────────────────────────────────

def client(store, monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    for purpose, token in (("CONTROL", "control-synthetic"), ("API", "read-synthetic")):
        monkeypatch.setenv("ONLYFANS_" + purpose + "_TOKEN", token)
        monkeypatch.setenv("ONLYFANS_" + purpose + "_TENANT", TENANT)
    app = FastAPI()
    app.state.control_store = store
    app.state.personality_credential_status_reader = reader
    app.add_middleware(AgencyScopeMiddleware)
    for router in (read_router, control_router):
        app.include_router(router)
    return TestClient(app)


def test_http_read_and_commands_return_codes_the_dashboard_can_render(account, monkeypatch):
    from onlyfans_read.contract import SCHEMA_VERSION
    store, acct, engine = account
    http = client(store, monkeypatch)
    read = {"Authorization": "Bearer read-synthetic", "X-OnlyFans-Schema-Version": SCHEMA_VERSION}
    control = {"Authorization": "Bearer control-synthetic", "X-OnlyFans-Control-Version": "of-controls.v1"}
    body = http.get(f"/onlyfans/api/v1/accounts/{acct}/activation", headers=read)
    assert body.status_code == 200, body.text
    data = body.json()["activation"]
    assert set(data["stages"]) == {"connection", "model", "runtime", "replies"} and data["bulkEnable"]["counts"]["total"] == 0
    payload = {"commandKey": key("h1"), "actor": ACTOR, "reason": "Activate", "expectedFingerprint": data["fingerprint"]}
    blocked = http.post(f"/onlyfans/control/v1/accounts/{acct}/activation/enable-existing", headers=control, json=payload)
    assert blocked.status_code == 409 and blocked.json()["code"] == "prerequisites_unmet" and "account_not_activated" in blocked.json()["blockers"]
    activate(store, acct)
    payload["expectedFingerprint"] = fingerprint(store, acct)
    payload["commandKey"] = key("h2")
    add_fan(engine, acct, 6001)
    ok = http.post(f"/onlyfans/control/v1/accounts/{acct}/activation/enable-existing", headers=control, json=payload)
    assert ok.status_code == 200 and ok.json()["command"]["enabled"] == 1 and ok.json()["command"]["done"] is True
    assert http.post(f"/onlyfans/control/v1/accounts/{acct}/activation/nonsense", headers=control, json=payload).status_code == 404
    bad = {**payload, "commandKey": key("h3"), "extra": 1}
    assert http.post(f"/onlyfans/control/v1/accounts/{acct}/activation/enable-existing", headers=control, json=bad).status_code == 400
    assert http.post(f"/onlyfans/control/v1/accounts/{acct}/activation/enable-existing",
                     headers={**control, "Authorization": "Bearer wrong"}, json=payload).status_code == 401


# ── 1: connect then activate, only with this account's own usable credential ──────────────────────────────────

def test_activation_needs_the_accounts_own_model_credential_and_never_arms_anything_else(database, monkeypatch):
    store, acct, _ = verified(database, monkeypatch, model=None)
    try:
        with Session(store.engine) as s, s.begin():
            s.get(AccountAiConfig, acct).model = MODEL
            s.execute(OfRuntime.__table__.update().where(OfRuntime.account_id == acct).values(recovery_needed=False))
        with Session(store.engine) as s:
            rev = s.get(OfProviderConnection, acct).revision
        def attempt(label, revision):
            return commands.activate_account(store, TENANT, acct, limit_units=300, throttle_seconds=25, media_enabled=False,
                                             expected_revision=revision, command_key=key(label), actor=ACTOR, credential_reader=reader)
        with pytest.raises(commands.Blocked) as blocked:
            attempt("a0", rev)
        assert blocked.value.blockers == ["model_credential_missing"]
        with Session(store.engine) as s:
            assert s.get(OfRuntime, acct).enabled is False and s.get(OfDeliveryPolicy, acct) is None
        revision = set_model_credential(store, TENANT, acct, model=MODEL_CRED, expected_revision=rev, command_key=key("mc"), actor=ACTOR)
        done = attempt("a1", revision)
        assert done["recorded"] is True and attempt("a1", revision)["revision"] == done["revision"]
        with Session(store.engine) as s:
            policy = s.get(OfDeliveryPolicy, acct)
            assert s.get(OfRuntime, acct).enabled is True and (policy.limit_units, policy.used_units, policy.throttle_seconds) == (300, 0, 25)
            assert s.get(OfProviderConnection, acct).automate_new_conversations is False
            assert s.scalar(select(func.count()).select_from(OfConversationState).where(OfConversationState.mode == "automated")) == 0
        assert status(store, acct)["activated"] is True
        # A read-only team member cannot activate.
        token = current_scope.set(AgencyScope(("creator-new",), AGENCY_ACTOR, can_manage=False))
        try:
            with pytest.raises(Refused):
                commands.activate_account(store, TENANT, acct, limit_units=9, throttle_seconds=9, media_enabled=False, expected_revision=done["revision"],
                                          command_key=key("a2"), actor=AGENCY_ACTOR, credential_reader=reader)
        finally:
            current_scope.reset(token)
    finally:
        store.close()
