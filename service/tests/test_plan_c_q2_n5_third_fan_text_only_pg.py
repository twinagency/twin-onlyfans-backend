"""Q2 journey I (N5): a third (non-pilot) fan in the autonomous text-only profile never reaches the wire or the model.

Profile: stopped_text -> text_no_offers (autonomous, delivery_mode text_only, pilot fans FAN and FAN2), signed HTTP
intake, normal pilot.tick(), real ModelRuntime with a counting fake model, real PostgreSQL, fake FansAPI provider.
Inbound from the third fan is signed intake like any other; the outbound side is probed at the engine/worker boundary
(planning scope, worker.turn/resume) for a fan whose canonical inbound message exists in the account.
Control in every test: the two pilot fans are still answered exactly once each.
"""
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Message
from db.onlyfans_engine_models import OfEnginePilotWebhookDigest, OfEngineRequest
from onlyfans_runtime.store import Refused
from test_engine_real_worker_pg import FAN2, inbound, world  # noqa: F401
from test_onlyfans_foundation_pg import database, event  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401
from test_plan_c_events_stage3_pg import ev, rig, tip_event  # noqa: F401
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401

THIRD = 999000111


def to_fan(rig, fan):
    return [p['body'] for p in rig.w.fans.posts if f'/{fan}/' in p['path']]


def test_third_fan_inbound_never_reaches_the_model_or_the_wire_and_pilot_fans_are_still_answered(rig):
    third = [
        'hi, send me a nude pic please',
        'how much for your videos?',
        'please stop texting me',                        # an explicit stop from a non-pilot fan
        'hello? anyone there',
        'can we meet up tonight?',
    ]
    for n, text in enumerate(third):
        rig.post(inbound(80000 + n, text, fan=THIRD), key=f'third-{n}')
    rig.post(tip_event(THIRD, tid='third-tip'), key='third-tip')
    rig.post(ev('messages.received', {'id': 80100, 'fromUser': {'id': THIRD}, 'text': 'with media', 'createdAt': '2026-11-10T00:00:00Z',
                                      'isSentByMe': False, 'media': [{'id': 1, 'type': 'photo'}], 'price': 5}), key='third-media')
    assert rig.w.fans.posts == [] and rig.seen == [], 'nothing before the pilot fans speak'
    rig.fans_in()
    rig.ticks(5)
    assert to_fan(rig, THIRD) == [] and len(rig.seen) == 2, 'one model call per pilot fan, none for the third fan'
    assert len(to_fan(rig, FAN)) == 1 and len(to_fan(rig, FAN2)) == 1, 'control: both pilot fans answered once'
    assert rig.requests() == {FAN: 'completed', FAN2: 'completed'}
    assert not any(THIRD_text in json.dumps(call) for call in rig.seen for THIRD_text in third), \
        'no third-fan text appears in any model prompt'
    with Session(rig.w.database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Message).where(Message.fan_id == THIRD)) == 0, \
            'the third fan has no canonical message, request or conversation in the pilot'
        assert s.scalar(select(func.count()).select_from(OfEngineRequest).where(OfEngineRequest.fan_id == THIRD)) == 0
        holds = [h for _, h in rig.digests()]
    assert not any(holds), 'a non-pilot fan\'s events do not freeze either pilot fan'
    assert rig.block(FAN) is None and rig.block(FAN2) is None
    # A later message from the third fan after the pilot fans were served is still ignored.
    rig.post(inbound(80200, 'hey again', fan=THIRD), key='third-late')
    rig.ticks(3)
    assert to_fan(rig, THIRD) == [] and len(rig.seen) == 2 and len(rig.w.fans.posts) == 2


def test_engine_and_worker_boundary_refuse_to_plan_or_answer_a_third_fan_even_with_a_canonical_message(rig):
    worker = rig.pilot.worker
    # The account already holds a canonical inbound message from this fan (history that predates the pilot scope).
    rig.w.store.record(TENANT, ACCOUNT, event(mid=80300, fan=THIRD, body='hello, anyone there?'))
    rig.w.store.drain(TENANT, ACCOUNT)
    with Session(rig.w.database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Message).where(Message.fan_id == THIRD)) == 1
    assert worker.turn(TENANT, ACCOUNT, THIRD, 80300) == {'outcome': 'outside_pilot_scope'}
    assert worker.resume(TENANT, ACCOUNT, THIRD, 80300) == {'outcome': 'outside_pilot_scope'}
    with pytest.raises(Refused):
        worker.engine.plan(TENANT, ACCOUNT, THIRD, 80300)
    assert rig.seen == [] and rig.w.fans.posts == [], 'no model call and no POST for the third fan'
    with Session(rig.w.database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfEngineRequest).where(OfEngineRequest.fan_id == THIRD)) == 0
    # The worker as a whole is unharmed: control, both pilot fans are answered.
    rig.fans_in()
    rig.ticks(5)
    assert to_fan(rig, THIRD) == [] and len(rig.seen) == 2
    assert len(to_fan(rig, FAN)) == 1 and len(to_fan(rig, FAN2)) == 1
