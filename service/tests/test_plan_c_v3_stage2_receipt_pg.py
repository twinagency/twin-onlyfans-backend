"""Stage 2B: a saved accepted receipt is materialized by normal bounded worker recovery, never by a second POST."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineRequest
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import TextAuthority
from test_plan_c_supervised_pg import supervised, stage, approve  # noqa: F401
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401
from test_engine_real_worker_pg import world, FAN2  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT, FAN


def snapshot(world):
    with Session(world.database[1]) as s:
        intent = s.scalar(select(OfTextIntent))
        decision = s.scalar(select(OfEngineDecision))
        request = s.scalar(select(OfEngineRequest))
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        return {'intent': intent.status, 'projected': intent.projected_at is not None, 'reason': intent.reason,
                'decision': decision.status, 'request': request.status, 'next_step': request.next_step,
                'unresolved': state.unresolved_attempt_id, 'attention': state.requires_attention,
                'input_revision': state.input_revision,
                'messages': len(list(s.scalars(select(Message).where(Message.direction == 'out')))),
                'used_units': s.get(OfDeliveryPolicy, ACCOUNT).used_units}


def crash_after_accepted(world, pilot, monkeypatch, mid):
    result = stage(pilot, mid)
    approve(world, result)
    authority = pilot.worker.authority
    original = authority.materialize

    def crash(*args, **kwargs):
        raise RuntimeError('synthetic crash after accepted receipt committed')
    monkeypatch.setattr(authority, 'materialize', crash)
    with pytest.raises(RuntimeError, match='synthetic crash'):
        pilot.worker.send_approved(TENANT, ACCOUNT, result['decisionId'])
    monkeypatch.setattr(authority, 'materialize', original)
    assert len(world.fans.posts) == 1
    before = snapshot(world)
    assert before['intent'] == 'accepted' and not before['projected'] and before['messages'] == 0
    return result


def test_restart_ticks_materialize_one_message_finish_the_request_and_post_nothing(supervised, monkeypatch):
    world, env, model, seen, pilot = supervised
    crash_after_accepted(world, pilot, monkeypatch, 99201)
    revision_before = snapshot(world)['input_revision']
    restarted = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    for _ in range(4):
        restarted.tick()
        assert len(world.fans.posts) == 1, 'recovery must never POST'
    final = snapshot(world)
    assert final['projected'] and final['messages'] == 1
    assert final['decision'] == 'completed' and final['request'] == 'completed' and final['next_step'] == 'none'
    assert final['unresolved'] is None and final['used_units'] == 1
    assert final['input_revision'] == revision_before + 1, 'exactly one canonical outbound message was projected'


def test_repair_is_idempotent_and_safe_under_concurrent_workers(supervised, monkeypatch):
    world, env, model, seen, pilot = supervised
    crash_after_accepted(world, pilot, monkeypatch, 99202)
    revision_before = snapshot(world)['input_revision']
    scope = frozenset({FAN, FAN2})

    def repair(_):
        return TextAuthority(world.store).repair(TENANT, ACCOUNT, limit=32, synthetic_engine_only=True, allowed_fans=scope)
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(repair, range(4)))
    final = snapshot(world)
    assert final['messages'] == 1 and final['projected'] and final['input_revision'] == revision_before + 1
    assert repair(0) == 0
    assert snapshot(world) == final
    assert len(world.fans.posts) == 1


def test_recovery_is_scoped_to_the_allowed_fans(supervised, monkeypatch):
    world, env, model, seen, pilot = supervised
    crash_after_accepted(world, pilot, monkeypatch, 99203)
    assert TextAuthority(world.store).repair(TENANT, ACCOUNT, limit=32, synthetic_engine_only=True,
                                             allowed_fans=frozenset({FAN2})) == 0
    assert not snapshot(world)['projected']
    for bad in (frozenset(), {FAN}, frozenset({'x'})):
        with pytest.raises(Refused):
            TextAuthority(world.store).repair(TENANT, ACCOUNT, limit=32, synthetic_engine_only=True, allowed_fans=bad)
    with pytest.raises(Refused):   # a fan scope is only meaningful for engine recovery
        TextAuthority(world.store).repair(TENANT, ACCOUNT, limit=32, allowed_fans=frozenset({FAN}))


def test_only_the_resolved_attempt_state_is_cleared(supervised, monkeypatch):
    world, env, model, seen, pilot = supervised
    crash_after_accepted(world, pilot, monkeypatch, 99204)
    with Session(world.database[1]) as s, s.begin():
        # Independent concerns on the same and on another fan must survive receipt repair.
        s.get(OfConversationState, (ACCOUNT, FAN)).requires_attention = True
        other = s.get(OfConversationState, (ACCOUNT, FAN2))
        other.unresolved_attempt_id, other.requires_attention = 'other-unresolved-attempt', True
    restarted = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    restarted.tick()
    restarted.tick()
    final = snapshot(world)
    assert final['projected'] and final['unresolved'] is None
    assert final['attention'] is True, 'receipt repair must not clear an independent attention flag'
    with Session(world.database[1]) as s:
        other = s.get(OfConversationState, (ACCOUNT, FAN2))
        assert other.unresolved_attempt_id == 'other-unresolved-attempt' and other.requires_attention is True
    assert len(world.fans.posts) == 1


def test_conflicting_canonical_receipt_stays_held_and_never_blocks_the_tick(supervised, monkeypatch):
    world, env, model, seen, pilot = supervised
    crash_after_accepted(world, pilot, monkeypatch, 99205)
    with Session(world.database[1]) as s, s.begin():
        intent = s.scalar(select(OfTextIntent))
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=intent.provider_message_id, direction='out',
                      body='a different canonical body', created_at=intent.dispatched_at, media_ids='[]', media_count=0,
                      price_cents=0))
    restarted = build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    for _ in range(3):
        restarted.tick()            # must not raise or POST
    final = snapshot(world)
    assert not final['projected'] and final['reason'] == 'canonical_identity_conflict'
    assert final['attention'] is True and final['unresolved'] is not None
    assert final['request'] != 'completed' and final['messages'] == 1   # only the conflicting row that was seeded
    assert len(world.fans.posts) == 1
