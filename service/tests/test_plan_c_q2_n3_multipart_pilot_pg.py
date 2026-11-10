"""Q2 journey C (N3): multipart replies through the AUTONOMOUS text-only pilot tick, with a stop between parts.

Text-only autonomous profile, signed HTTP intake, the real ModelRuntime (httpx.MockTransport model that emits
"body" + "parts"), the normal pilot.tick(), real PostgreSQL, fake FansAPI provider. The stop happens inside the
provider call that carries part one (the exact window between part one and part two).

The model answers with a different sequence on each call (A..., then B...), so a part of the FIRST sequence
reaching the wire after the stop is visible no matter how the request is re-planned. Nothing is seeded: stop and
resume use the audited scope/policy operations, takeover uses the store control, the correction is signed intake.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from db.models import OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineRequest
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.store import Refused
from plan_c_worker import create_app
from test_engine_real_worker_pg import FAN2, inbound, world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401
from test_plan_c_events_stage3_pg import rig  # noqa: F401
from test_plan_c_scope_pg import ACTOR, key
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401

A = ['oh that is such a fun story', 'i was laughing the whole time', 'what happened after that']
B = ['sorry, say that again for me', 'i want to get this right']
STALE = (A[1], A[2])


def install_model(rig):
    """The one model: call 1 -> sequence A, every later call -> sequence B. Returns after rebuilding the real pilot."""
    def respond(request):
        rig.seen.append(json.loads(request.content))
        texts = A if len(rig.seen) == 1 else B
        return httpx.Response(200, json={'model': 'm/primary', 'choices': [{'message': {'content': json.dumps(
            {'body': texts[0], 'parts': texts[1:]})}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'cost': 0.0001},
            'openrouter_metadata': {'endpoints': {'available': [{'provider': 'Fixture Provider', 'selected': True}]}}})
    rig.model = httpx.MockTransport(respond)
    reopen(rig)


def reopen(rig):
    rig.client.__exit__(None, None, None)
    rig.pilot = build_supervised_pilot(rig.w.store, rig.env, model_transport=rig.model, fans_transport=rig.w.fans.transport)
    rig.client = TestClient(create_app(rig.pilot, run_loop=False))
    rig.client.__enter__()


def tick(rig):
    try:
        return rig.pilot.tick()
    except Refused:           # a disabled sender policy makes the worker refuse to run at all: nothing is sent
        return None


def to_fan(rig, fan):
    return [p['body']['text'] for p in rig.w.fans.posts if f'/{fan}/' in p['path']]


def decisions(rig):
    """(request, part, status) of every engine decision, oldest request first."""
    with Session(rig.w.database[1]) as s:
        rows = list(s.scalars(select(OfEngineDecision).order_by(OfEngineDecision.created_at, OfEngineDecision.part_index)))
        return [(d.request_id, d.part_index, d.status) for d in rows]


def intents(rig):
    with Session(rig.w.database[1]) as s:
        return [(i.status, i.reason) for i in s.scalars(select(OfTextIntent))]


def requests(rig):
    with Session(rig.w.database[1]) as s:
        return [(r.fan_id, r.status) for r in s.scalars(select(OfEngineRequest).order_by(OfEngineRequest.created_at))]


def policy_cycle(rig):
    with Session(rig.w.database[1]) as s:
        p = s.get(OfDeliveryPolicy, ACCOUNT)
        revision, limit, throttle = p.revision, p.limit_units, p.throttle_seconds
    common = dict(limit_units=limit, throttle_seconds=throttle, media_enabled=False, transport_kind='fansapi', actor=ACTOR)
    rig.w.authority.configure(TENANT, ACCOUNT, expected_revision=revision, enabled=False, reason='disable between parts', **common)
    rig.w.authority.configure(TENANT, ACCOUNT, expected_revision=revision + 1, enabled=True, reason='re-enable between parts', **common)


def scope_stop(rig, holder):
    current = pilot_scope.describe(rig.w.store, TENANT, ACCOUNT)
    holder['stopped'] = pilot_scope.set_status(rig.w.store, TENANT, ACCOUNT, 'stopped', expected_revision=current['revision'],
                                                command_key=key(81001), actor=ACTOR, reason='normal stop between parts')


def takeover(rig):
    with Session(rig.w.database[1]) as s:
        revision = s.get(OfConversationState, (ACCOUNT, FAN)).revision
    rig.w.store.control(TENANT, ACCOUNT, FAN, mode='human_controlled', expected_revision=revision, command_key='n3-takeover',
                        actor=ACTOR, reason='takeover between parts', action='request_takeover')


def test_control_a_multipart_reply_goes_out_in_order_exactly_once_through_the_autonomous_tick(rig):
    install_model(rig)
    rig.post(inbound(79001, 'so yesterday something wild happened', fan=FAN), key='n3-in')
    first = rig.pilot.tick()
    assert first['dispatched'] == ['accepted'] and to_fan(rig, FAN) == A
    for _ in range(3):
        assert rig.pilot.tick()['dispatched'] == []
    assert to_fan(rig, FAN) == A and len(rig.seen) == 1, 'no repeat, no second model call'
    assert decisions(rig) == [(decisions(rig)[0][0], i, 'completed') for i in range(3)]
    assert intents(rig) == [('accepted', None)] * 3 and requests(rig) == [(FAN, 'completed')]


@pytest.mark.parametrize('kind', ['policy_disable_enable', 'scope_stop_then_resume', 'takeover', 'fan_correction'])
def test_a_stop_between_part_one_and_part_two_never_lets_the_stale_parts_resume(rig, kind):
    install_model(rig)
    holder = {}

    def between_parts(count):
        if count != 1:
            return
        if kind == 'policy_disable_enable':
            policy_cycle(rig)
        elif kind == 'scope_stop_then_resume':
            scope_stop(rig, holder)
        elif kind == 'takeover':
            takeover(rig)
        else:
            rig.post(inbound(79002, 'wait, actually i meant something else', fan=FAN), key='n3-correction')
    rig.w.fans.hooks.insert(0, between_parts)
    rig.post(inbound(79001, 'so yesterday something wild happened', fan=FAN), key='n3-in')
    tick(rig)
    rig.w.fans.hooks.remove(between_parts)

    # Part one was the only text that reached the wire from the first sequence; the rest were cancelled, not parked.
    assert to_fan(rig, FAN)[:1] == [A[0]], 'part one is real and recorded once'
    assert not set(STALE) & {p['body']['text'] for p in rig.w.fans.posts}
    first_request = decisions(rig)[:3]
    assert [(part, status) for _, part, status in first_request] == [(0, 'completed'), (1, 'cancelled'), (2, 'cancelled')]
    assert intents(rig)[0] == ('accepted', None) and ('refused', 'sequence_capacity_insufficient') not in intents(rig)
    assert requests(rig)[0][1] != 'completed'

    if kind == 'scope_stop_then_resume':
        assert tick(rig) is None, 'stopped scope: the worker does not run'
        resumed = pilot_scope.set_status(rig.w.store, TENANT, ACCOUNT, 'active', expected_revision=holder['stopped']['revision'],
                                         command_key=key(81002), actor=ACTOR, reason='resume after part one was resolved')
        assert resumed['status'] == 'active'
        reopen(rig)
    posts_before = len(rig.w.fans.posts)
    for _ in range(3):
        tick(rig)
    assert not set(STALE) & {p['body']['text'] for p in rig.w.fans.posts}, 'a stale part crossed the stop/resume boundary'
    if kind != 'fan_correction':
        assert len(rig.w.fans.posts) == posts_before and len(rig.seen) == 1, \
            'resume or re-enable alone neither resumes the old parts nor re-plans without a new fan message'
    else:
        assert to_fan(rig, FAN)[1:] == B, 'the correction is answered by a fresh sequence, never by the old parts'

    # The other fan is unaffected (after the same normal resume) and the stale parts still cannot appear.
    rig.post(inbound(79010, 'hey are you there?', fan=FAN2), key='n3-other')
    for _ in range(3):
        tick(rig)
    assert to_fan(rig, FAN2) == B
    assert not set(STALE) & {p['body']['text'] for p in rig.w.fans.posts}
    if kind in ('policy_disable_enable', 'scope_stop_then_resume'):
        # A new message from the fan is answered by a new sequence only.
        rig.post(inbound(79011, 'anyway, what do you think?', fan=FAN), key='n3-later')
        for _ in range(3):
            tick(rig)
        assert to_fan(rig, FAN)[1:] == B and not set(STALE) & set(to_fan(rig, FAN))
    if kind == 'takeover':
        rig.post(inbound(79012, 'hello??', fan=FAN), key='n3-while-taken-over')
        for _ in range(3):
            tick(rig)
        assert to_fan(rig, FAN) == [A[0]], 'a person has the fan: nothing more is sent'
