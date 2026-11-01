"""R1 on real PostgreSQL: the shared conversation-hold policy through the real worker, planner and TextAuthority.

Synthetic model and wire only (SyntheticNativeModel / SyntheticTextTransport); the guard under test is never mocked.
Fan ids and account ids are synthetic. Message texts are modelled on the failure register, not copied from it.
"""
from datetime import timedelta
import json
import threading

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, due, event, seed
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured, unthrottle
from db.models import AccountAiConfig, Fan, Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from onlyfans_runtime.campaigns import CampaignService
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.native_worker import NativeWorker, WorkerAccount
from onlyfans_runtime.store import Refused, Store, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport, TextAuthority



class _Lazy:
    """The R1 modules are imported on first use, so this file still imports (and its behaviour tests still run, and
    fail on their assertions) against the unpatched base commit, which has neither module."""
    def __getattr__(self, name):
        import onlyfans_runtime.operator_cases as module
        return getattr(module, name)


operator_cases = _Lazy()


def OfOperatorCase():  # noqa: N802 - mirrors the model name; resolved lazily
    from db.onlyfans_case_models import OfOperatorCase as model
    return model


REPLY = "Thanks for telling me, how was your day?"


def start(database, monkeypatch, body, *, safety=None, fan=FAN):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setenv("DATABASE_URL", database[0])
    store, authority = configured(database, mode="automated")
    config = None if safety is None else json.dumps({"conversation_safety_mode": safety})
    with Session(database[1]) as s, s.begin():
        s.add(AccountAiConfig(account_id=ACCOUNT, model="grok-4-1-fast-non-reasoning",
            persona="Synthetic Creator enjoys hiking and coffee.", daily_cost_cap_cents=100,
            ai_chatter_config_json=config))
    for n, line in enumerate(body if isinstance(body, list) else [body]):
        store.record(TENANT, ACCOUNT, event(mid=n + 1, fan=fan, body=line))
    store.drain(TENANT, ACCOUNT)
    due(database[1])
    return store, authority


def runtime(store, model=None, wire=None):
    model = model or SyntheticNativeModel(body=REPLY, reported_cost_millicents=7)
    wire = wire or SyntheticTextTransport(ACCOUNT)
    return NativeWorker(store, TENANT, (WorkerAccount(ACCOUNT, model, wire),)), model, wire


def state(engine, fan=FAN):
    with Session(engine) as s:
        row = s.get(OfConversationState, (ACCOUNT, fan))
        return row.mode, row.takeover_state, row.requires_attention, row.revision


def cases(engine, *, status=None):
    with Session(engine) as s:
        M = OfOperatorCase()
        q = select(M)
        if status:
            q = q.where(M.status.in_(status))
        return [(c.category, c.status, c.scope) for c in s.scalars(q.order_by(M.created_at)).all()]


def outbound(engine):
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(Message).where(Message.direction == "out"))


# ── shadow is the default and enforces nothing ────────────────────────────────────────────────────────────────────
def test_default_shadow_records_the_verdict_and_keeps_the_existing_behaviour(database, monkeypatch):
    store, _ = start(database, monkeypatch, "Ok we are done stop talking")
    worker, model, wire = runtime(store)
    worker.step()
    assert len(model.calls) == 1 and len(wire.calls) == 1            # unchanged behaviour: a reply is generated and sent
    assert state(database[1])[0] == "automated"
    assert cases(database[1]) == [("stop_contact", "shadow", "all")]  # ...but the verdict is on record
    assert cases(database[1], status=("open", "acknowledged")) == []
    store.close()


# ── the failures, live ────────────────────────────────────────────────────────────────────────────────────────────
def test_explicit_stop_is_enforced_before_any_reply_survives_restart_and_feature_disablement(database, monkeypatch):
    store, _ = start(database, monkeypatch, "Ok we are done stop talking", safety="live")
    worker, model, wire = runtime(store)
    worker.step()
    assert model.calls == [] and wire.calls == [] and outbound(database[1]) == 0
    mode, takeover, attention, _revision = state(database[1])
    assert (mode, takeover, attention) == ("no_contact", "none", True)
    assert cases(database[1], status=("open",)) == [("stop_contact", "open", "all")]
    # restart: a brand-new store, worker, model and wire
    store.close()
    fresh = Store(database[0])
    worker, model, wire = runtime(fresh)
    for _ in range(3):
        worker.step()
    assert model.calls == [] and wire.calls == []
    # feature disablement: detectors off, delivery media off; the durable hold is untouched
    with Session(database[1]) as s, s.begin():
        s.get(AccountAiConfig, ACCOUNT).ai_chatter_config_json = json.dumps({"conversation_safety_mode": "off"})
    worker.step()
    assert model.calls == [] and wire.calls == []
    assert state(database[1])[0] == "no_contact"
    # a manual courtesy message is NOT a bypass of a hard no-contact restriction
    with pytest.raises(Refused, match="contact_blocked"):
        TextAuthority(fresh).prepare(TENANT, ACCOUNT, FAN, command_key="courtesy", expected_revision=state(database[1])[3],
                                     body="Sorry to hear that, take care", origin="manual", actor="synthetic-operator")
    # a plain resume cannot lift it either; only recorded renewed consent (audited) does, and it closes the case
    with pytest.raises(Refused):
        fresh.control(TENANT, ACCOUNT, FAN, mode="automated", expected_revision=state(database[1])[3],
                      command_key="resume-1", actor="synthetic-operator", reason="Resume")
    fresh.control(TENANT, ACCOUNT, FAN, mode="paused", expected_revision=state(database[1])[3],
                  command_key="consent-1", actor="synthetic-operator", reason="Fan asked to talk again", consent_ref="synthetic-consent-1")
    assert cases(database[1], status=("open", "acknowledged")) == []
    assert [c[1] for c in cases(database[1])] == ["resolved"]
    fresh.close()


@pytest.mark.parametrize("line", ["Trying to drown myself", "honestly i just want to end my life"])
def test_distress_stops_commercial_activity_and_needs_a_person(database, monkeypatch, line):
    store, authority = start(database, monkeypatch, line, safety="live")
    worker, model, wire = runtime(store)
    worker.step()
    assert model.calls == [] and wire.calls == []
    assert state(database[1])[0] == "paused"
    [(category, status, scope)] = cases(database[1])
    assert (category, status, scope) == ("distress", "open", "all")
    with Session(database[1]) as s:
        case = s.scalar(select(OfOperatorCase()))
        assert case.due_at is not None and case.due_at <= utcnow() + timedelta(minutes=16)   # urgent clock
        assert json.loads(case.evidence_json)["rule"] == "distress-phrase" and case.reason == "distress-phrase"
        case_id = case.case_id
    # promos continuing is exactly the failure: a due campaign skips the recipient and queues nothing
    service = CampaignService(store)
    service.create(TENANT, ACCOUNT, command_key="a" * 64, actor="synthetic-operator", name="Synthetic promo",
                   body="A synthetic promotion body.", audience={"kind": "all_known"})
    assert service.queue_due(TENANT, ACCOUNT) is None
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
    # a resume (or hand-back) cannot lift a distress case
    rev = state(database[1])[3]
    with pytest.raises(Refused, match="operator case holds resumption"):
        store.control(TENANT, ACCOUNT, FAN, mode="automated", expected_revision=rev, command_key="r1",
                      actor="synthetic-operator", reason="Resume", action="resume_automation")
    # the authorised route: takeover -> manual reply (still works) -> acknowledge -> resolve -> hand back
    store.control(TENANT, ACCOUNT, FAN, mode="human_controlled", expected_revision=rev, command_key="t1",
                  actor="synthetic-operator", reason="Take over", action="request_takeover")
    unthrottle(database[1])
    rev = state(database[1])[3]
    intent = authority.prepare(TENANT, ACCOUNT, FAN, command_key="manual-1", expected_revision=rev,
                               body="I am here, are you safe right now? Please reach out to someone close to you.",
                               origin="manual", actor="synthetic-operator")
    assert authority.dispatch(TENANT, ACCOUNT, intent, SyntheticTextTransport(ACCOUNT)) == "accepted"
    acknowledged = operator_cases.acknowledge(store, TENANT, ACCOUNT, case_id, actor="synthetic-operator")
    assert acknowledged["status"] == "acknowledged" and acknowledged["owner"] == "synthetic-operator"
    with pytest.raises(Refused, match="operator case holds resumption"):
        store.control(TENANT, ACCOUNT, FAN, mode="automated", expected_revision=state(database[1])[3], command_key="r2",
                      actor="synthetic-operator", reason="Hand back", action="release_takeover")
    done = operator_cases.resolve(store, TENANT, ACCOUNT, case_id, actor="synthetic-operator", resolution="Spoke with the fan; checked in.")
    assert done["status"] == "resolved" and done["resolution"].startswith("Spoke")
    store.control(TENANT, ACCOUNT, FAN, mode="automated", expected_revision=state(database[1])[3], command_key="r3",
                  actor="synthetic-operator", reason="Hand back", action="release_takeover")
    assert state(database[1])[0] == "automated" and state(database[1])[2] is False
    store.close()


@pytest.mark.parametrize("line,category", [
    ("I'm a garbage man got paid yesterday so I will give you my weekly wages instead of feeding my children", "hardship"),
    ("I paid 80 dollars and I didn't see a single nipple", "complaint"),
])
def test_commercial_scope_holds_stop_selling_but_not_the_conversation(database, monkeypatch, line, category):
    store, authority = start(database, monkeypatch, line, safety="live")
    worker, model, wire = runtime(store)
    worker.step()
    assert len(model.calls) == 1 and len(wire.calls) == 1                 # plain chat to resolve the matter may go on
    assert state(database[1])[0] == "automated"
    assert cases(database[1], status=("open",)) == [(category, "open", "commercial")]
    # ...but no proactive / commercial sender can reach this fan
    service = CampaignService(store)
    created = service.create(TENANT, ACCOUNT, command_key="b" * 64, actor="synthetic-operator", name="Synthetic promo",
                             body="A synthetic promotion body.", audience={"kind": "all_known"})
    unthrottle(database[1])
    assert service.queue_due(TENANT, ACCOUNT) is None
    detail = service.detail(TENANT, ACCOUNT, created["campaignId"])
    assert detail["campaign"]["counts"]["skipped"] == 1
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent).where(OfTextIntent.origin == "campaign")) == 0
    store.close()


# ── nearby legitimate cases through the same worker, live ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("line", [
    "stop teasing me",
    "stop it 😂",
    "I don't want u to stop",
    "after rent I have 50 left, can u do 30 for the bundle",
    "Yes im in pain from not cumming😫",
    "Till you're passed out on the bed naked ?",
])
def test_legitimate_neighbours_get_their_normal_reply_and_no_hold(database, monkeypatch, line):
    store, _ = start(database, monkeypatch, line, safety="live")
    worker, model, wire = runtime(store)
    worker.step()
    assert len(model.calls) == 1 and len(wire.calls) == 1
    assert state(database[1])[0] == "automated"
    assert cases(database[1], status=("open", "acknowledged")) == []
    store.close()


def test_ambiguous_consent_concern_stops_the_affected_activity_and_waits_for_clarification(database, monkeypatch):
    store, _ = start(database, monkeypatch, "please stop", safety="live")
    worker, model, wire = runtime(store)
    worker.step()
    assert model.calls == [] and wire.calls == []
    assert state(database[1])[0] == "paused"
    with Session(database[1]) as s:
        case = s.scalar(select(OfOperatorCase()))
        assert (case.category, case.status) == ("consent", "open") and json.loads(case.evidence_json)["ambiguous"] is True
    store.close()


def test_identity_question_is_held_in_every_mode_and_a_hand_back_closes_the_case(database, monkeypatch):
    store, _ = start(database, monkeypatch, "wait are you a bot?")        # no mode set: shadow default
    worker, model, wire = runtime(store)
    worker.step()
    assert model.calls == [] and wire.calls == []                          # the existing pause-and-flag policy, both planes
    assert state(database[1])[0] == "paused"
    assert cases(database[1], status=("open",)) == [("identity", "open", "all")]
    rev = state(database[1])[3]
    store.control(TENANT, ACCOUNT, FAN, mode="automated", expected_revision=rev, command_key="h1",
                  actor="synthetic-operator", reason="Answered by a person", action="resume_automation")
    assert cases(database[1], status=("open", "acknowledged")) == []
    store.close()


# ── cross-sender matrix with the real authority ───────────────────────────────────────────────────────────────────
def eligibility(database, authority, origin, action=None, own=None):
    with Session(database[1]) as s, s.begin():
        runtime_row = authority.store._delivery_runtime(s, TENANT, ACCOUNT)
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        st = s.get(OfConversationState, (ACCOUNT, FAN))
        fan = s.get(Fan, (ACCOUNT, FAN))
        return authority._eligibility(s, runtime_row, policy, st, fan, origin, authority._db_now(s),
                                      own_attempt_id=own, action=action)


def hold(database, *, mode="automated", takeover="none", category=None, status="open", unresolved=None, skip=None, flag=False):
    with Session(database[1]) as s, s.begin():
        row = s.get(OfConversationState, (ACCOUNT, FAN))
        row.mode, row.takeover_state, row.unresolved_attempt_id = mode, takeover, unresolved
        if category:
            from onlyfans_runtime.conversation_policy import CATEGORY_SCOPE
            operator_cases.open_case(s, ACCOUNT, FAN, category, reason="synthetic", evidence={"rule": "synthetic"}, status=status)
        if skip:
            from db.models import SkipList
            s.merge(SkipList(account_id=ACCOUNT, fan_id=FAN, reason=skip, added_at=utcnow()))
        if flag:
            from db.models import FanSafetyFlag
            s.add(FanSafetyFlag(account_id=ACCOUNT, fan_id=FAN, flag_type="suicide_risk", severity="critical", flagged_by="human"))


SENDERS = [("chat", "automated", "chat"), ("pack/upsell/contextual card", "automated", "commercial"),
           ("campaign", "campaign", None), ("engagement", "engagement", None)]


@pytest.mark.parametrize("category,commercial_only", [("stop_contact", False), ("distress", False), ("consent", False),
                                                      ("identity", False), ("external_sender", False),
                                                      ("hardship", True), ("complaint", True)])
def test_after_each_hold_no_internal_sender_may_send(database, category, commercial_only):
    store, authority = configured(database, mode="automated")
    hold(database, category=category)
    for label, origin, action in SENDERS:
        reason = eligibility(database, authority, origin, action)
        chat_allowed = commercial_only and label == "chat"
        assert (reason is None) == chat_allowed, (category, label, reason)
        if reason:
            assert reason == "case_" + category
    store.close()


@pytest.mark.parametrize("kw,reason", [
    ({"skip": "of_restricted"}, "fan_restricted"),
    ({"skip": "manual_restrict"}, "fan_restricted"),
    ({"flag": True}, "safety_hold"),
    ({"unresolved": "attempt-x"}, "unresolved_delivery"),
])
def test_durable_stops_flags_and_uncertain_delivery_hold_every_internal_sender(database, kw, reason):
    store, authority = configured(database, mode="automated")
    hold(database, **kw)
    for _label, origin, action in SENDERS:
        assert eligibility(database, authority, origin, action) == reason
    store.close()


def test_uncertain_delivery_blocks_a_second_charge_even_for_a_person_but_not_its_own_attempt(database):
    store, authority = configured(database, mode="human_controlled")
    hold(database, mode="human_controlled", takeover="effective", unresolved="attempt-1")
    assert eligibility(database, authority, "manual") == "unresolved_delivery"
    assert eligibility(database, authority, "manual", own="attempt-1") is None     # the in-flight attempt itself proceeds
    with pytest.raises(Refused, match="unresolved_delivery"):
        authority.prepare(TENANT, ACCOUNT, FAN, command_key="second-charge", expected_revision=0, body="Another try",
                          origin="manual", actor="synthetic-operator")
    store.close()


@pytest.mark.parametrize("category", ["distress", "consent", "identity", "hardship", "complaint", "external_sender"])
def test_authorised_manual_response_under_takeover_still_works(database, category):
    store, authority = configured(database, mode="human_controlled")
    hold(database, mode="human_controlled", takeover="effective", category=category)
    assert eligibility(database, authority, "manual") is None
    unthrottle(database[1])
    intent = authority.prepare(TENANT, ACCOUNT, FAN, command_key="manual-ok", expected_revision=0,
                               body="A person is answering you now.", origin="manual", actor="synthetic-operator")
    assert authority.dispatch(TENANT, ACCOUNT, intent, SyntheticTextTransport(ACCOUNT)) == "accepted"
    store.close()


def test_a_stop_request_after_planning_is_judged_again_immediately_before_the_wire(database, monkeypatch):
    store, authority = start(database, monkeypatch, "hello there", safety="live")
    # an operator campaign is queued BEFORE the fan asks us to stop
    service = CampaignService(store)
    service.create(TENANT, ACCOUNT, command_key="c" * 64, actor="synthetic-operator", name="Synthetic promo",
                   body="A synthetic promotion body.", audience={"kind": "all_known"})
    # the reactive turn is left unclaimed; the campaign is the sender under test
    with Session(database[1]) as s, s.begin():
        s.execute(text("UPDATE of_work SET due_at = CURRENT_TIMESTAMP + INTERVAL '1 day'"))
    unthrottle(database[1])
    identity = service.queue_due(TENANT, ACCOUNT)
    assert identity is not None
    store.record(TENANT, ACCOUNT, event(mid=50, body="So yeah stop, you still haven't blocked me"))
    store.drain(TENANT, ACCOUNT)
    transport = SyntheticTextTransport(ACCOUNT)
    outcome = authority.dispatch(TENANT, ACCOUNT, identity, transport)
    assert transport.calls == [] and outcome != "accepted"
    assert state(database[1])[0] == "no_contact"
    assert cases(database[1], status=("open",))[0][0] == "stop_contact"
    store.close()


# ── external senders: observed into the same durable state ────────────────────────────────────────────────────────
def external_out(engine, minutes_ago=5):
    with Session(engine) as s, s.begin():
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=777, direction="out", body="typed in another tool",
                      created_at=utcnow() - timedelta(minutes=minutes_ago)))


def test_external_message_is_observed_shadow_then_live_and_cancels_obsolete_internal_work(database, monkeypatch):
    store, authority = start(database, monkeypatch, "hello there")
    external_out(database[1])
    assert operator_cases.scan(store, TENANT, ACCOUNT, FAN) == []
    assert cases(database[1]) == [("external_sender", "shadow", "all")] and state(database[1])[0] == "automated"
    # queued internal work exists (queued under shadow) when the external message is observed under live
    service = CampaignService(store)
    service.create(TENANT, ACCOUNT, command_key="d" * 64, actor="synthetic-operator", name="Synthetic promo",
                   body="A synthetic promotion body.", audience={"kind": "all_known"})
    with Session(database[1]) as s, s.begin():
        s.execute(text("UPDATE of_work SET due_at = CURRENT_TIMESTAMP + INTERVAL '1 day'"))
    unthrottle(database[1])
    identity = service.queue_due(TENANT, ACCOUNT)
    assert identity is not None
    with Session(database[1]) as s, s.begin():
        s.get(AccountAiConfig, ACCOUNT).ai_chatter_config_json = json.dumps({"conversation_safety_mode": "live"})
    assert operator_cases.scan(store, TENANT, ACCOUNT, FAN) == ["external_sender"]
    assert state(database[1])[0] == "paused"
    transport = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, identity, transport) != "accepted" and transport.calls == []
    with Session(database[1]) as s:
        assert s.get(OfTextIntent, (ACCOUNT, identity)).status in {"cancelled", "refused"}
    # our own accepted message is never mistaken for an external one
    store.close()


def test_our_own_projected_send_is_not_an_external_message(database, monkeypatch):
    store, authority = start(database, monkeypatch, "hello there", safety="live")
    worker, model, wire = runtime(store)
    worker.step()
    assert len(wire.calls) == 1 and outbound(database[1]) == 1
    with Session(database[1]) as s, s.begin():
        s.execute(text("UPDATE messages SET created_at = created_at - INTERVAL '10 minutes' WHERE direction='out'"))
    assert operator_cases.scan(store, TENANT, ACCOUNT, FAN) == []
    assert state(database[1])[0] == "automated"
    store.close()


# ── two workers / two send paths cannot both own a turn ───────────────────────────────────────────────────────────
def test_two_workers_cannot_both_win_one_turn(database, monkeypatch):
    store, _ = start(database, monkeypatch, "what are you up to tonight?")
    other = Store(database[0])
    first, model_a, wire_a = runtime(store)
    second, model_b, wire_b = runtime(other)
    barrier = threading.Barrier(2)
    errors = []

    def go(worker):
        try:
            barrier.wait(10)
            worker.step()
        except Exception as exc:        # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=go, args=(w,)) for w in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(model_a.calls) + len(model_b.calls) == 1, errors
    assert len(wire_a.calls) + len(wire_b.calls) == 1
    assert outbound(database[1]) == 1
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 1
    other.close()
    store.close()


def test_a_planned_internal_reply_cannot_race_a_person_who_takes_over(database, monkeypatch):
    store, authority = start(database, monkeypatch, "what are you up to tonight?")
    from onlyfans_runtime.native_planning import NativeTextPlanner
    planner = NativeTextPlanner(store)
    claim = store.claim(TENANT)
    plan = planner.prepare(TENANT, claim, SyntheticNativeModel(body=REPLY))
    identity = planner.queue(TENANT, ACCOUNT, plan)
    store.control(TENANT, ACCOUNT, FAN, mode="human_controlled", expected_revision=state(database[1])[3], command_key="tk",
                  actor="synthetic-operator", reason="Take over", action="request_takeover")
    transport = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, identity, transport) != "accepted"
    assert transport.calls == []                       # the second send path lost: the person owns the turn
    unthrottle(database[1])
    intent = authority.prepare(TENANT, ACCOUNT, FAN, command_key="manual-turn", expected_revision=state(database[1])[3],
                               body="Hi, it's me typing now.", origin="manual", actor="synthetic-operator")
    assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == "accepted"
    assert len(transport.calls) == 1
    store.close()


# ── the operator case record ──────────────────────────────────────────────────────────────────────────────────────
def test_case_record_carries_owner_due_overdue_escalation(database):
    store, _ = configured(database, mode="automated")
    with Session(database[1]) as s, s.begin():
        case, _new = operator_cases.open_case(s, ACCOUNT, FAN, "distress", reason="distress-phrase",
                                              evidence={"rule": "distress-phrase", "excerpt": "synthetic"})
        case_id = case.case_id
        # opening it again for the same fan and category is the same case, not a second one
        again, created = operator_cases.open_case(s, ACCOUNT, FAN, "distress", reason="distress-phrase", evidence={})
        assert again.case_id == case_id and created is False
    with Session(database[1]) as s:
        listing = operator_cases.list_cases(s, ACCOUNT)
        assert [c["caseId"] for c in listing] == [case_id] and listing[0]["overdue"] is False
        assert listing[0]["dueAt"] is not None and listing[0]["status"] == "open" and listing[0]["escalationLevel"] == 0
    with Session(database[1]) as s, s.begin():
        s.get(OfOperatorCase(), (ACCOUNT, case_id)).due_at = utcnow() - timedelta(minutes=40)
    with Session(database[1]) as s:
        assert operator_cases.list_cases(s, ACCOUNT)[0]["overdue"] is True
    assert operator_cases.sweep_overdue(store, TENANT, ACCOUNT) == 1
    assert operator_cases.sweep_overdue(store, TENANT, ACCOUNT) == 0                       # idempotent within an interval
    with Session(database[1]) as s:
        assert operator_cases.list_cases(s, ACCOUNT)[0]["escalationLevel"] >= 2            # 40 min late of a 15 min clock
    with pytest.raises(Refused):
        operator_cases.acknowledge(store, TENANT, ACCOUNT, "00000000-0000-0000-0000-000000000000", actor="x")
    store.close()


def test_migration_adds_the_case_table_and_upgrades_cleanly_from_the_previous_head(database):
    from alembic import command
    from db.schema import HEAD, _config, check_schema
    url, engine = database
    assert HEAD == "0108_commercial_progress"
    with engine.begin() as c:
        check_schema(c)
        command.downgrade(_config(c), "0104_contextual_item_scope")
        c.execute(text("DROP TABLE of_operator_case"))
    with engine.begin() as c:
        command.upgrade(_config(c), HEAD)
        check_schema(c)
        cols = {r[0] for r in c.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='of_operator_case'"))}
    assert {"case_id", "category", "scope", "status", "reason", "evidence_json", "owner", "due_at", "acknowledged_at",
            "resolution", "resolved_at", "escalation_level"} <= cols
