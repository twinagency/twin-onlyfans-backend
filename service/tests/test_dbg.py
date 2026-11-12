import json
import pytest
from sqlalchemy.orm import Session
from commercial_worker_fixture import *
from test_commercial_normal_worker_pg import world  # noqa
from test_onlyfans_foundation_pg import database  # noqa
from db.onlyfans_engine_models import OfEngineDecision
from conversation_engine.writer_context import set_active_profiles

def test_dbg(world):
    world.product('Sunset photo', [31, 32], previews=[31])
    world.launch()
    staged, mid = world.say('show me the sunset photo please')
    world.approve_turn(staged); world.unthrottle()
    print('DBG tick', world.pilot.tick())
    print('DBG posts', world.fans_mock.posts)
    for d in world.rows(OfEngineDecision): print('DBG dec', d.action, d.status, d.body)
    print('DBG prompts', world.model_prompts[-1][-1500:])
    assert False
