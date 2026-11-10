"""Held deletions for messages an OUTSIDE writer sent: proven from retained evidence, or they stay held.

Reproduces the 2026-11-10 pilot blocker: 55 messages.deleted projections held as deletion_mapping_missing_or_ambiguous
(54 sent by an outside tool to non-pilot fans, digest-only; one staff send to a pilot fan) stopped both fans.
"""
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from conversation_engine import operator_v2
from db.models import Message, OfConversationState
from db.onlyfans_fans_intake_models import OfFansEvidence, OfFansProjection
from fans_api_adapter.events import FansEventIntake
from onlyfans_runtime.store import utcnow
from test_engine_real_worker_pg import FAN2, PROVIDER
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN
from test_plan_c_events_stage3_pg import K, rig, sent_event, ev, inbound  # noqa: F401
from test_plan_c_scope_pg import ACTOR
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_engine_real_worker_pg import world  # noqa: F401
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401

OUTSIDE = 999000111


def held_deletions(rig):
    return [i for i in rig.items('held_projection')]


def proof(rig, key, mid=None, fan=OUTSIDE, queued=True, account=PROVIDER):
    body = sent_event(mid, fan, queued=queued)
    body['account_id'] = account
    return {'eventKey': key, 'body': body}


def outside_send_then_delete(rig, mid=88001, fan=OUTSIDE):
    rig.post(sent_event(mid, fan, queued=True), key=f'sent-{mid}')           # digest-only: outside the pilot
    rig.post(ev('messages.deleted', {'id': mid}), key=f'del-{mid}')
    rig.ticks(1)


def test_outside_fan_deletion_stays_held_without_proof_and_blocks_both_fans(rig):
    outside_send_then_delete(rig)
    assert rig.block(FAN) == rig.block(FAN2) == 'pilot_evidence_pending'
    [item] = held_deletions(rig)
    with pytest.raises(operator_v2.BadRequest):
        rig.reconcile(item, 900, sendProof='not-a-proof')


def test_outside_fan_deletion_is_resolved_by_retained_send_proof_only(rig):
    outside_send_then_delete(rig)
    [item] = held_deletions(rig)
    digest_key = 'sent-88001'
    p = proof(rig, digest_key, 88001)
    ok = rig.reconcile(item, 901, sendProof=p)
    assert ok['resolved'] and ok['projection'] == 'ignored' and ok['proof']['fan'] == OUTSIDE
    assert rig.reconcile(item, 901, sendProof=p)['replayed']
    with Session(rig.w.database[1]) as s:
        p = s.get(OfFansProjection, (ACCOUNT, 1, 'del-88001'))
        assert (p.status, p.detail) == ('ignored', 'deletion_outside_pilot_proven')
        assert s.scalar(select(Message).where(Message.message_id == 88001)) is None   # nothing invented
    assert rig.block(FAN) is None and rig.block(FAN2) is None
    rig.fans_in(base=88100)
    rig.ticks()
    assert len(rig.w.fans.posts) == 2


@pytest.mark.parametrize('case', ['wrong_message', 'pilot_fan', 'wrong_account', 'unknown_key', 'not_a_send',
                                  'held_digest', 'native_row', 'no_fan'])
def test_unproven_or_conflicting_proof_keeps_the_deletion_held(rig, case):
    outside_send_then_delete(rig)
    if case == 'held_digest':
        rig.post(ev('chat_queue.updated', {'id': 5, 'total': -1}), key='sent-held-digest')
    if case == 'native_row':
        rig.post(inbound(88001, 'native', fan=FAN), key='native-88001')
        rig.ticks(1)
    [item] = [i for i in held_deletions(rig) if i['eventKey'] == 'del-88001']
    bad = {'wrong_message': proof(rig, 'sent-88001', 88002), 'pilot_fan': proof(rig, 'sent-88001', 88001, fan=FAN),
           'wrong_account': proof(rig, 'sent-88001', 88001, account='acct_other'),
           'unknown_key': proof(rig, 'sent-unknown', 88001),
           'not_a_send': {'eventKey': 'sent-88001', 'body': ev('messages.received', {'id': 88001})},
           'held_digest': proof(rig, 'sent-held-digest', 88001),
           'native_row': proof(rig, 'sent-88001', 88001),
           'no_fan': {'eventKey': 'sent-88001', 'body': ev('messages.sent', {'id': 88001, 'createdAt': 'x'})}}[case]
    with pytest.raises((operator_v2.Conflict, operator_v2.BadRequest)):
        rig.reconcile(item, 910, sendProof=bad)
    with Session(rig.w.database[1]) as s:
        assert s.get(OfFansProjection, (ACCOUNT, 1, 'del-88001')).status == 'held'


def test_staff_send_to_pilot_fan_then_deletion_is_proven_through_takeover_not_the_outside_proof(rig):
    rig.post(sent_event(88500, FAN, queued=False), key='staff-88500')
    rig.post(ev('messages.deleted', {'id': 88500}), key='del-88500')
    rig.ticks(1)
    [item] = [i for i in held_deletions(rig) if i['eventKey'] == 'del-88500']
    with pytest.raises(operator_v2.Conflict):                      # pilot fan: not the outside-fan proof
        rig.reconcile(item, 920, sendProof=proof(rig, 'staff-88500', 88500, fan=FAN, queued=False))
    # Before takeover is applied the evidence is only pending: the deletion cannot be proven and re-drive stays held.
    with Session(rig.w.database[1]) as s:
        row = s.scalar(select(OfFansEvidence).where(OfFansEvidence.kind == 'human_sent', OfFansEvidence.message_id == 88500))
        assert row.status == 'pending'
        row.created_at = utcnow() - timedelta(minutes=5)
        s.commit()
    redriven = rig.reconcile(item, 921)
    assert redriven['projection'] == 'held'
    FansEventIntake(rig.w.store).classify_sent(ACCOUNT, FAN)
    rig.ticks(2)
    with Session(rig.w.database[1]) as s:
        row = s.scalar(select(OfFansEvidence).where(OfFansEvidence.kind == 'human_sent', OfFansEvidence.message_id == 88500))
        assert row.status == 'applied'
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode != 'automated'
        assert s.get(Message, (ACCOUNT, FAN, 88500)).direction == 'out'
    [item] = [i for i in held_deletions(rig) if i['eventKey'] == 'del-88500']
    done = rig.reconcile(item, 922)
    assert done['projection'] == 'projected', done
    with Session(rig.w.database[1]) as s:
        assert s.get(Message, (ACCOUNT, FAN, 88500)).is_unsent is True
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode != 'automated', 'takeover is not undone by a deletion'


def test_deletion_of_a_message_not_proven_outside_writer_stays_held(rig):
    # Out-direction row without engine intent AND without an applied matching human-send receipt.
    rig.post(sent_event(88600, FAN, queued=False), key='staff-88600')
    rig.ticks(1)
    with Session(rig.w.database[1]) as s:
        from db.models import Message as M
        s.add(M(account_id=ACCOUNT, fan_id=FAN2, message_id=88600, direction='out', body='x', created_at=utcnow()))
        s.commit()
    rig.post(ev('messages.deleted', {'id': 88600}), key='del-88600')
    rig.ticks(1)
    with Session(rig.w.database[1]) as s:
        p = s.get(OfFansProjection, (ACCOUNT, 1, 'del-88600'))
        assert p.status == 'held'
