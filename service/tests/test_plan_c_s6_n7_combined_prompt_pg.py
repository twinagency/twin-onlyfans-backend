"""V5 section 6, N7: one combined test, burst + corrections/clears + memory + truncation/omission, through the RENDERED prompt
and the final decision, in the AUTONOMOUS text-only pilot (stopped_text -> text_no_offers), signed HTTP intake, normal
pilot.tick(), real ModelRuntime with a recording fake model, real PostgreSQL, fake FansAPI provider.

Components are covered separately elsewhere (writer, audit_regressions, historical_memory); this is the missing combination:
  FAN : a signed burst (an earlier statement, a question, a FAN correction of the earlier statement, then a newest message that is
        longer than the prompt bound, ends with the real question and names a remembered topic) + learned memories in the real
        store (a location, an expired event) + an operator correction of the city, then later an operator CLEAR of that city.
  FAN2: a signed oversize message in the same tick (human handover, no call, no invented answer).
Allowed side and nearest-forbidden side are asserted from the one prompt that the model actually received.
Nothing is seeded as a grant: memories are written as the learner writes them (validated source quotes, digests); the
operator corrections go through HistoricalMemory.correct.
"""
import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from conversation_engine.writer_context import PromptBudget
from db.models import Message
from db.onlyfans_models import OfIngress
from db.onlyfans_engine_models import OfEngineDecision, OfEngineModelCall, OfEngineRequest
from onlyfans_runtime.launch_history_memory.memory import HistoricalMemory
from onlyfans_runtime.profile_learning.evidence import merge, validate
from onlyfans_runtime.profile_learning.worker import fingerprint
from test_engine_real_worker_pg import FAN2, inbound, world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401
from test_plan_c_events_stage3_pg import rig  # noqa: F401
from test_plan_c_scope_pg import ACTOR
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401


@pytest.fixture(autouse=True)
def learning_schema(request, monkeypatch):
    if 'database' not in request.fixturenames:
        return
    monkeypatch.setenv('ONLYFANS_PROFILE_LEARNING_ENABLED', '1')
    _, db_engine = request.getfixturevalue('database')
    root = Path(__file__).parents[2] / 'deploy/profile-learning'
    with db_engine.begin() as connection:
        connection.exec_driver_sql((root / 'schema.sql').read_text())
        connection.exec_driver_sql((root / 'notifications-postgresql.sql').read_text())


def seed_memory(rig, mid, value, *, field, operation='assert', quote=None, date_text=''):
    """A learned memory exactly as the learner stores it, from an OLD stored message (not new intake, so no pilot work)."""
    body = quote or ('Cancel ' + value if operation == 'cancel' else 'I planned ' + value + ' on 2026-01-02')
    with Session(rig.w.database[1]) as s, s.begin():
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=mid, direction='in', body=body,
                      created_at=datetime(2026, 1, 1)))
        s.flush()
        runtime = rig.w.store._runtime(s, TENANT, ACCOUNT)
        # Imported history carries the current binding's source evidence (otherwise the memory projection is, correctly,
        # 'unavailable: unscoped history'); source 'import' is never pilot work.
        s.add(OfIngress(account_id=ACCOUNT, event_key=f'n7-seed-{mid}', binding_id=runtime.binding_id, fan_id=FAN, message_id=mid,
                        source='import', payload='{}', status='applied', received_at=datetime(2026, 1, 1), applied_at=datetime(2026, 1, 1)))
        message = s.get(Message, (ACCOUNT, FAN, mid))
        source = f'{FAN}:{mid}'
        observations = validate({'facts': [{'field': field, 'value': value, 'key': value, 'source': source, 'quote': body,
                                            'operation': operation, 'date_text': date_text}]},
                                {source: {'text': body, 'direction': 'in', 'at': message.created_at.isoformat(),
                                          'digest': fingerprint(message), 'fan': FAN}})
        assert len(observations) == 1
        fact, = merge([], observations)
        s.execute(text('''INSERT INTO of_profile_memories
            (tenant_id,account_id,binding_id,fan_id,memory_key,field,status,kind,document_json)
            VALUES (:tenant,:account,:binding,:fan,:key,:field,:status,:kind,:doc)'''),
                  dict(tenant=TENANT, account=ACCOUNT, binding=runtime.binding_id, fan=FAN, key=fact['memory_key'],
                       field=field, status=fact['status'], kind=fact['kind'], doc=json.dumps(fact)))
    return fact


def correct(rig, key, *, value):
    memory = HistoricalMemory(rig.w.store)
    before = memory.read(TENANT, ACCOUNT, FAN)
    return memory.correct(TENANT, ACCOUNT, FAN, field='home_city', operation='set' if value is not None else 'clear',
                          value=value, expected_version=before['version'], command_key=key, actor=ACTOR,
                          reason='Verified operator correction')


def prompt(rig, i=-1):
    return '\n'.join(m['content'] for m in rig.seen[i]['messages'])


def section(rendered, start, end='\n\n'):
    return rendered.split(start, 1)[1].split(end, 1)[0]


def out_to(rig, fan):
    return [p['body']['text'] for p in rig.w.fans.posts if f'/{fan}/' in p['path']]


def test_burst_corrections_clears_memory_truncation_and_omission_reach_the_one_rendered_prompt_and_the_decision(rig):
    seed_memory(rig, 11, 'Oldtown', field='location', quote='I live in Oldtown')
    seed_memory(rig, 12, 'lagoon festival', field='event', date_text='2026-01-02')
    stored_before = None
    with Session(rig.w.database[1]) as s:
        stored_before = s.scalar(select(func.count()).select_from(Message))
    # The operator corrects the city; every learned location memory is then ineligible.
    correct(rig, 'n7-set-city', value='Perth')

    long_tail = 'Correction: I live in Cairns, not Oldtown. Do you remember the lagoon festival?'
    newest = ('Yesterday was busy. ' * 62) + long_tail
    assert 1200 < len(newest) < 2000, 'longer than the prompt bound, shorter than the memory-query window'
    burst = [(72000, 'I am flying to Sydney next week'), (72001, 'what is your favorite color?'),
             (72002, 'sorry, I meant Cairns, not Sydney'), (72003, newest)]
    for mid, body in burst:
        rig.post(inbound(mid, body, fan=FAN), key=f'n7-{mid}')
    oversize = 'ZZ-FAN2-OVERSIZE I need to tell you everything about my week. ' * 160     # about 9,600 characters
    assert len(oversize) > 6000
    rig.post(inbound(72100, oversize, fan=FAN2), key='n7-fan2')
    assert rig.seen == [] and rig.w.fans.posts == [], 'intake alone never plans or sends'

    rig.ticks(5)

    # --- the single prompt the model received (one call: the whole burst is one turn, the oversize fan made none) ---------
    assert len(rig.seen) == 1, 'one model call for FAN; none for FAN2'
    p = prompt(rig, 0)
    unresolved = section(p, 'UNRESOLVED REQUESTS AND ACTIONS', '\n\nLATEST')
    for mid, needle in ((72000, 'flying to Sydney'), (72001, 'favorite color'), (72002, 'I meant Cairns, not Sydney')):
        assert f'UNANSWERED {{"message_id": {mid}' in unresolved and needle in unresolved, \
            f'earlier burst message {mid} is shown as unanswered, not dropped'
    assert 'UNANSWERED lines are his earlier messages' in unresolved
    latest = p.split('LATEST:\nFAN: ', 1)[1].split('\n\nTHIS TURN:', 1)[0]
    assert latest.startswith('Yesterday was busy.') and long_tail in latest, 'the trailing correction and question survive'
    assert 'characters omitted from the middle of this long message' in latest and len(latest) <= 1200, 'explicit truncation marker'
    assert 'ZZ-FAN2-OVERSIZE' not in p, 'the other fan\'s message never enters this fan\'s prompt'
    know = section(p, 'WHAT SHE KNOWS ABOUT HIM')
    assert 'his city: Perth' in know and 'current corrections override older chat' in know, 'operator correction is the current fact'
    assert 'Oldtown' not in know, 'the superseded learned location is not presented as memory'
    assert 'lagoon festival' in know and 'historical' in know and 'expired' in know, 'topical historical memory, labelled as such'
    assert 'not current fact' in know or 'outcome unconfirmed' in know or 'uncertain' in know or 'expired' in know

    # --- the final decision: one answer to FAN through the real path, none for FAN2 --------------------------------------
    assert out_to(rig, FAN) == ['hey you'] and out_to(rig, FAN2) == []
    assert not rig.w.fans.posts[0]['body'].get('price') and not rig.w.fans.posts[0]['body'].get('mediaFiles')
    with Session(rig.w.database[1]) as s:
        requests = {r.fan_id: r.status for r in s.scalars(select(OfEngineRequest))}
        calls = s.scalars(select(OfEngineModelCall)).all()
        decisions = s.scalars(select(OfEngineDecision).where(OfEngineDecision.fan_id == FAN2)).all()
        assert s.scalar(select(func.count()).select_from(Message).where(Message.fan_id == FAN2, Message.direction == 'out')) == 0
    assert requests.get(FAN) == 'completed', requests
    assert requests.get(FAN2) != 'completed', 'the oversize request was not answered'
    assert len(calls) == 1 and calls[0].fan_id == FAN and calls[0].outcome == 'verified_ok'
    assert decisions == [], 'no decision text was invented for the oversize message'
    cases = rig.w.cases('model_handover')
    assert cases, 'the oversize message is an explicit human handover case, not a silent stall'
    assert rig.pilot.tick()['dispatched'] == [] and len(rig.seen) == 1, 'a further tick re-plans nothing'

    # --- phase B: the operator CLEARS the city; a new message; a tight memory budget forces an explicit omission ---------
    correct(rig, 'n7-clear-city', value=None)
    rig.pilot.worker.engine.model.budget = PromptBudget(memory_chars=350)
    rig.post(inbound(72200, 'so where do you think I live, and was the lagoon festival fun?', fan=FAN), key='n7-b')
    rig.ticks(5)
    assert len(rig.seen) == 2 and len(out_to(rig, FAN)) == 2 and out_to(rig, FAN2) == []
    q = prompt(rig, 1)
    knowb = section(q, 'WHAT SHE KNOWS ABOUT HIM')
    assert 'Operator-cleared fields (do not reuse older statements): his city' in q, 'the clear is rendered as a clear'
    assert 'Perth' not in q and 'Oldtown' not in knowb, 'neither the old operator value nor the learned one is a fact now'
    assert 'omitted' in knowb, 'the budget omission is stated, not silent'
    assert 'ZZ-FAN2-OVERSIZE' not in q
    with Session(rig.w.database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfEngineModelCall)) == 2
    assert out_to(rig, FAN2) == [] and len(rig.seen) == 2, 'FAN2 stays a handover: still no call and no send'
    assert stored_before is not None
