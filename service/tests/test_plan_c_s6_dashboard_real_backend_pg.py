"""V5 section 6: the ACTUAL dashboard Plan C component (twin dashboard 3cedc8f, React in jsdom) against the REAL backend.

Chain: jsdom + PlanCConsole.tsx + plan-c-client.ts  ->  the dashboard's server-side handlers (plan-c-route.ts incl. planCOperatorOnly,
plan-c-backend.ts)  ->  REAL HTTP over loopback  ->  the real FastAPI app `onlyfans_app:app` (role api, control token) in a separate
process  ->  real PostgreSQL (the rig's disposable database).  The pilot worker, signed intake, fake model and fake FansAPI live in
this process, so state changes come from real ticks and real signed events; every UI action is checked against durable rows.

Needs the node toolchain mounted into the container (see evidence/v5-s6/run-pg-dash.sh): node at /usr/local/bin/node and a copy of
the dashboard at /work (read only). Without it the test is SKIPPED, never faked. Driver protocol: journey.tsx prints `SYNC {json}`
after each step and waits for `go`.

Not exercised (named in DASHBOARD-FINDINGS.md): the Next route files' own session-cookie verification (requireOnlyFansRead,
planCOwnerActor) and same-origin checks; a real browser. The session/identity is modelled by feeding the REAL planCOperatorOnly the
context the route builds.
"""
import json
import os
import select as io_select
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conversation_engine import operator_v2
from db.models import OfConversationState
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEnginePilotReconnectReceipt
from db.onlyfans_models import OfControlAudit
from test_engine_real_worker_pg import FAN2, inbound, world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft  # noqa: F401
from test_plan_c_events_stage3_pg import rig, tip_event  # noqa: F401
from test_plan_c_q2_n2_crash_recovery_pilot_pg import Crash, expire_dispatching, restart
from test_plan_c_scope_pg import ACTOR, OTHER
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401

NODE, WORK = '/usr/local/bin/node', '/work'
HARNESS = WORK + '/tests/s6/journey.tsx'
pytestmark = pytest.mark.skipif(not (os.path.exists(NODE) and os.path.isfile(HARNESS)),
                                reason='dashboard toolchain not mounted: run through evidence/v5-s6/run-pg-dash.sh')
CONTROL_TOKEN = 'fixture-control-token-s6'


def key(n):
    return f'{n:064x}'


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def conv(fan):
    return f'{ACCOUNT}:{fan}'


class Backend:
    """The real app in its own process: role api, the control credential, the rig's database."""
    def __init__(self, rig):
        self.port = free_port()
        self.base = f'http://127.0.0.1:{self.port}'
        self.log = open('/tmp/s6-api.log', 'w')
        env = {'PATH': os.environ['PATH'], 'HOME': '/tmp', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': os.environ['PYTHONPATH'],
               'TWIN_FASTT_CONFIG_ISOLATED': '1', 'DATABASE_URL': rig.w.database[0], 'ONLYFANS_PROCESS_ROLE': 'api',
               'ONLYFANS_API_TOKEN': 'fixture-api-token-s6', 'ONLYFANS_API_TENANT': TENANT, 'ONLYFANS_CONTROL_TOKEN': CONTROL_TOKEN,
               'ONLYFANS_CONTROL_TENANT': TENANT, 'ENGINE_MODEL_PROFILES_FILE': rig.env['ENGINE_MODEL_PROFILES_FILE'],
               'TWIN_ENGINE_FANSAPI_REAL': '0', 'TWIN_PLAN_C_FIXTURE_ONLY': '1'}
        self.proc = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'onlyfans_app:app', '--host', '127.0.0.1', '--port', str(self.port),
                                      '--log-level', 'warning'], cwd='/app/service', env=env, stdout=self.log, stderr=subprocess.STDOUT)
        end = time.monotonic() + 120
        while True:
            assert self.proc.poll() is None, 'backend exited: ' + Path('/tmp/s6-api.log').read_text()[-2000:]
            try:
                with urllib.request.urlopen(self.base + '/healthz', timeout=2) as r:
                    if r.status < 500:
                        return
            except Exception:
                assert time.monotonic() < end, 'backend never became ready'
                time.sleep(.5)

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


class Journey:
    def __init__(self, backend):
        env = {'PATH': os.environ['PATH'], 'HOME': '/tmp', 'NODE_OPTIONS': '--max-old-space-size=700', 'NEXT_TELEMETRY_DISABLED': '1',
               'API_BASE': backend.base, 'CONTROL_TOKEN': CONTROL_TOKEN, 'ACCOUNT': ACCOUNT, 'PILOT_ACTOR': ACTOR, 'OTHER_ACTOR': OTHER,
               'FAN_CONV': conv(FAN), 'FAN2_CONV': conv(FAN2)}
        self.proc = subprocess.Popen([NODE, '--import', 'tsx', HARNESS], cwd=WORK, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    def step(self, name, timeout=300):
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            assert left > 0 and io_select.select([self.proc.stdout], [], [], left)[0], f'node harness silent waiting for {name}'
            line = self.proc.stdout.readline()
            assert line, f'node harness exited before {name}: rc={self.proc.poll()}'
            if line.startswith('SYNC '):
                event = json.loads(line[5:])
                assert event['name'] == name, f'expected {name}, got {event["name"]}'
                print('SYNC', name, json.dumps(event, indent=1)[:6000])
                return event
            print('node:', line.rstrip())
            assert not line.startswith('FAILED'), line

    def go(self):
        self.proc.stdin.write('go\n')
        self.proc.stdin.flush()

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def state(rig, fan):
    with Session(rig.w.database[1]) as s:
        st = s.get(OfConversationState, (ACCOUNT, fan))
        audits = s.scalar(select(func.count()).select_from(OfControlAudit).where(OfControlAudit.account_id == ACCOUNT))
        return (st.mode, st.takeover_state, st.revision), audits


def backend_items(rig):
    return operator_v2.unresolved(rig.w.store, TENANT, ACCOUNT, ACTOR)


def dom_ids(event):
    return {u['id'] for u in event['snap']['unresolved']}


def posts_models(rig):
    return len(rig.w.fans.posts), len(rig.seen)


def arrange(rig):
    """FAN: a send that reached the wire and has no answer (unknown). FAN2: an oversize message the engine hands to a human."""
    rig.post(inbound(79701, 'hello, how was your day?', fan=FAN), key='s6-a')
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
    expire_dispatching(rig)
    restart(rig)
    rig.post(inbound(79702, 'ZZ-FAN2-OVERSIZE tell you about my week. ' * 200, fan=FAN2), key='s6-b')
    rig.ticks(5)


def test_actual_dashboard_component_against_the_real_backend_stop_takeover_handback_stale_unauthorized_held_work_and_case_resolution(rig):
    arrange(rig)
    items = backend_items(rig)
    kinds = {i['id']: i for i in items}
    print('BACKEND ITEMS BEFORE', json.dumps([(i['id'][:60], i['kind'], i.get('conversationId'), i.get('revision'), i['nextStep']) for i in items]))
    assert any(i['kind'] == 'unknown_send' and i.get('conversationId') == conv(FAN) for i in items), 'precondition: FAN holds an unknown send'
    assert any(i.get('conversationId') == conv(FAN2) for i in items), 'precondition: FAN2 has operator-visible held work'
    base_fan, base_fan2 = state(rig, FAN), state(rig, FAN2)
    base_counts = posts_models(rig)
    assert base_fan[0][0] == 'automated' and base_fan2[0][0] == 'automated'

    backend = Backend(rig)
    node = Journey(backend)
    try:
        # ---- 1. held work display ----------------------------------------------------------------------------------------
        e = node.step('display')
        ui = {u['id']: u for u in e['snap']['unresolved']}
        assert set(ui) == set(kinds), f'the screen shows exactly the backend\'s unresolved items: {set(ui) ^ set(kinds)}'
        for item in items:
            row = ui[item['id']]
            assert item['nextStep'].replace('_', ' ') in row['text'] and 'Owner: ' + item['owner'] in row['text']
        assert {c['id'] for c in e['snap']['conversations']} >= {conv(FAN), conv(FAN2)}
        assert e['snap']['unresolvedError'] is None and e['snap']['draftsError'] is None
        assert state(rig, FAN) == base_fan and state(rig, FAN2) == base_fan2 and posts_models(rig) == base_counts, 'viewing changes nothing'
        node.go()

        # ---- 2. unauthorised / unauthenticated / wrong credential / other actor -----------------------------------------------
        e = node.step('unauthorized')
        results = {r['name']: r for r in e['refused']}
        codes = {n: [(c['method'], c['status'], c['backend']) for c in r['snap']['calls'] if c['method'] == 'POST'] for n, r in results.items()}
        print('UNAUTHORIZED CODES', json.dumps(codes))
        assert codes['agency session'] == [('POST', 403, None)] and codes['view-as session'] == [('POST', 403, None)]
        assert codes['no session'] == [('POST', 401, None)] and codes['read-only operator (cannot manage)'] == [('POST', 403, None)]
        assert codes['wrong backend credential'] == [('POST', 400, 401)], codes['wrong backend credential']
        assert codes['another operator actor'][0][0] == 'POST' and codes['another operator actor'][0][1] in (400, 409)
        assert all(c[1] != 200 for v in codes.values() for c in v), 'no refused command was recorded'
        assert state(rig, FAN) == base_fan and state(rig, FAN2) == base_fan2 and posts_models(rig) == base_counts, \
            'every refused command changed nothing'
        for button in [b for c in e['viewer']['conversations'] for b in c['actions']]:
            assert button['disabled'] is True, 'a viewer who cannot act has every control disabled'
        node.go()

        # ---- 3. authorised stop (fresh revision) ----------------------------------------------------------------------------
        e = node.step('stop')
        assert e['stop'] == {'present': True, 'enabled': True}
        (mode, take, rev), audits = state(rig, FAN)
        assert mode == 'paused' and rev == base_fan[0][2] + 1 and audits == base_fan[1] + 1, state(rig, FAN)
        assert [c for c in e['snap']['calls'] if c['method'] == 'POST'][0]['backend'] == 200
        rig.post(inbound(79711, 'you there?', fan=FAN), key='s6-c')
        rig.ticks(4)
        assert posts_models(rig) == base_counts, 'a stopped fan gets nothing and costs no model call'
        assert state(rig, FAN2)[0] == base_fan2[0], 'the stop was scoped to one fan'
        node.go()

        # ---- 4. authorised takeover ------------------------------------------------------------------------------------------
        e = node.step('takeover')
        assert e['takeover'] == {'present': True, 'enabled': True}
        (mode, take, rev2), _ = state(rig, FAN2)
        assert mode == 'human_controlled' and rev2 == base_fan2[0][2] + 1
        assert any(i['kind'] == 'human_hold' and i.get('conversationId') == conv(FAN2) for i in backend_items(rig))
        assert any(u['id'].startswith('human_hold:') for u in e['snap']['unresolved']), 'the human hold is displayed'
        node.go()

        # ---- 5. stale revision --------------------------------------------------------------------------------------------------
        node.step('stale-ready')
        out = operator_v2.conversation_command(rig.w.store, TENANT, ACCOUNT, FAN2, 'handback',
                                               {'commandKey': key(5001), 'actor': ACTOR, 'expectedRevision': rev2})
        assert out['recordedRevision'] == rev2 + 1
        after_oob = state(rig, FAN2)
        assert after_oob[0][0] == 'automated'
        node.go()
        e = node.step('stale')
        post = [c for c in e['snap']['calls'] if c['method'] == 'POST']
        assert post and post[0]['status'] == 409 and post[0]['backend'] == 409, post
        assert 'changed since you loaded it' in (e['snap']['notice'] or '') or any(
            'changed since' in (c['status'] or '') for c in e['snap']['conversations']), e['snap']
        assert state(rig, FAN2) == after_oob, 'the stale hand back changed nothing'
        node.go()

        # ---- 6. authorised takeover then hand back with the fresh revision ------------------------------------------------------
        e = node.step('takeover-2')
        assert state(rig, FAN2)[0][0] == 'human_controlled'
        node.go()
        e = node.step('handback')
        assert e['back'] == {'present': True, 'enabled': True}
        assert state(rig, FAN2)[0][:2] == ('automated', 'none')
        remaining = {i['id']: i for i in backend_items(rig)}
        assert any(i['kind'] != 'human_hold' and i.get('conversationId') == conv(FAN2) for i in remaining.values()), \
            'hand back did not erase the independent case for FAN2'
        assert not any(i['kind'] == 'human_hold' and i.get('conversationId') == conv(FAN2) for i in remaining.values())
        assert posts_models(rig) == base_counts
        rig.ticks(4)
        assert posts_models(rig) == base_counts, 'a hand back sends nothing and makes no model call; the oversize case still waits for a person'
        node.go()

        # ---- 7. an independent evidence hold, displayed, then resolved through its own audited path --------------------------------
        node.step('event-ready')
        rig.post(tip_event(FAN2, tid='s6-tip'), key='s6-tip')
        rig.ticks(3)
        assert rig.block(FAN2) == 'pilot_evidence_pending'
        node.go()
        e = node.step('event-shown')
        events = [u for u in e['snap']['unresolved'] if u['id'].startswith('provider_event:')]
        assert events, f'the evidence hold is displayed: {[u["id"] for u in e["snap"]["unresolved"]]}'
        assert all(not u['reconcile'] and 'Review this item using its next step' in u['text'] for u in events), \
            'the dashboard offers no action for a provider-event hold: it is resolved only through the audited operator path'
        assert any(u['id'].startswith('unknown_send:') for u in e['snap']['unresolved'])
        node.go()
        e = node.step('evidence-note')
        assert e['hadInput'], 'the unknown send offers an evidence note'
        notes = [c for c in e['snap']['calls'] if c['method'] == 'POST']
        print('EVIDENCE NOTE CALLS', json.dumps(notes))
        assert notes and notes[0]['status'] == 200 and notes[0]['backend'] == 200
        still = [u for u in e['snap']['unresolved'] if u['id'].startswith('unknown_send:')]
        assert still and 'awaiting provider proof' in still[0]['text'], 'an evidence note never resolves the send'
        with Session(rig.w.database[1]) as s:
            assert [i.status for i in s.scalars(select(OfTextIntent).where(OfTextIntent.fan_id == FAN))] == ['unknown']
        assert rig.block(FAN2) == 'pilot_evidence_pending' and posts_models(rig) == base_counts
        node.go()

        node.step('resolved-elsewhere')
        [item] = [i for i in backend_items(rig) if i['kind'] == 'provider_event']
        with Session(rig.w.database[1]) as s:
            raw = bytes(s.scalar(select(OfEnginePilotReconnectReceipt.raw_body).where(OfEnginePilotReconnectReceipt.event_key == 's6-tip')))
        assert rig.reconcile(item, 5100, raw=raw)['resolved']
        assert rig.block(FAN2) is None
        node.go()
        e = node.step('final')
        ids = dom_ids(e)
        assert not any(i.startswith('provider_event:') for i in ids), 'the evidence hold is gone once it is resolved'
        assert any(i.startswith('unknown_send:') for i in ids), 'the unknown send is a separate case and remains'
        assert any(i in ids for i in remaining), 'the independent FAN2 case remains'
        assert state(rig, FAN)[0][0] == 'paused'
        node.go()
        assert node.proc.wait(timeout=60) == 0
    finally:
        node.close()
        backend.close()
