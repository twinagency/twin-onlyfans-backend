"""W2OR-04 / W3RCA-02: dashboard Activate (connections.change_connection) must not widen an active scope or undo a stop.

Real PostgreSQL (throwaway), synthetic connectors only; no network. Declared bypasses:
  - pilot case: native credentials saved by connections.save_connection, then status='verified' set by ORM (stand-in for
    the live identity verify call);
  - resume case: launch_operations._readiness is patched to ready (resume's other prerequisites run for real).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_models import OfRuntime
from onlyfans_runtime import launch_operations
from onlyfans_runtime.launch_operations import account_state, control_account
from onlyfans_runtime.store import Refused, utcnow
from test_onlyfans_connections_pg import ACTOR, LIMITS, TENANT, change, data, save, settings, verified
from test_plan_c_text_activation_pg import stopped_text, start, configured_draft, database, world  # noqa: F401

POLICY = ('enabled', 'transport_kind', 'media_enabled', 'limit_units', 'throttle_seconds', 'revision')


def _snapshot(store, account):
    with Session(store.engine) as s:
        policy = s.get(OfDeliveryPolicy, account)
        return (s.get(OfRuntime, account).enabled, s.get(OfProviderConnection, account).revision,
                tuple(getattr(policy, name) for name in POLICY) if policy else None)


def _control(store, account, action, key, **kw):
    with Session(store.engine) as s:
        fingerprint = account_state(s, store._runtime(s, TENANT, account, lock=False))['fingerprint']
    return control_account(store, TENANT, account, action=action, command_key=key, actor=ACTOR,
                           reason='synthetic ' + action, expected_fingerprint=fingerprint, **kw)


# ---- W2OR-04: active text-only pilot scope ----

def test_w2or04_dashboard_activate_cannot_widen_an_active_text_only_commercial_scope(stopped_text, monkeypatch):
    from test_onlyfans_text_delivery_pg import ACCOUNT
    w, _, _, _, first = stopped_text
    assert start(w, first['revision'])['status'] == 'active'
    settings(monkeypatch)   # save_connection has no scope check; it disables runtime + policy, the scope stays active
    revision = save(w.store, ACCOUNT, data(), key='6' * 64)
    with Session(w.store.engine) as s, s.begin():
        row = s.get(OfProviderConnection, ACCOUNT)
        row.status, row.verified_at, revision = 'verified', utcnow(), row.revision
    before = _snapshot(w.store, ACCOUNT)
    assert before[2][1:3] == ('fansapi', False)   # text-only scope precondition
    with pytest.raises(Refused, match='scope governs'):
        change(w.store, ACCOUNT, 'activate', revision, '4' * 64,
               {'limitUnits': 10000, 'throttleSeconds': 30, 'mediaEnabled': True})
    assert _snapshot(w.store, ACCOUNT) == before


def test_w2or04_activate_refuses_a_preparation_scope(database, monkeypatch):
    from conversation_engine import preparation
    from test_commercial_preparation_pg import config
    store, account, _ = verified(database, monkeypatch)
    try:
        preparation.configure(store, TENANT, account, config(), expected_revision=0, command_key='a' * 64,
                              actor=ACTOR, reason='synthetic preparation')
        before = _snapshot(store, account)
        with pytest.raises(Refused, match='preparation'):
            change(store, account, 'activate', 1, 'c' * 64, LIMITS)
        assert _snapshot(store, account) == before
    finally:
        store.close()


# ---- W3RCA-02: Activate cannot undo an operator stop ----

@pytest.mark.parametrize('action', ['pause', 'emergency_stop'])
def test_w3rca02_activate_after_a_stop_is_refused_and_changes_nothing(database, monkeypatch, action):
    store, account, _ = verified(database, monkeypatch)
    try:
        assert change(store, account, 'activate', 1, 'c' * 64, LIMITS) == 2
        _control(store, account, action, 'd' * 64)
        before = _snapshot(store, account)
        assert before[0] is False
        with pytest.raises(Refused, match='resume'):
            change(store, account, 'activate', 2, 'e' * 64, LIMITS)
        assert _snapshot(store, account) == before
    finally:
        store.close()


def test_w3rca02_activate_after_pause_then_resume_is_allowed(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    try:
        assert change(store, account, 'activate', 1, 'c' * 64, LIMITS) == 2
        _control(store, account, 'pause', 'd' * 64)
        monkeypatch.setattr(launch_operations, '_readiness', lambda *a, **k: {'readyToEnable': True})
        assert _control(store, account, 'resume', 'e' * 64)['enabled'] is True
        assert change(store, account, 'activate', 2, 'f' * 64, {**LIMITS, 'limitUnits': 9}) == 3
        with Session(store.engine) as s:
            assert s.get(OfRuntime, account).enabled and s.get(OfDeliveryPolicy, account).limit_units == 9
    finally:
        store.close()


def test_never_stopped_scopeless_verified_account_still_activates(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    try:
        assert change(store, account, 'activate', 1, 'c' * 64, LIMITS) == 2
        with Session(store.engine) as s:
            policy = s.get(OfDeliveryPolicy, account)
            assert s.get(OfRuntime, account).enabled and policy.enabled and policy.transport_kind == 'native'
    finally:
        store.close()


def test_w3rca02_connection_api_activate_after_pause_is_409_and_changes_nothing(database, monkeypatch):
    from onlyfans_commands.connection_api import HEADER, SCHEMA, router
    store, account, _ = verified(database, monkeypatch)
    try:
        assert change(store, account, 'activate', 1, 'c' * 64, LIMITS) == 2
        _control(store, account, 'pause', 'd' * 64)
        before = _snapshot(store, account)
        for key, value in {'ONLYFANS_PROCESS_ROLE': 'api', 'ONLYFANS_CONNECTION_TOKEN': 'synthetic-connect',
                           'ONLYFANS_CONNECTION_TENANT': TENANT}.items():
            monkeypatch.setenv(key, value)
        app = FastAPI(); app.include_router(router); app.state.control_store = store
        reply = TestClient(app).post('/onlyfans/connection/v1/accounts/' + account,
            headers={'Authorization': 'Bearer synthetic-connect', HEADER: SCHEMA},
            json={'action': 'activate', 'payload': LIMITS, 'expectedRevision': 2, 'commandKey': 'e' * 64, 'actor': ACTOR})
        assert reply.status_code == 409 and reply.json()['code'] == 'connection_conflict', reply.text
        assert _snapshot(store, account) == before
    finally:
        store.close()
