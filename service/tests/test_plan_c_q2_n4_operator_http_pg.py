"""Q2 journey H (N4): authenticated operator HTTP stop / takeover / scoped handback in the AUTONOMOUS text-only profile.

Real routes (onlyfans_commands.engine_approval_api, contract of-engine-operator.v2) on a FastAPI app over the real
store; bearer token, schema header, strict JSON; the pilot scope's own operator actor. Profile: stopped_text ->
text_no_offers (autonomous, text_only), signed intake, normal pilot.tick(). Real PostgreSQL, fake model and provider.
Nothing is seeded: state changes come only from the HTTP commands, signed events and the audited reconcile.
Every refusal is checked for NO state change (conversation row, control audit, sends, model calls).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import OfConversationState
from db.onlyfans_models import OfControlAudit
from onlyfans_commands.engine_approval_api import HEADER, SCHEMA, router
from test_engine_real_worker_pg import FAN2, inbound, world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401
from test_plan_c_events_stage3_pg import rig, tip_event  # noqa: F401
from test_plan_c_scope_pg import ACTOR, OTHER
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401

TOKEN = 'fixture-control-token'
HEADERS = {'Authorization': f'Bearer {TOKEN}', HEADER: SCHEMA}


def key(n):
    return f'{n:064x}'


class Ops:
    def __init__(self, rig):
        self.rig = rig
        self.app = FastAPI()
        self.app.state.control_store = rig.w.store
        self.app.include_router(router)

        @self.app.middleware('http')
        async def agency_scope(request, call_next):          # stands in for AgencyScopeMiddleware on a scoped request
            if request.headers.get('x-test-agency-scope'):
                from onlyfans_read.agency_scope import AgencyScope, current_scope
                current_scope.set(AgencyScope(('11111111-1111-1111-1111-111111111111',),
                    'agency-user:22222222-2222-2222-2222-222222222222:33333333-3333-3333-3333-333333333333', True, True))
            return await call_next(request)

    def post(self, fan, action, body, headers=HEADERS):
        with pytest.MonkeyPatch.context() as env:            # the API role only for the HTTP call, never for the worker
            env.setenv('ONLYFANS_PROCESS_ROLE', 'api')
            env.setenv('ONLYFANS_CONTROL_TOKEN', TOKEN)
            env.setenv('ONLYFANS_CONTROL_TENANT', TENANT)
            for name in ('ONLYFANS_API_TOKEN', 'ONLYFANS_CONFIGURATION_TOKEN', 'ONLYFANS_CAMPAIGN_TOKEN',
                         'ONLYFANS_CONTENT_TOKEN', 'ONLYFANS_CONNECTION_TOKEN'):
                env.delenv(name, raising=False)
            with TestClient(self.app) as client:
                return client.post(f'/onlyfans/control/v2/accounts/{ACCOUNT}/conversations/{ACCOUNT}:{fan}/{action}',
                                   headers=headers, json=body)

    def command(self, fan, action, n, revision, actor=ACTOR, headers=HEADERS):
        return self.post(fan, action, {'commandKey': key(n), 'actor': actor, 'expectedRevision': revision}, headers)

    def state(self, fan):
        with Session(self.rig.w.database[1]) as s:
            st = s.get(OfConversationState, (ACCOUNT, fan))
            audits = s.scalar(select(func.count()).select_from(OfControlAudit).where(OfControlAudit.account_id == ACCOUNT))
            return (st.mode, st.takeover_state, st.revision), audits


def counts(rig):
    return len(rig.w.fans.posts), len(rig.seen)


def to_fan(rig, fan):
    return [p for p in rig.w.fans.posts if f'/{fan}/' in p['path']]


@pytest.fixture
def ops(rig):
    return Ops(rig)


def test_rejected_http_commands_change_nothing_and_a_current_stop_pauses_only_that_fan(rig, ops):
    rig.fans_in()
    rig.ticks()
    assert len(to_fan(rig, FAN)) == 1 and len(to_fan(rig, FAN2)) == 1, 'control: both fans served before any command'
    (mode, _, revision), audits = ops.state(FAN)
    assert mode == 'automated'
    rig.post(inbound(80501, 'are you still there?', fan=FAN), key='n4-a')
    rig.post(inbound(80502, 'hello again', fan=FAN2), key='n4-b')
    before_state, before_counts = ops.state(FAN), counts(rig)

    good = {'commandKey': key(1), 'actor': ACTOR, 'expectedRevision': revision}
    rejected = {
        'no bearer': ops.post(FAN, 'stop', good, headers={HEADER: SCHEMA}),
        'wrong bearer': ops.post(FAN, 'stop', good, headers={**HEADERS, 'Authorization': 'Bearer wrong'}),
        'wrong schema': ops.post(FAN, 'stop', good, headers={**HEADERS, HEADER: 'of-engine-operator.v1'}),
        'agency scoped': ops.post(FAN, 'stop', good, headers={**HEADERS, 'x-test-agency-scope': '1'}),
        'not the pilot operator': ops.command(FAN, 'stop', 2, revision, actor=OTHER),
        'wrong (future) revision': ops.command(FAN, 'stop', 4, revision + 5),
        'bool revision': ops.command(FAN, 'stop', 5, True),
        'extra field': ops.post(FAN, 'stop', {**good, 'extra': 1}),
        'engine actor': ops.command(FAN, 'stop', 6, revision, actor='engine'),
        'outside the pilot': ops.command(999000111, 'stop', 7, revision),
    }
    codes = {name: r.status_code for name, r in rejected.items()}
    assert codes == {'no bearer': 401, 'wrong bearer': 401, 'wrong schema': 400, 'agency scoped': 403,
                     'not the pilot operator': 409, 'wrong (future) revision': 409, 'bool revision': 400,
                     'extra field': 400, 'engine actor': 400, 'outside the pilot': 404}, codes
    assert ops.state(FAN) == before_state and counts(rig) == before_counts, 'a rejected command changes nothing'

    ok = ops.command(FAN, 'stop', 10, revision)
    assert ok.status_code == 200 and ok.json()['recordedRevision'] == revision + 1 and ok.json()['replayed'] is False
    assert ops.command(FAN, 'stop', 10, revision).json()['replayed'] is True
    assert ops.command(FAN, 'stop', 11, revision).status_code == 409, 'the old revision is stale after the stop'
    assert ops.command(FAN, 'stop', 10, revision + 1).status_code == 409, 'a command key cannot be reused for another revision'
    assert ops.state(FAN)[0][0] == 'paused'
    rig.ticks(4)
    assert len(to_fan(rig, FAN)) == 1, 'the stopped fan got nothing more'
    assert len(to_fan(rig, FAN2)) == 2 and len(rig.seen) == before_counts[1] + 1, 'control: the other fan progressed'


def test_takeover_then_stale_and_unauthorised_handback_change_nothing_and_a_current_handback_sends_nothing_itself(rig, ops):
    rig.fans_in()
    rig.ticks()
    (_, _, revision), _ = ops.state(FAN)
    take = ops.command(FAN, 'takeover', 20, revision)
    assert take.status_code == 200 and take.json()['recordedRevision'] == revision + 1
    taken = ops.state(FAN)
    assert taken[0][0] == 'human_controlled'
    rig.post(inbound(80601, 'you there?', fan=FAN), key='n4-c')
    rig.post(inbound(80602, 'hello', fan=FAN2), key='n4-d')
    rig.ticks(4)
    assert len(to_fan(rig, FAN)) == 1 and len(to_fan(rig, FAN2)) == 2, 'a person has FAN: no send; FAN2 progressed'
    after_take = counts(rig)

    stale = ops.command(FAN, 'handback', 21, revision)                       # the revision before the takeover
    wrong_actor = ops.command(FAN, 'handback', 22, taken[0][2], actor=OTHER)
    bad_bearer = ops.post(FAN, 'handback', {'commandKey': key(23), 'actor': ACTOR, 'expectedRevision': taken[0][2]},
                          headers={**HEADERS, 'Authorization': 'Bearer nope'})
    assert (stale.status_code, wrong_actor.status_code, bad_bearer.status_code) == (409, 409, 401)
    assert ops.state(FAN) == taken and counts(rig) == after_take, 'refused handbacks change nothing'
    rig.ticks(2)
    assert counts(rig) == after_take

    back = ops.command(FAN, 'handback', 24, taken[0][2])
    assert back.status_code == 200 and back.json()['recordedRevision'] == taken[0][2] + 1
    assert ops.command(FAN, 'handback', 24, taken[0][2]).json()['replayed'] is True
    assert ops.command(FAN, 'handback', 25, taken[0][2]).status_code == 409, 'the same revision cannot hand back twice'
    assert ops.state(FAN)[0][:2] == ('automated', 'none')
    assert counts(rig) == after_take, 'handback itself sends nothing and makes no model call'
    rig.post(inbound(80603, 'so, anything?', fan=FAN), key='n4-e')
    rig.ticks(4)
    assert len(to_fan(rig, FAN)) >= 2, 'control: after handback and a new message the engine plans afresh and answers'


def test_handback_does_not_clear_a_contact_stop_or_a_standing_evidence_hold(rig, ops):
    rig.fans_in()
    rig.ticks()
    # (1) A fan who asked for no contact is not handed back by the operator surface.
    rig.post(inbound(80701, 'please stop texting me', fan=FAN), key='n4-stop')
    rig.ticks(2)
    state = ops.state(FAN)
    assert state[0][0] == 'no_contact'
    refused = ops.command(FAN, 'handback', 30, state[0][2])
    assert refused.status_code == 409 and ops.state(FAN) == state
    rig.post(inbound(80702, 'hello??', fan=FAN), key='n4-stop-2')
    posts = len(rig.w.fans.posts)
    rig.ticks(3)
    assert len(rig.w.fans.posts) == posts, 'no contact stays no contact'

    # (2) A standing evidence hold on a taken-over fan survives the handback: the handback is a control change only.
    (_, _, revision), _ = ops.state(FAN2)
    assert ops.command(FAN2, 'takeover', 31, revision).status_code == 200
    rig.post(tip_event(FAN2, tid='n4-tip'), key='n4-tip')
    assert rig.block(FAN2) == 'pilot_evidence_pending'
    (_, _, revision), _ = ops.state(FAN2)
    assert ops.command(FAN2, 'handback', 32, revision).status_code == 200
    assert ops.state(FAN2)[0][:2] == ('automated', 'none')
    rig.post(inbound(80703, 'back again', fan=FAN2), key='n4-after-handback')
    seen, posts = len(rig.seen), len(to_fan(rig, FAN2))
    rig.ticks(4)
    assert rig.block(FAN2) == 'pilot_evidence_pending' and len(to_fan(rig, FAN2)) == posts and len(rig.seen) == seen, \
        'handback did not release the evidence hold: no send and no model call'
    [item] = rig.items('provider_event')
    raw = rig.w.store and None
    # Released only by the audited review, after which the fan is served.
    with Session(rig.w.database[1]) as s:
        from db.onlyfans_engine_models import OfEnginePilotReconnectReceipt
        raw = bytes(s.scalar(select(OfEnginePilotReconnectReceipt.raw_body).where(OfEnginePilotReconnectReceipt.event_key == 'n4-tip')))
    assert rig.reconcile(item, 900, raw=raw)['resolved']
    rig.ticks(4)
    assert len(to_fan(rig, FAN2)) == posts + 1
