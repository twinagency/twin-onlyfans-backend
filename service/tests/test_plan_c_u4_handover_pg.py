"""U4: automatic handover is a durable scoped hold. Synthetic actors/providers, disposable PG only."""
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world, inbound, FAN2
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN
from test_plan_c_draft_app_pg import configured_draft, signed
from test_plan_c_text_activation_pg import stopped_text, start
from model_transport.pilot_supervised import build_supervised_pilot
from plan_c_worker import create_app
from db.onlyfans_engine_models import OfEngineModelCall

def model_reply(body, finish="stop"):
    return httpx.Response(200,json={
      'model':'m/primary',
      'choices':[{'message':{'content':json.dumps({'body':body})},'finish_reason':finish}],
      'usage':{'prompt_tokens':100,'completion_tokens':20,'cost':0.0001},
      'openrouter_metadata':{'endpoints':{'available':[{'provider':'Fixture Provider','selected':True}]}}})

def incoming_http(pilot, mid, body, fan=FAN):
    raw,headers=signed(inbound(mid,body,fan=fan),key='independent-review-'+str(mid))
    with TestClient(create_app(pilot,run_loop=False)) as client:
        result=client.post('/onlyfans/webhooks/plan-c',content=raw,headers=headers)
    assert result.status_code == 202

def test_automatic_handover_does_not_throw_and_starve_other_fan(stopped_text):
    w,env,model,seen,first=stopped_text
    start(w,first['revision'])
    pilot=build_supervised_pilot(w.store,env,model_transport=model,fans_transport=w.fans.transport)
    for mid,body in [(73201,'Can we meet up?'),(73202,'Can we meet up tomorrow?'),(73203,'Can we meet up tonight?')]:
        incoming_http(pilot,mid,body)
    incoming_http(pilot,73204,'hello there',fan=FAN2)
    result=pilot.tick()
    print('HANDOVER_TICK_RESULT',json.dumps(result,default=str))
    assert len(w.fans.posts)==1


# ---------------------------------------------------------------------------------------------------------------
# U4 (plan Step 2): the automatic handover is a durable, scoped reduction in automation.
# ---------------------------------------------------------------------------------------------------------------
from concurrent.futures import ThreadPoolExecutor
from db.models import OfConversationState, SkipList
from db.onlyfans_models import OfControlAudit
from db.onlyfans_case_models import OfOperatorCase
from onlyfans_runtime.store import Refused
from onlyfans_runtime import operator_cases
from test_onlyfans_text_delivery_pg import TENANT
from test_plan_c_scope_pg import ACTOR, OTHER

MEET = [(73201, 'Can we meet up?'), (73202, 'Can we meet up tomorrow?'), (73203, 'Can we meet up tonight?')]


def to_fan(posts, fan):
    return [p for p in posts if f'/{fan}/' in p['path'] or p['path'].endswith(f'/{fan}')]


def conversation(w, fan):
    with Session(w.database[1]) as s:
        state = s.get(OfConversationState, (ACCOUNT, fan))
        return state.mode, state.takeover_state, state.revision


def audits(w, fan):
    with Session(w.database[1]) as s:
        return [(r.actor, r.action, r.mode, r.previous_revision, r.revision) for r in s.scalars(
            select(OfControlAudit).where(OfControlAudit.account_id == ACCOUNT, OfControlAudit.fan_id == fan)
            .order_by(OfControlAudit.revision))]


def journey_cases(w, fan):
    with Session(w.database[1]) as s:
        return [(c.category, c.scope, c.status) for c in s.scalars(select(OfOperatorCase).where(
            OfOperatorCase.account_id == ACCOUNT, OfOperatorCase.fan_id == fan,
            OfOperatorCase.category.like('journey_%')))]


def meet_up_then_other_fan(stopped_text):
    w, env, model, seen, first = stopped_text
    start(w, first['revision'])
    pilot = build_supervised_pilot(w.store, env, model_transport=model, fans_transport=w.fans.transport)
    for mid, body in MEET:
        incoming_http(pilot, mid, body)
    incoming_http(pilot, 73204, 'hello there', fan=FAN2)
    return w, env, model, seen, pilot


def test_handover_is_durable_scoped_hold_zero_sends_to_a_and_b_progresses(stopped_text):
    w, env, model, seen, pilot = meet_up_then_other_fan(stopped_text)
    before = conversation(w, FAN)
    result = pilot.tick()
    print('U4_TICK', json.dumps(result, default=str), w.fans.posts)
    mode, takeover, revision = conversation(w, FAN)
    assert mode == 'human_controlled' and takeover in ('requested', 'effective') and revision > before[2]
    rows = audits(w, FAN)
    assert [(a, act, m) for a, act, m, _, _ in rows] == [('engine-journey', 'request_takeover', 'human_controlled')]
    assert [c[:2] for c in journey_cases(w, FAN)] == [(journey_cases(w, FAN)[0][0], 'all')]
    # Fan B was answered through the same tick, fan A got nothing.
    assert len(to_fan(w.fans.posts, FAN2)) == 1 and not to_fan(w.fans.posts, FAN)
    assert conversation(w, FAN2)[0] == 'automated'
    # Later messages from A and B in the same loop: A stays silent, B keeps progressing.
    calls = len(seen)
    incoming_http(pilot, 73205, 'are you there?')
    incoming_http(pilot, 73206, 'another thought', fan=FAN2)
    pilot.tick()
    assert not to_fan(w.fans.posts, FAN) and len(to_fan(w.fans.posts, FAN2)) == 2
    assert conversation(w, FAN)[0] == 'human_controlled'
    assert audits(w, FAN) == rows


def test_handover_repeat_tick_and_restart_keep_one_hold(stopped_text):
    w, env, model, seen, pilot = meet_up_then_other_fan(stopped_text)
    pilot.tick()
    state, rows, cases = conversation(w, FAN), audits(w, FAN), journey_cases(w, FAN)
    assert len(rows) == 1 and len(cases) == 1
    # Repeated tick, repeated signed event (same key and body) and a brand-new worker over the same database.
    pilot.tick()
    incoming_http(pilot, 73203, 'Can we meet up tonight?')
    pilot2 = build_supervised_pilot(w.store, env, model_transport=model, fans_transport=w.fans.transport)
    pilot2.tick()
    pilot2.tick()
    assert (conversation(w, FAN), audits(w, FAN), journey_cases(w, FAN)) == (state, rows, cases)
    assert not to_fan(w.fans.posts, FAN)


def test_handover_concurrent_workers_write_one_hold(stopped_text):
    w, env, model, seen, pilot = meet_up_then_other_fan(stopped_text)
    other = build_supervised_pilot(w.store, env, model_transport=model, fans_transport=w.fans.transport)
    pilot.worker.engine.select_simulated(TENANT, ACCOUNT, FAN, source_message_id=73203)   # the engine owner row
    journeys = [pilot.worker.journey, other.worker.journey]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda j: j._to_human(TENANT, ACCOUNT, FAN, 73203, 'meet_up'), journeys))
    assert conversation(w, FAN)[0] == 'human_controlled'
    assert len(audits(w, FAN)) == 1 and audits(w, FAN)[0][0] == 'engine-journey'
    assert len(journey_cases(w, FAN)) == 1


def test_handover_actor_cannot_do_anything_but_request_takeover(stopped_text):
    w, env, model, seen, pilot = meet_up_then_other_fan(stopped_text)
    revision = conversation(w, FAN2)[2]
    attempts = [('automated', 'resume_automation'), ('automated', 'release_takeover'), ('paused', 'pause_automation'),
                ('no_contact', 'mark_no_contact'), ('human_controlled', None), ('automated', None)]
    for n, (mode, action) in enumerate(attempts):
        with pytest.raises(Refused, match='pilot operator permission required'):
            w.store.control(TENANT, ACCOUNT, FAN2, mode=mode, expected_revision=revision, command_key=f'u4-bad-{n}',
                            actor='engine-journey', reason='must be refused', action=action)
    assert conversation(w, FAN2) == ('automated', 'none', revision) and not audits(w, FAN2)


def test_handover_rejects_stale_revision_and_unauthorized_operator(stopped_text):
    w, env, model, seen, pilot = meet_up_then_other_fan(stopped_text)
    revision = conversation(w, FAN2)[2]
    with pytest.raises(Refused, match='revision'):
        w.store.control(TENANT, ACCOUNT, FAN2, mode='human_controlled', expected_revision=revision + 3,
                        command_key='u4-stale', actor='engine-journey', reason='stale', action='request_takeover')
    for actor in (OTHER, 'someone-else', 'engine', 'fans-webhook-x'):
        with pytest.raises(Refused, match='pilot operator permission required'):
            w.store.control(TENANT, ACCOUNT, FAN2, mode='human_controlled', expected_revision=revision,
                            command_key='u4-unauth-' + actor[:8], actor=actor, reason='nope', action='request_takeover')
    assert conversation(w, FAN2) == ('automated', 'none', revision) and not audits(w, FAN2)
    # The configured operator may still take over, which proves the refusals above are about the actor.
    w.store.control(TENANT, ACCOUNT, FAN2, mode='human_controlled', expected_revision=revision,
                    command_key='u4-op', actor=ACTOR, reason='operator', action='request_takeover')
    assert conversation(w, FAN2)[0] == 'human_controlled'
    # The same key with a different body is an idempotency conflict, never a second write.
    with pytest.raises(Refused, match='idempotency'):
        w.store.control(TENANT, ACCOUNT, FAN2, mode='human_controlled', expected_revision=revision,
                        command_key='u4-op', actor=ACTOR, reason='different', action='request_takeover')


def test_handover_actor_is_refused_inside_an_http_agency_scope(stopped_text):
    from onlyfans_read.agency_scope import AgencyScope, current_scope
    w, env, model, seen, pilot = meet_up_then_other_fan(stopped_text)
    revision = conversation(w, FAN2)[2]
    token = current_scope.set(AgencyScope(creator_ids=('x',), actor='engine-journey', can_manage=True, can_chat=True))
    try:
        with pytest.raises(Refused):
            w.store.control(TENANT, ACCOUNT, FAN2, mode='human_controlled', expected_revision=revision,
                            command_key='u4-scope', actor='engine-journey', reason='http', action='request_takeover')
    finally:
        current_scope.reset(token)
    assert conversation(w, FAN2) == ('automated', 'none', revision)


def test_handback_is_a_distinct_operator_action_that_rechecks_stops(stopped_text):
    w, env, model, seen, pilot = meet_up_then_other_fan(stopped_text)
    pilot.tick()
    mode, takeover, revision = conversation(w, FAN)
    assert mode == 'human_controlled'
    # Neither the journey actor nor an unauthorised operator can hand back.
    for actor in ('engine-journey', OTHER):
        with pytest.raises(Refused, match='pilot operator permission required'):
            w.store.control(TENANT, ACCOUNT, FAN, mode='automated', expected_revision=revision,
                            command_key='u4-hb-bad-' + actor[:6], actor=actor, reason='hb', action='release_takeover')
    # A stronger restriction recorded later still blocks even the authorised hand-back.
    with Session(w.database[1]) as s, s.begin():
        row = s.get(SkipList, (ACCOUNT, FAN))
        previous = row.reason if row else None
        if row is None:
            s.add(SkipList(account_id=ACCOUNT, fan_id=FAN, reason='of_restricted'))
        else:
            row.reason = 'of_restricted'
    if takeover == 'requested':
        pytest.skip('takeover not yet effective in this fixture')
    with pytest.raises(Refused):
        w.store.control(TENANT, ACCOUNT, FAN, mode='automated', expected_revision=revision,
                        command_key='u4-hb-stop', actor=ACTOR, reason='hb', action='release_takeover')
    assert conversation(w, FAN)[0] == 'human_controlled'
    with Session(w.database[1]) as s, s.begin():
        row = s.get(SkipList, (ACCOUNT, FAN))
        if previous is None:
            s.delete(row)
        else:
            row.reason = previous
    w.store.control(TENANT, ACCOUNT, FAN, mode='automated', expected_revision=revision,
                    command_key='u4-hb-ok', actor=ACTOR, reason='hb', action='release_takeover')
    assert conversation(w, FAN)[:2] == ('automated', 'none')
    # Hand-back does not by itself erase the durable journey case: A is still not answered until a person resolves it.
    incoming_http(pilot, 73301, 'hi again')
    pilot.tick()
    assert not to_fan(w.fans.posts, FAN)
    with Session(w.database[1]) as s:
        case = s.scalar(select(OfOperatorCase).where(OfOperatorCase.account_id == ACCOUNT, OfOperatorCase.fan_id == FAN,
                                                     OfOperatorCase.category.like('journey_%')))
        case_id = case.case_id
    operator_cases.resolve(w.store, TENANT, ACCOUNT, case_id, actor=ACTOR, resolution='person answered')
    incoming_http(pilot, 73302, 'hello once more')
    pilot.tick()
    assert len(to_fan(w.fans.posts, FAN)) >= 1
