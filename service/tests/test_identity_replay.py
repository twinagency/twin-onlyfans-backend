import json
import sqlite3
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from native_pilot.ledger import Ledger
from native_pilot.service import Service

OLD, TRUE, STORY, YO, NEW = 11853522168780, 11854658663789, 11855309515258, 11855309515259, 11855309515260

def setup(tmp_path):
    path = tmp_path / 'identity.db'
    with sqlite3.connect(path) as con:
        con.executescript('''CREATE TABLE native_pilot_inputs(input_key TEXT,pilot_id TEXT,message_id INTEGER,
            member_ids_json TEXT,state TEXT,updated_at TEXT);
            CREATE TABLE native_pilots(pilot_id TEXT,owner_token TEXT);
            INSERT INTO native_pilots VALUES('gervonta','owner');''')
    return path

def service(path, pilot='gervonta'):
    s = Service.__new__(Service)
    s.ledger, s.pilot_id = Ledger(path), pilot
    return s

def record(path, anchor, members, state='generating', pilot='gervonta'):
    with sqlite3.connect(path) as con:
        con.execute('INSERT INTO native_pilot_inputs VALUES(?,?,?,?,?,NULL)',
                    (str(anchor), pilot, anchor, json.dumps(members), state))

def finish(s, anchor):
    s.ledger.finish_input(pilot_id=s.pilot_id, owner_token='owner', input_key=str(anchor), now='2026-10-30T08:18:02')

def test_handoff_resume_new_yo_and_restart(tmp_path):
    path = setup(tmp_path)
    s = service(path)
    old = {OLD: {'body': '<p>Are you a bot or something?</p>'}}
    record(path, OLD, [OLD])
    assert set(s.identity_questions(old)) == {OLD}  # first question must hand off
    finish(s, OLD)  # same durable completion used after identity_handoff
    burst = {**old, TRUE: {'body': 'True'}, STORY: {'body': 'Tell me a story'}, YO: {'body': 'Yo'}}
    record(path, YO, list(burst))
    for _ in range(3):
        s = service(path)  # a fresh process / repeated operator hand-back
        assert s.identity_questions(burst) == {}
        assert OLD in burst  # complete history is retained
        assert s.ledger.input_claimed(pilot_id='gervonta', message_id=YO)
    burst[NEW] = {'body': 'Are you a bot?'}
    assert set(s.identity_questions(burst)) == {NEW}
    finish(s, YO)
    assert set(service(path).identity_questions(burst)) == {NEW}

@pytest.mark.parametrize('state', ['generating', 'requeued:1', 'abandoned'])
def test_unfinished_input_never_suppresses_guard(tmp_path, state):
    path = setup(tmp_path)
    record(path, OLD, [OLD], state)
    assert set(service(path).identity_questions({OLD: {'body': 'are you a bot?'}})) == {OLD}

def test_completed_burst_member_and_pilot_isolation(tmp_path):
    path = setup(tmp_path)
    record(path, TRUE, [OLD, TRUE], 'complete')
    questions = {OLD: {'body': 'are you a bot?'}, NEW: {'body': 'is this ai?'}}
    assert set(service(path).identity_questions(questions)) == {NEW}
    assert set(service(path, 'other-fan').identity_questions(questions)) == {OLD, NEW}

def test_empty_legacy_members_use_completed_anchor(tmp_path):
    path = setup(tmp_path)
    record(path, OLD, [], 'complete')
    assert service(path).identity_questions({OLD: {'body': 'are you a bot?'}}) == {}

def test_actual_turn_handoff_resume_and_duplicate_admission(tmp_path, monkeypatch):
    from native_pilot import service as mod
    path = setup(tmp_path)
    sources = {OLD: {'body': 'Are you a bot or something?'}}
    sent, pauses = [], []
    monkeypatch.setattr(mod.runner, 'read_sources', lambda *a: dict(sources))
    try:
        from native_pilot import pacing
    except ImportError:
        pacing = None
    if pacing is not None:
        monkeypatch.setattr(pacing, 'settings', lambda *a: {})

    def instance():
        s = service(path)
        real = s.ledger
        def snapshot(**kw):
            return SimpleNamespace(message_id=max(sources), member_ids=tuple(sources),
                                   ingested_at='2026-10-30T08:00:00', created_at='2026-10-30T08:00:00',
                                   earliest_created_at='2026-10-30T08:00:00')
        def claim(**kw):
            mid = kw['snapshot'].message_id
            record(path, mid, list(sources))
            return str(mid)
        s.ledger = SimpleNamespace(snapshot=snapshot, claim_input=claim,
            input_claimed=real.input_claimed, finish_input=real.finish_input,
            unhandled_source_ids=real.unhandled_source_ids)
        s.pilot = SimpleNamespace(pilot_id='gervonta', owner_token='owner')
        s.args, s.path, s.db = SimpleNamespace(fan=318022380), path, 'of-323358988'
        s.quiet_s, s.poll_s = 0, 0
        s.tuning = SimpleNamespace(burst_max_wait_s=90)
        s.cooldown_left = lambda: 0
        s._note_unlabelled, s.wait = AsyncMock(), AsyncMock()
        async def handoff(*a, **kw): pauses.append(a[0])
        async def execute(key, context, **kw):
            assert OLD in context  # guard filtering must not remove context
            sent.append(kw['source_id'])
            real.finish_input(pilot_id='gervonta', owner_token='owner', input_key=key, now=mod.runner.now())
        s.identity_handoff, s.execute = handoff, execute
        return s

    asyncio.run(instance().turn())
    assert pauses == ['fan-asked'] and sent == []
    sources[YO] = {'body': 'Yo'}  # operator resumed; the old question remains in the batch
    asyncio.run(instance().turn())
    assert pauses == ['fan-asked'] and sent == [YO]
    for _ in range(3):
        asyncio.run(instance().turn())  # restart / repeated resume cannot admit it again
    assert sent == [YO] and len(pauses) == 1
    sources[NEW] = {'body': 'Are you a bot?'}
    asyncio.run(instance().turn())
    assert pauses == ['fan-asked', 'fan-asked'] and sent == [YO]
