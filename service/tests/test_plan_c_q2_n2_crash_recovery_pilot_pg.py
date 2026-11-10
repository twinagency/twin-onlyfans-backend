"""Q2 journey D (N2): crash recovery through the AUTONOMOUS text-only pilot tick.

The earlier prewire/receipt restart tests run the supervised fixture (authorization_mode='supervised', approve() per
message, full-mode scope). Here the profile is the deployed one: stopped_text -> text_no_offers (autonomous,
delivery_mode text_only), signed HTTP intake, the real ModelRuntime, normal pilot.tick() before and after the crash,
a cold-built replacement pilot on the same durable state. Real PostgreSQL, fake model, fake FansAPI provider.
Crashes are BaseException so no `except Exception` swallows them, as with a killed process.
"""
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineRequest
from model_transport.pilot_supervised import build_supervised_pilot
from test_engine_real_worker_pg import FAN2, answer, inbound, world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401
from test_plan_c_events_stage3_pg import rig  # noqa: F401
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401


class Crash(BaseException):
    """The process dies here."""


def expire_dispatching(rig):
    with rig.w.database[1].begin() as c:
        c.execute(text("UPDATE of_text_intent SET deadline = clock_timestamp() - interval '1 second' WHERE status='dispatching'"))


def restart(rig):
    rig.client.__exit__(None, None, None)
    from fastapi.testclient import TestClient
    from plan_c_worker import create_app
    rig.pilot = build_supervised_pilot(rig.w.store, rig.env, model_transport=rig.model, fans_transport=rig.w.fans.transport)
    rig.client = TestClient(create_app(rig.pilot, run_loop=False))
    rig.client.__enter__()


def facts(rig):
    with Session(rig.w.database[1]) as s:
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        return dict(
            intents=[(i.fan_id, i.status, i.reason, i.wire_started_at is not None, i.projected_at is not None, i.reservation_held)
                     for i in s.scalars(select(OfTextIntent).order_by(OfTextIntent.created_at))],
            decisions=[(d.status) for d in s.scalars(select(OfEngineDecision).order_by(OfEngineDecision.created_at))],
            requests={r.fan_id: r.status for r in s.scalars(select(OfEngineRequest))},
            used=policy.used_units, queued=policy.queued_count,
            unresolved={f: s.get(OfConversationState, (ACCOUNT, f)).unresolved_attempt_id is not None for f in (FAN, FAN2)},
            out_messages=s.scalar(select(func.count()).select_from(Message).where(Message.direction == 'out')),
            posts=len(rig.w.fans.posts), model=len(rig.seen))


def test_crash_before_the_wire_is_retired_and_the_fan_is_answered_exactly_once_after_restart(rig, monkeypatch):
    rig.post(inbound(79101, 'hello, how was your day?', fan=FAN), key='n2-a')

    def die(*args, **kwargs):
        raise Crash()
    with monkeypatch.context() as scoped:       # undo only this patch, never the fixture's environment
        scoped.setattr(rig.pilot.worker.transport, 'send', die)
        with pytest.raises(Crash):
            rig.pilot.tick()
    crashed = facts(rig)
    assert crashed['posts'] == 0 and crashed['model'] == 1
    assert [(i[1], i[3]) for i in crashed['intents']] == [('dispatching', False)], 'the grant exists, the wire was never started'
    assert crashed['used'] == 1
    expire_dispatching(rig)
    restart(rig)
    rig.post(inbound(79102, 'hey are you there?', fan=FAN2), key='n2-b')
    rig.ticks(5)
    final = facts(rig)
    first = final['intents'][0]
    assert first[:5] == (FAN, 'cancelled', 'prewire_crash_never_wired', False, False) and first[5] is False, \
        'the never-wired attempt is retired, not held unknown, with its reservation released'
    assert final['posts'] == 2 and final['out_messages'] == 2, 'one POST per fan, never a replay of the crashed one'
    assert [p['key'] for p in rig.w.fans.posts] == sorted({p['key'] for p in rig.w.fans.posts}, key=[p['key'] for p in rig.w.fans.posts].index), 'no repeated idempotency key'
    assert final['used'] == 2 and final['queued'] == 0, 'units: the released one is not counted twice, nothing leaked'
    assert final['requests'] == {FAN: 'completed', FAN2: 'completed'}
    assert final['unresolved'] == {FAN: False, FAN2: False}
    assert final['decisions'].count('cancelled') >= 1 and 'unknown' not in final['decisions']
    assert not any(i[1] == 'unknown' for i in final['intents'])


def test_crash_after_the_provider_accepted_and_before_projection_materializes_once_without_a_second_post(rig, monkeypatch):
    rig.post(inbound(79111, 'hello, how was your day?', fan=FAN), key='n2-c')
    authority = rig.pilot.worker.authority
    original = authority.materialize

    def crash(*args, **kwargs):
        raise RuntimeError('crash after the accepted receipt committed')
    monkeypatch.setattr(authority, 'materialize', crash)
    with pytest.raises(RuntimeError, match='crash after the accepted receipt'):
        rig.pilot.tick()
    crashed = facts(rig)
    assert crashed['posts'] == 1
    assert [(i[1], i[4]) for i in crashed['intents']] == [('accepted', False)], 'accepted receipt saved, not yet projected'
    monkeypatch.setattr(authority, 'materialize', original)
    restart(rig)
    rig.post(inbound(79112, 'hey are you there?', fan=FAN2), key='n2-d')
    results = rig.ticks(5)
    final = facts(rig)
    assert final['posts'] == 2, 'the accepted send was not repeated and the other fan was answered once'
    assert [(i[0], i[1], i[4]) for i in final['intents']] == [(FAN, 'accepted', True), (FAN2, 'accepted', True)]
    assert final['out_messages'] == 2 and final['requests'] == {FAN: 'completed', FAN2: 'completed'}
    assert final['unresolved'] == {FAN: False, FAN2: False} and final['used'] == 2 and final['queued'] == 0
    assert final['model'] == 2, 'recovery spent no extra model call'
    assert sum(r['dispatched'].count('accepted') for r in results) <= 1


def test_crash_after_the_request_reached_the_wire_stays_unknown_never_replayed_and_the_other_fan_continues(rig):
    rig.post(inbound(79121, 'hello, how was your day?', fan=FAN), key='n2-e')
    handler = rig.w.fans.transport.handler

    def wire_then_die(request):
        if request.method == 'POST' and request.url.path.endswith('/messages'):
            rig.w.fans.posts.append({'path': request.url.path, 'key': request.headers['Idempotency-Key'], 'body': None, 'auth': None})
            raise Crash()
        return handler(request)
    rig.w.fans.transport.handler = wire_then_die
    with pytest.raises(Crash):
        rig.pilot.tick()
    rig.w.fans.transport.handler = handler
    assert len(rig.w.fans.posts) == 1
    expire_dispatching(rig)
    restart(rig)
    rig.post(inbound(79122, 'hey are you there?', fan=FAN2), key='n2-f')
    rig.ticks(5)
    final = facts(rig)
    assert final['intents'][0][:4] == (FAN, 'unknown', 'dispatch_expired', True), 'a wired attempt is unknown, never retried'
    assert final['intents'][0][5] is True, 'its reservation is kept (uncertain cost is never refunded)'
    assert final['unresolved'][FAN] is True
    assert final['posts'] == 2, 'exactly one more POST: the other fan, nothing for the unknown attempt'
    assert [p['key'] for p in rig.w.fans.posts][0] not in [p['key'] for p in rig.w.fans.posts][1:]
    assert final['requests'][FAN2] == 'completed' and final['requests'][FAN] != 'completed'
    rig.post(inbound(79123, 'hello??', fan=FAN), key='n2-g')
    rig.ticks(4)
    assert facts(rig)['posts'] == 2, 'an unresolved unknown send blocks that fan'
