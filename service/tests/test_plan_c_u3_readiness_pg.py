"""U3 (plan Step 3): scope-aware readiness through intake, ticks, /readyz and the ACTUAL pinned host guard.

Real here: signed HTTP intake, the worker loop and /readyz, PostgreSQL scope/holds, ops/plan-c-supervision/supervisor.py
check()/run(), gated_worker.permitted, and guard_core.py loaded from the byte-pinned host copy (sha256 asserted).
Mocked (labelled): provider webhook-health listing, FansAPI usage/credits, sender-process identity, the root-owned
receiver /readyz, and the docker-exec psql read (replaced by the same query on the lab DB).
"""
import contextlib
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import OfEnginePilotScope
from db.onlyfans_models import OfRuntime
from plan_c_worker import create_app
from test_engine_real_worker_pg import FAN2, inbound, world  # noqa: F401
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_draft_app_pg import configured_draft, signed  # noqa: F401
from test_plan_c_events_stage3_pg import Rig, ev, tip_event, queue_event, K, EVIDENCE  # noqa: F401
from test_plan_c_scope_pg import ACTOR
from test_plan_c_text_activation_pg import stopped_text  # noqa: F401

SUP = Path(__file__).resolve().parents[2] / 'ops' / 'plan-c-supervision'
PINNED = SUP / 'guard_core.py'
PINNED_SHA256 = 'c2be833f7191bf5a94f3c1f841e09effac62cf5a0b03b751663545e9af905797'


def _load(name, path, **modules):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    saved = {k: sys.modules.get(k) for k in modules}
    sys.modules.update(modules)
    try:
        spec.loader.exec_module(module)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return module


@pytest.fixture(scope='module')
def guard():
    assert hashlib.sha256(PINNED.read_bytes()).hexdigest() == PINNED_SHA256, 'pinned host guard bytes changed'
    return _load('u3_pinned_guard_core', PINNED)


@pytest.fixture(scope='module')
def sup(guard):
    return _load('u3_supervisor', SUP / 'supervisor.py', guard_core=guard)


@pytest.fixture
def rig(stopped_text):
    from test_plan_c_text_no_offers_pg import text_no_offers  # noqa: F401  (Rig builds it)
    r = Rig(stopped_text)
    try:
        yield r
    finally:
        r.close()


@contextlib.contextmanager
def live(rig, *, want='ready', timeout=60):
    """The real worker loop and /readyz over the rig's pilot. Yields (client, readyz json, status)."""
    with TestClient(create_app(rig.pilot)) as client:
        end = time.monotonic() + timeout
        while True:
            r = client.get('/readyz')
            if r.json()['code'] == want and (want != 'ready' or r.json()['ticks'] >= 1):
                break
            assert time.monotonic() < end, r.json()
            time.sleep(.1)
        yield client, r.json(), r.status_code


def wait_posts(rig, n, client, timeout=60):
    end = time.monotonic() + timeout
    while len(rig.w.fans.posts) < n:
        assert time.monotonic() < end, (len(rig.w.fans.posts), client.get('/readyz').json())
        time.sleep(.1)


def settle(client, rig, ticks=2):
    """Let the real loop run a few more full ticks so nothing late can slip through unobserved."""
    start = client.get('/readyz').json()['ticks']
    end = time.monotonic() + 60
    while client.get('/readyz').json()['ticks'] < start + ticks:
        assert time.monotonic() < end
        time.sleep(.1)
    return client.get('/readyz')


def to_fan(posts, fan):
    return [p for p in posts if f'/{fan}/' in p['path'] or p['path'].endswith(f'/{fan}')]


class Host:
    """supervisor.check()/run() over real readiness and real guard; only host-only inputs are mocked."""
    def __init__(self, sup, rig, client, tmp_path, monkeypatch):
        self.sup, self.rig, self.client, self.mp = sup, rig, client, monkeypatch
        self.team = (100 + 5, 1000 - 5)          # (team total credits, team balance) vs baseline (100, 1000)
        self.webhook = lambda deadline=None: True
        self.identity = lambda: None
        self.hard = None
        self.log = []
        mp = monkeypatch
        mp.setattr(sup, 'readjson', lambda p: {'team_credits_observed': 100, 'team_balance': 1000, 'start': '2026-11-01'}
                   if p == sup.BASELINE else json.loads(Path(p).read_text()))
        mp.setattr(sup, 'db_state', self.db_state)
        mp.setattr(sup, 'external_reservations', lambda: 38)
        mp.setattr(sup, 'status_request', self.status_request)
        mp.setattr(sup, 'team_usage', lambda baseline: (self.team[0], self.team[1], 0))
        mp.setattr(sup, 'webhook_health', lambda deadline=None: self.webhook(deadline))
        mp.setattr(sup, 'sender_identity', lambda: self.identity())

    def db_state(self):
        with Session(self.rig.w.database[1]) as s:
            scope = s.get(OfEnginePilotScope, 1)
            return {'status': scope.status, 'revision': scope.revision, 'reserved': scope.provider_credits_reserved,
                    'runtime': s.get(OfRuntime, ACCOUNT).enabled, 'policy': s.get(OfDeliveryPolicy, ACCOUNT).enabled}

    def status_request(self, url, method='GET', headers=None, *, timeout=6):
        if url == self.sup.PUBLIC or url == self.sup.LOCAL + '/onlyfans/webhooks/plan-c':
            return 401, b''
        if url == self.sup.LOCAL + '/readyz':      # root-owned receiver (mock): intake-only, as the supervisor requires
            if self.db_state()['status'] == 'active':
                return 503, json.dumps({'ok': False, 'mode': 'intake_only', 'code': 'restart_required',
                                        'consecutiveFailures': 0, 'work': {}}).encode()
            return 200, json.dumps({'ok': True, 'mode': 'intake_only', 'code': 'ready'}).encode()
        assert url == self.sup.SENDER + '/readyz', url
        r = self.client.get('/readyz')
        return r.status_code, r.content

    def check(self, state=None):
        return self.sup.check(state or {'prior_estimate': 50, 'scope_status': 'active'})


def two_fans(rig, base=71000):
    rig.fans_in(base=base)


# ----------------------------------------------------------------------------------------------------------------
def test_pinned_guard_is_the_host_guard(guard):
    assert hashlib.sha256(PINNED.read_bytes()).hexdigest() == PINNED_SHA256
    assert guard.require_health.__module__ == 'u3_pinned_guard_core'


def test_signed_tip_holds_fan_a_while_fan_b_replies_and_real_guard_and_supervisor_permit(rig, guard, sup, tmp_path, monkeypatch):
    rig.post(tip_event(FAN), key='u3-tip-a')
    rig.fans_in(base=71000)
    with live(rig) as (client, ready, status):
        wait_posts(rig, 1, client)
        ready = settle(client, rig).json()
        assert status == 200 and ready['mode'] == 'active' and ready['code'] == 'ready'
        assert ready['sendingReady'] is True, ready
        assert ready['work']['accountHold'] is False and ready['work']['eligibleFans'] == 1 and ready['work']['heldFans'] == 1
        assert ready['work']['malformedHolds'] == 1 and ready['workHeld'] is True    # the hold stays visible
        assert len(to_fan(rig.w.fans.posts, FAN2)) == 1 and not to_fan(rig.w.fans.posts, FAN)
        guard.require_health(public_status=401, local_status=401, intake={'ok': True, 'mode': 'intake_only'},
                             scope_status='active', active_sender=ready)
        result = Host(sup, rig, client, tmp_path, monkeypatch).check()
        assert result['scope_status'] == 'active' and result['last_ok_utc']
    assert rig.block(FAN) == 'pilot_evidence_pending' and rig.block(FAN2) is None


def test_both_fans_held_sends_nothing_with_clear_state_and_no_health_failure(rig, guard, sup, tmp_path, monkeypatch):
    rig.post(tip_event(FAN), key='u3-tip-a')
    rig.post(tip_event(FAN2, tid='t2'), key='u3-tip-b')
    rig.fans_in(base=71100)
    with live(rig) as (client, ready, status):
        ready = settle(client, rig, ticks=3).json()
        assert ready['sendingReady'] is True and ready['ok'] is True and ready['code'] == 'ready'
        assert ready['work']['accountHold'] is False
        assert ready['work']['eligibleFans'] == 0 and ready['work']['heldFans'] == 2
        assert rig.w.fans.posts == [] and rig.seen == []
        guard.require_health(public_status=401, local_status=401, intake={'ok': True}, scope_status='active', active_sender=ready)
        Host(sup, rig, client, tmp_path, monkeypatch).check()


@pytest.mark.parametrize('how', ['chat_queue', 'posts_new', 'malformed_bytes', 'conflicting_body', 'unbound_material'])
def test_account_wide_unknown_malformed_or_conflicting_evidence_denies_both_fans(rig, guard, sup, tmp_path, monkeypatch, how):
    if how == 'chat_queue':
        rig.post(queue_event(), key='u3-q')
    elif how == 'posts_new':
        rig.post(ev('posts.new', {'id': 77}), key='u3-posts-new')
    elif how == 'malformed_bytes':
        rig.post(None, raw=b'not a valid webhook envelope', key='u3-bad')
    elif how == 'conflicting_body':
        rig.post(ev('users.typing', {'id': 5}), key='u3-typing')
        rig.post(ev('users.typing', {'id': 6}), key='u3-typing')
    else:
        other = ev('tips.received', tip_event(FAN)['payload'])
        other['account_id'] = 'acct_someone_else'
        rig.post(other, key='u3-unbound')
    rig.post(tip_event(FAN2, tid='t9'), key='u3-tip-b')      # a fan-scoped hold alongside must not soften it
    rig.fans_in(base=71200)
    with live(rig) as (client, ready, status):
        ready = settle(client, rig).json()
        assert ready['ok'] is True and ready['sendingReady'] is False and ready['work']['accountHold'] is True, ready
        assert ready['work']['eligibleFans'] == 0
        assert rig.w.fans.posts == [] and rig.seen == []
        with pytest.raises(guard.GuardFailure, match='sender_not_ready'):
            guard.require_health(public_status=401, local_status=401, intake={'ok': True}, scope_status='active', active_sender=ready)
        with pytest.raises(guard.GuardFailure, match='sender_not_ready'):
            Host(sup, rig, client, tmp_path, monkeypatch).check()


def test_fan_resolution_restores_only_eligible_work_not_other_holds(rig, guard, sup, tmp_path, monkeypatch):
    rig.post(tip_event(FAN), key='u3-tip-a')
    rig.post(tip_event(FAN2, tid='t2'), key='u3-tip-b')
    rig.fans_in(base=71300)
    with live(rig) as (client, ready, status):
        settle(client, rig)
        assert rig.w.fans.posts == []
        items = {i['eventKey']: i for i in rig.items('provider_event')}
        assert set(items) == {'u3-tip-a', 'u3-tip-b'}
        raw_a = json.dumps(tip_event(FAN)).encode()
        with Session(rig.w.database[1]) as s:      # exact retained authenticated bytes, as the operator sees them
            from db.onlyfans_engine_models import OfEnginePilotReconnectReceipt as R
            raw_a = bytes(s.scalar(select(R.raw_body).where(R.event_key == 'u3-tip-a')))
        rig.reconcile(items['u3-tip-a'], 700, raw=raw_a)
        wait_posts(rig, 1, client)
        ready = settle(client, rig).json()
        assert len(to_fan(rig.w.fans.posts, FAN)) == 1 and not to_fan(rig.w.fans.posts, FAN2)
        assert ready['sendingReady'] is True and ready['work']['eligibleFans'] == 1 and ready['work']['heldFans'] == 1
        assert rig.block(FAN) is None and rig.block(FAN2) == 'pilot_evidence_pending'
        Host(sup, rig, client, tmp_path, monkeypatch).check()


def test_resolving_the_fan_hold_does_not_clear_a_coexisting_account_hold(rig, guard):
    rig.post(tip_event(FAN), key='u3-tip-a')
    rig.post(queue_event(), key='u3-q')
    rig.fans_in(base=71400)
    with live(rig) as (client, ready, status):
        settle(client, rig)
        items = {i['eventKey']: i for i in rig.items('provider_event')}
        with Session(rig.w.database[1]) as s:
            from db.onlyfans_engine_models import OfEnginePilotReconnectReceipt as R
            raw_a = bytes(s.scalar(select(R.raw_body).where(R.event_key == 'u3-tip-a')))
        rig.reconcile(items['u3-tip-a'], 701, raw=raw_a)
        ready = settle(client, rig, ticks=3).json()
        assert ready['sendingReady'] is False and ready['work']['accountHold'] is True
        assert rig.w.fans.posts == [] and rig.block(FAN2) == 'pilot_evidence_pending'


# ---- the other denials stay denials, with the real guard / gate ------------------------------------------------
def test_stale_loop_and_stale_lease_still_deny(rig, guard, sup, tmp_path, monkeypatch):
    import gated_worker
    rig.post(tip_event(FAN), key='u3-tip-a')                    # a fan hold must not make a stale loop look fine
    rig.fans_in(base=71500)
    with live(rig) as (client, ready, status):
        # (a) the worker loop stops making progress: readiness reports it and the real guard refuses
        monkeypatch.setattr(type(rig.pilot), 'tick', lambda self, **kw: (_ for _ in ()).throw(RuntimeError('stalled')))
        end = time.monotonic() + 30
        while client.get('/readyz').json()['code'] != 'loop_failed':
            assert time.monotonic() < end
            time.sleep(.1)
        stalled = client.get('/readyz')
        assert stalled.status_code == 503 and stalled.json()['sendingReady'] is False
        with pytest.raises(guard.GuardFailure, match='sender_not_ready'):
            guard.require_health(public_status=401, local_status=401, intake={'ok': True}, scope_status='active', active_sender=stalled.json())
        with pytest.raises(guard.GuardFailure):
            Host(sup, rig, client, tmp_path, monkeypatch).check()
    # (b) the monitor lease: the real guard's freshness rule and the sender gate's 45s wire lease
    with pytest.raises(guard.GuardFailure, match='monitor_stale'):
        guard.require_fresh(now=1000, last_ok=900)
    guard.require_fresh(now=1000, last_ok=990)
    gate, incident = tmp_path / 'budget-state.json', tmp_path / 'outage-open.json'
    boot = tmp_path / 'boot'
    boot.write_text('boot-1')
    monkeypatch.setattr(gated_worker, 'GATE', gate)
    monkeypatch.setattr(gated_worker, 'INCIDENT', incident)
    monkeypatch.setattr(gated_worker, 'BOOT', boot)
    def write(age):
        gate.write_text(json.dumps({'scope_status': 'active', 'scope_revision': 7, 'boot_id': 'boot-1',
                                    'last_ok_monotonic': time.monotonic() - age}))
    write(5)
    assert gated_worker.permitted(7) is True
    write(60)
    assert gated_worker.permitted(7) is False
    write(5)
    assert gated_worker.permitted(8) is False            # wrong scope revision


def test_budget_failure_still_denies_with_a_fan_hold_present(rig, guard, sup, tmp_path, monkeypatch):
    rig.post(tip_event(FAN), key='u3-tip-a')
    rig.fans_in(base=71600)
    with live(rig) as (client, ready, status):
        settle(client, rig)
        host = Host(sup, rig, client, tmp_path, monkeypatch)
        host.check()
        host.team = (100 + 310, 1000 - 5)                  # provider credits beyond the early-stop threshold (300)
        with pytest.raises(guard.GuardFailure, match='credit_early_stop'):
            host.check()
        host.team = (90, 1000)                             # inconsistent monitor evidence also denies
        with pytest.raises(guard.GuardFailure, match='credit_monitor_inconsistent'):
            host.check()
    # the pilot's own request budget: an exhausted ledger refuses the worker, so readiness is not sending-ready
    with Session(rig.w.database[1]) as s, s.begin():
        row, _ = pilot_scope.require(s, rig.w.store._runtime(s, TENANT, ACCOUNT, lock=False))
        row.provider_requests_used = 10 ** 6
    with TestClient(create_app(rig.pilot, run_loop=False)) as client:
        data = client.get('/readyz').json()
        assert data['sendingReady'] is False


def test_emergency_stop_still_denies_sender_permission(rig, guard, sup, tmp_path, monkeypatch):
    import gated_worker
    rig.post(tip_event(FAN), key='u3-tip-a')
    rig.fans_in(base=71700)
    with live(rig) as (client, ready, status):
        wait_posts(rig, 1, client)
        host = Host(sup, rig, client, tmp_path, monkeypatch)
        fresh = host.check()
        assert fresh['scope_status'] == 'active'
        view = pilot_scope.describe(rig.w.store, TENANT, ACCOUNT)
        pilot_scope.set_status(rig.w.store, TENANT, ACCOUNT, 'stopped', expected_revision=view['revision'],
                               command_key=K(990), actor=ACTOR, reason='emergency stop under a fan hold')
        end = time.monotonic() + 30
        while client.get('/readyz').json()['sendingReady']:
            assert time.monotonic() < end
            time.sleep(.1)
        stopped = client.get('/readyz').json()
        assert stopped['sendingReady'] is False
        with pytest.raises(guard.GuardFailure, match='sender_not_ready'):
            guard.require_health(public_status=401, local_status=401, intake={'ok': True}, scope_status='active', active_sender=stopped)
        # the supervisor now publishes a stopped scope, which the sender gate never accepts
        published = host.check({'prior_estimate': 50, 'scope_status': 'active'})
        assert published['scope_status'] == 'stopped'
    gate, boot = tmp_path / 'g.json', tmp_path / 'boot'
    boot.write_text(published['boot_id'])
    gate.write_text(json.dumps({**published, 'last_ok_monotonic': time.monotonic()}))
    monkeypatch.setattr(gated_worker, 'GATE', gate)
    monkeypatch.setattr(gated_worker, 'INCIDENT', tmp_path / 'none.json')
    monkeypatch.setattr(gated_worker, 'BOOT', boot)
    assert gated_worker.permitted(published['scope_revision']) is False


# ---- transient tolerance --------------------------------------------------------------------------------------
class Stop(Exception):
    pass


def run_loop(host, sup, monkeypatch, *, sleeps_before_stop):
    """supervisor.run() with recorded STATE/WATCHDOG publication."""
    published = {'atomic': [], 'notify': [], 'sleep': 0}
    monkeypatch.setattr(sup, 'unresolved', lambda p: False)
    original = sup.readjson
    monkeypatch.setattr(sup, 'readjson', lambda p: {'prior_estimate': 50} if p == sup.STATE else original(p))
    monkeypatch.setattr(sup, 'atomic', lambda p, data: published['atomic'].append(data))
    monkeypatch.setattr(sup, 'notify', lambda m: published['notify'].append(m))
    def sleep(_):
        published['sleep'] += 1
        if published['sleep'] >= sleeps_before_stop:
            raise Stop()
    monkeypatch.setattr(sup.time, 'sleep', sleep)
    return published


def stopped_host(sup, rig, tmp_path, monkeypatch):
    """Stopped scope (the supervisor's required restart state) over the real receiver-ready contract."""
    view = pilot_scope.describe(rig.w.store, TENANT, ACCOUNT)
    pilot_scope.set_status(rig.w.store, TENANT, ACCOUNT, 'stopped', expected_revision=view['revision'],
                           command_key=K(991), actor=ACTOR, reason='stopped for supervisor restart')
    return Host(sup, rig, None, tmp_path, monkeypatch)


def test_transient_failures_never_refresh_state_or_watchdog_and_stay_bounded_to_two(rig, guard, sup, tmp_path, monkeypatch):
    host = stopped_host(sup, rig, tmp_path, monkeypatch)
    script = iter([sup.TransientFailure('provider_webhook_health_unavailable')] * 2 + [True, Stop()])
    def webhook(deadline=None):
        step = next(script)
        if isinstance(step, BaseException):
            raise step
        return step
    host.webhook = webhook
    published = run_loop(host, sup, monkeypatch, sleeps_before_stop=3)
    with pytest.raises(Stop):
        sup.run()
    # two tolerated transients published nothing; only the later success refreshed STATE and the watchdog once
    assert len(published['atomic']) == 1 and len(published['notify']) == 1
    assert published['atomic'][0]['scope_status'] == 'stopped'


def test_third_consecutive_transient_failure_fails_closed_without_publication(rig, guard, sup, tmp_path, monkeypatch):
    host = stopped_host(sup, rig, tmp_path, monkeypatch)
    def webhook(deadline=None):
        raise sup.TransientFailure('provider_webhook_health_unavailable')
    host.webhook = webhook
    published = run_loop(host, sup, monkeypatch, sleeps_before_stop=50)
    with pytest.raises(guard.GuardFailure, match='provider_webhook_health_unavailable'):
        sup.run()
    assert published['atomic'] == [] and published['notify'] == [] and published['sleep'] == 2


@pytest.mark.parametrize('which', ['credit_early_stop', 'credit_monitor', 'receiver', 'hard_webhook'])
def test_identity_security_and_credit_errors_are_not_retried(rig, guard, sup, tmp_path, monkeypatch, which):
    host = stopped_host(sup, rig, tmp_path, monkeypatch)
    hard = guard.GuardFailure
    if which == 'credit_early_stop':
        host.team = (100 + 310, 995)
        expect = 'credit_early_stop'
    elif which == 'credit_monitor':
        host.mp.setattr(sup, 'team_usage', lambda baseline: (_ for _ in ()).throw(hard('credit_monitor_unavailable')))
        expect = 'credit_monitor_unavailable'
    elif which == 'receiver':
        host.mp.setattr(sup, 'intake_readiness', lambda s: (_ for _ in ()).throw(hard('receiver_unavailable')))
        expect = 'receiver_unavailable'
    else:
        def webhook(deadline=None):
            raise hard('provider_webhook_health_unavailable')
        host.webhook = webhook
        expect = 'provider_webhook_health_unavailable'
    published = run_loop(host, sup, monkeypatch, sleeps_before_stop=50)
    with pytest.raises(hard, match=expect):
        sup.run()
    assert published['atomic'] == [] and published['notify'] == [] and published['sleep'] == 0, 'a hard failure is never retried'


def test_sender_identity_failure_is_hard_in_check(rig, guard, sup, tmp_path, monkeypatch):
    rig.fans_in(base=71800)
    with live(rig) as (client, ready, status):
        host = Host(sup, rig, client, tmp_path, monkeypatch)
        host.identity = lambda: (_ for _ in ()).throw(guard.GuardFailure('sender_identity_unavailable'))
        with pytest.raises(guard.GuardFailure, match='sender_identity_unavailable'):
            host.check()
        assert not isinstance(guard.GuardFailure('x'), sup.TransientFailure)
