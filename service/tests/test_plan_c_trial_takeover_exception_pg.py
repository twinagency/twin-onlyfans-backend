"""Owner's temporary trial exception: an outside manual send to an approved test fan is recorded, not a takeover.

Jesse 2026-11-10: "Just because a human takes over don't block it for our test." Two test fans only; removable;
stops, queued-send holds, scope and budgets unchanged; evidence and history of the outside send are kept.
"""
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from db.models import Message, OfConversationState
from db.onlyfans_fans_intake_models import OfFansEvidence
from fans_api_adapter.events import FansEventIntake
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow
from test_engine_real_worker_pg import FAN2
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_events_stage3_pg import rig, sent_event, inbound  # noqa: F401
from test_plan_c_scope_pg import ACTOR, OTHER
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_engine_real_worker_pg import world  # noqa: F401
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401


def outside_send(rig, mid, fan, key):
    rig.post(sent_event(mid, fan, queued=False), key=key)
    with Session(rig.w.database[1]) as s:
        row = s.scalar(select(OfFansEvidence).where(OfFansEvidence.kind == 'human_sent', OfFansEvidence.message_id == mid))
        row.created_at = utcnow() - timedelta(minutes=5)
        s.commit()
    FansEventIntake(rig.w.store).classify_sent(ACCOUNT, fan)
    rig.w.store.drain(TENANT, ACCOUNT)
    with Session(rig.w.database[1]) as s:
        row = s.scalar(select(OfFansEvidence).where(OfFansEvidence.kind == 'human_sent', OfFansEvidence.message_id == mid))
        st = s.get(OfConversationState, (ACCOUNT, fan))
        return (row.status, row.reason), (st.mode, st.takeover_state), s.get(Message, (ACCOUNT, fan, mid)) is not None


def enable(rig, enabled=True):
    return pilot_scope.set_trial_takeover_exception(rig.w.store, TENANT, ACCOUNT, enabled=enabled, actor=ACTOR,
                                                    reason='owner trial exception (test)')


def test_without_the_exception_an_outside_send_still_takes_over(rig):
    ev, state, history = outside_send(rig, 97001, FAN, 'out-1')
    assert ev == ('applied', 'takeover_requested') and state[0] == 'human_controlled' and history


def test_exception_records_the_outside_send_and_keeps_the_fan_automated_then_bot_replies(rig):
    result = enable(rig)
    assert result['fans'] == sorted(result['fans']) or set(result['fans']) == {FAN, FAN2}
    assert enable(rig)['replayed']
    ev, state, history = outside_send(rig, 97002, FAN, 'out-2')
    assert ev == ('applied', 'outside_send_recorded_trial_exception')
    assert state == ('automated', 'none') and history, 'evidence + history kept, no takeover'
    assert rig.block(FAN) is None
    rig.fans_in(base=97100)
    rig.ticks()
    assert len(rig.w.fans.posts) == 2, 'the bot still answers both fans after the outside send'
    with Session(rig.w.database[1]) as s:
        audit = s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.kind == pilot_scope.TRIAL_TAKEOVER_EXCEPTION))
        assert audit is not None and audit.actor == ACTOR


def test_ended_exception_restores_takeover_and_cannot_be_reopened(rig):
    enable(rig)
    enable(rig, False)
    ev, state, _ = outside_send(rig, 97003, FAN2, 'out-3')
    assert ev == ('applied', 'takeover_requested') and state[0] == 'human_controlled'
    with pytest.raises(Refused):
        enable(rig)


def test_exception_requires_operator_permission_and_never_covers_other_fans(rig):
    with pytest.raises(Refused):
        pilot_scope.set_trial_takeover_exception(rig.w.store, TENANT, ACCOUNT, enabled=True, actor=OTHER, reason='x')
    enable(rig)
    with Session(rig.w.database[1]) as s:
        from db.onlyfans_models import OfRuntime
        binding = s.get(OfRuntime, ACCOUNT).binding_id
        assert pilot_scope.trial_takeover_exception(s, ACCOUNT, binding, FAN)
        assert not pilot_scope.trial_takeover_exception(s, ACCOUNT, binding, 999000111)
        assert not pilot_scope.trial_takeover_exception(s, ACCOUNT, 'other-binding', FAN)


def test_exception_does_not_weaken_stops_or_queued_send_holds(rig):
    enable(rig)
    rig.post(inbound(97200, 'please stop texting me', fan=FAN2), key='stop-fan2')
    rig.post(sent_event(97201, FAN, queued=True), key='queued-fan')
    rig.ticks()
    with Session(rig.w.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN2)).mode == 'no_contact'
        held = s.scalar(select(OfFansEvidence).where(OfFansEvidence.message_id == 97201))
        assert (held.status, held.reason) == ('held', 'queued_send_to_engine_owned_fan')
    assert rig.block(FAN) == 'pilot_evidence_pending'
    assert rig.w.fans.posts == []
