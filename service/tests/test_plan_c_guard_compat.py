"""Guard decision record: the historical host guard (c2be833f) versus the release-owned guard (8a06770e).

History: until a815e8e the byte-pinned guard lived at ops/plan-c-supervision/host-guard-c2be833f/guard_core.py and
test_plan_c_u3_readiness_pg.py asserted its sha256. That commit moved it to ops/plan-c-supervision/guard_core.py
(release-owned, imported directly by supervisor.py and collect_monitor_evidence.py) and added exactly one keyword,
credit_limit=300, so each creator/team can carry its own reviewed early-stop threshold. The pin stayed on the old hash,
so every readiness test errored at fixture setup.

DECISION (reviewed, not just re-hashed):
  * the historical bytes are retained as a test fixture and keep their own pin (c2be833f);
  * the current release-owned bytes get their own pin (8a06770e) so a further change is still deliberate;
  * this file proves the two differ ONLY in credit_estimate's new optional limit: same functions elsewhere
    (AST-identical), same 300 early stop at the default, identical results over a grid of inputs, and every
    host-guard denial (ingress, receiver, scope, sender, freshness, credit) still holds on BOTH.
No pure-Python behaviour of the host guard is relaxed.
"""
import ast
import hashlib
import importlib.util
import itertools
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
HISTORICAL = HERE / 'fixtures' / 'host_guard_core_c2be833f.py'
CURRENT = HERE.parents[1] / 'ops' / 'plan-c-supervision' / 'guard_core.py'
HISTORICAL_SHA256 = 'c2be833f7191bf5a94f3c1f841e09effac62cf5a0b03b751663545e9af905797'
CURRENT_SHA256 = '8a06770e214e7b69847ebb0f65f0b2481c4467fd1102a9084a98489ebd11e04b'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def host():
    assert hashlib.sha256(HISTORICAL.read_bytes()).hexdigest() == HISTORICAL_SHA256, 'historical host guard bytes changed'
    return load('compat_host_guard', HISTORICAL)


@pytest.fixture(scope='module')
def current():
    assert hashlib.sha256(CURRENT.read_bytes()).hexdigest() == CURRENT_SHA256, 'release-owned guard bytes changed'
    return load('compat_current_guard', CURRENT)


def defs(path):
    return {n.name: n for n in ast.parse(path.read_text()).body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}


def test_both_pins_are_distinct_and_exact():
    assert HISTORICAL_SHA256 != CURRENT_SHA256
    assert hashlib.sha256(HISTORICAL.read_bytes()).hexdigest() == HISTORICAL_SHA256
    assert hashlib.sha256(CURRENT.read_bytes()).hexdigest() == CURRENT_SHA256


def test_only_credit_estimate_differs_between_the_two_guards():
    old, new = defs(HISTORICAL), defs(CURRENT)
    assert set(old) == set(new) == {'GuardFailure', '_number', 'credit_estimate', 'require_health', 'require_fresh'}
    for name in ('GuardFailure', '_number', 'require_health', 'require_fresh'):
        assert ast.dump(old[name]) == ast.dump(new[name]), f'{name} must be byte-for-byte equivalent code'
    assert ast.dump(old['credit_estimate']) != ast.dump(new['credit_estimate'])
    old_args = [a.arg for a in old['credit_estimate'].args.kwonlyargs]
    new_args = [a.arg for a in new['credit_estimate'].args.kwonlyargs]
    assert new_args == old_args + ['credit_limit']
    assert [ast.literal_eval(d) for d in new['credit_estimate'].args.kw_defaults[-1:]] == [300]


GRID = [0, 1, 100, 249, 250, 299, 300, 301]


def call(module, **extra):
    def run(**kw):
        try:
            return ('ok', module.credit_estimate(**kw, **extra))
        except module.GuardFailure as exc:
            return ('refused', str(exc))
    return run


@pytest.mark.parametrize('limit_kw', [{}, {'credit_limit': 300}])
def test_default_credit_behaviour_matches_the_host_guard_over_a_grid(host, current, limit_kw):
    run_old, run_new = call(host), call(current, **limit_kw)
    seen = set()
    for scope, external, prior in itertools.product(GRID, GRID[:6], (0, 150, 299)):
        kw = dict(baseline_total=0, baseline_balance=1000, team_total=0, team_balance=1000, usage_cost=0,
                  scope_reserved=scope, external_reserved=external, prior_estimate=prior)
        assert run_old(**kw) == run_new(**kw), kw
        seen.add(run_old(**kw)[0])
    assert seen == {'ok', 'refused'}, 'the grid must cross the 300 early stop'
    for bad in ({'usage_cost': 1}, {'usage_cost': True}, {'team_total': -1}, {'team_total': 'x'},
                {'baseline_total': 5, 'team_total': 4}, {'baseline_balance': 5, 'team_balance': 6}):
        kw = dict(baseline_total=0, baseline_balance=1000, team_total=0, team_balance=1000, usage_cost=0,
                  scope_reserved=1, external_reserved=0, prior_estimate=0)
        kw.update(bad)
        assert run_old(**kw) == run_new(**kw) and run_old(**kw)[0] == 'refused', bad


def test_the_new_limit_can_only_tighten_or_set_a_reviewed_value_never_disable_the_stop(host, current):
    base = dict(baseline_total=0, baseline_balance=1000, team_total=0, team_balance=1000, usage_cost=0,
                scope_reserved=9, external_reserved=1, prior_estimate=0)
    assert current.credit_estimate(**base, credit_limit=11) == 10
    with pytest.raises(current.GuardFailure, match='credit_early_stop'):
        current.credit_estimate(**base, credit_limit=10)
    for bad in (0, -1, 10**9, float('nan'), 'x', None, True):
        with pytest.raises(current.GuardFailure, match='credit_monitor_malformed'):
            current.credit_estimate(**base, credit_limit=bad)
    with pytest.raises(TypeError):
        host.credit_estimate(**base, credit_limit=10)   # the historical guard has no such keyword at all


HEALTH = dict(public_status=401, local_status=401, intake={'ok': True, 'mode': 'intake_only', 'code': 'ready'},
              scope_status='configured', active_sender=None)
READY = {'ok': True, 'mode': 'active', 'sendingReady': True, 'code': 'ready'}


@pytest.mark.parametrize('which', ['host', 'current'])
def test_every_host_guard_denial_still_holds_on_both_guards(host, current, which):
    guard = {'host': host, 'current': current}[which]
    guard.require_health(**HEALTH)
    guard.require_health(**{**HEALTH, 'scope_status': 'active', 'intake': {'ok': True}, 'active_sender': READY})
    refusals = [
        (dict(public_status=200), 'ingress_unavailable'), (dict(local_status=500), 'ingress_unavailable'),
        (dict(intake=None), 'receiver_unavailable'), (dict(intake={'ok': False}), 'receiver_unavailable'),
        (dict(scope_status='paused'), 'scope_unavailable'),
        (dict(intake={'ok': True, 'mode': 'active', 'code': 'ready'}), 'receiver_not_ready'),
        (dict(intake={'ok': True, 'mode': 'intake_only', 'code': 'loop_stale'}), 'receiver_not_ready'),
        (dict(scope_status='active', active_sender=None), 'sender_not_ready'),
        (dict(scope_status='active', active_sender={**READY, 'sendingReady': False}), 'sender_not_ready'),
        (dict(scope_status='active', active_sender={**READY, 'code': 'loop_stale'}), 'sender_not_ready'),
        (dict(scope_status='active', active_sender={**READY, 'mode': 'stopped'}), 'sender_not_ready'),
    ]
    for change, code in refusals:
        with pytest.raises(guard.GuardFailure, match=code):
            guard.require_health(**{**HEALTH, **change})
    guard.require_fresh(now=1000, last_ok=990)
    for now, last in ((1000, 900), (1000, 1001), (-1, 0), (10**9, 0)):
        with pytest.raises(guard.GuardFailure, match='monitor_stale'):
            guard.require_fresh(now=now, last_ok=last)
    with pytest.raises(guard.GuardFailure, match='credit_early_stop'):
        guard.credit_estimate(baseline_total=0, baseline_balance=1000, team_total=300, team_balance=700,
                              usage_cost=0, scope_reserved=0, external_reserved=0, prior_estimate=0)
