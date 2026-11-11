"""Commercial scope lifecycle: configure, launch, stop and emergency stop, per account, text-only first."""
import json
import uuid
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from conversation_engine import commercial_scope as cs, pilot_scope, preparation as prep, scope_contract as sc
from db.models import Fan, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEnginePilotScope
from db.onlyfans_fans_intake_models import OfFansLink
from db.onlyfans_models import OfRuntime
from db.onlyfans_vault_models import OfVaultSyncJob
from onlyfans_runtime.launch_operations import account_state, control_account
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import TextAuthority
from test_commercial_preparation_pg import ACTOR, TENANT
from test_commercial_scope_contract import body, catalogue
from test_commercial_scope_promote_pg import AGENCY, key, prepared, promote
from test_onlyfans_foundation_pg import database  # noqa: F401

OTHER = 'op-session:' + 'b' * 64
AUDIENCE = [101, 102]


@pytest.fixture(autouse=True)
def fixture_only(monkeypatch):
    monkeypatch.setenv('TWIN_PLAN_C_FIXTURE_ONLY', '1')


def add_fan(engine, account, fan):
    with Session(engine) as s, s.begin():
        s.add(Fan(account_id=account, fan_id=fan))
        s.flush()
        s.add(OfConversationState(account_id=account, fan_id=fan, mode='automated', revision=0,
                                  takeover_state='none', requires_attention=False))


def recover(store, account, fans):
    for fan in fans:
        store.need_recovery(TENANT, account, fan)
        claim = store.claim_recovery(TENANT, account)
        assert claim and claim.fan_id == fan
        assert store.recovery_page(TENANT, claim, [], has_more=False) == 'complete'


def unblock(engine, account, creator):
    """The signed provider 'accounts.connected' event is what clears a send block; the test applies its effect."""
    with Session(engine) as s, s.begin():
        link = s.get(OfFansLink, (account, creator))
        link.send_blocked, link.block_reason = False, None


def launchable(database, i=1, n=100, fans=AUDIENCE, **changes):
    """A promoted account that satisfies every launch prerequisite except the operator's own decision."""
    store, account, cfg = prepared(database, i)
    for fan in fans:
        if fan != 101:
            add_fan(database[1], account, fan)
    recover(store, account, fans)
    unblock(database[1], account, cfg['creator_id'])
    promoted = promote(store, account, n=n, fans=list(fans), **changes)
    return store, account, cfg, promoted


def configure(store, account, config, revision, n, actor=ACTOR):
    return pilot_scope.configure(store, TENANT, account, config, expected_revision=revision, command_key=key(n),
                                 actor=actor, reason='Operator scope change')


def activate(store, account, revision, n, actor=ACTOR):
    return pilot_scope.set_status(store, TENANT, account, 'active', expected_revision=revision, command_key=key(n),
                                  actor=actor, reason='Operator launch decision')


def stop(store, account, revision, n, actor=ACTOR):
    return pilot_scope.set_status(store, TENANT, account, 'stopped', expected_revision=revision, command_key=key(n),
                                  actor=actor, reason='Operator stop')


def scope_state(engine, account):
    with Session(engine) as s:
        row = s.scalar(select(OfEnginePilotScope).where(OfEnginePilotScope.account_id == account))
        policy = s.get(OfDeliveryPolicy, account)
        return {'status': row.status, 'revision': row.revision, 'active_since': row.active_since,
                'runtime': s.get(OfRuntime, account).enabled, 'policy': policy.enabled, 'media': policy.media_enabled}


def test_operator_configure_edits_in_place_and_keeps_identity_lineage_and_counters(database):
    store, account, cfg, promoted = launchable(database)
    engine = database[1]
    config = pilot_scope.describe(store, TENANT, account)['config']
    with Session(engine) as s:
        before = {c.name: getattr(r, c.name) for r in [s.scalar(select(OfEnginePilotScope))] for c in r.__table__.columns}
    edited = {**config, 'fans': [101, 102, 103], 'limits': {**config['limits'], 'send_units': 30, 'daily_send_units': 12}}
    done = configure(store, account, edited, promoted['revision'], 400)
    assert (done['status'], done['revision'], done['replayed']) == ('configured', promoted['revision'] + 1, False)
    assert configure(store, account, edited, promoted['revision'], 400)['replayed'] is True
    with Session(engine) as s:
        row = s.scalar(select(OfEnginePilotScope))
        assert s.scalar(select(OfEnginePilotScope.scope_id).where(OfEnginePilotScope.account_id == account)) == before['scope_id']
        for same in ('scope_id', 'account_id', 'binding_id', 'created_at', 'provider_requests_used', 'provider_credits_reserved',
                     'last_attempt_json', 'transition_command_key', 'commercial_since'):
            assert getattr(row, same) == before[same], same
        assert json.loads(row.config_json)['fans'] == [101, 102, 103] and json.loads(row.config_json)['transition'] == config['transition']
        audit = s.get(OfLaunchCommand, (account, key(400)))
        assert audit.kind == 'plan_c_scope' and audit.actor == ACTOR
    # Authority: only the named operator, with the exact permission, on the current revision.
    revision = done['revision']
    with pytest.raises(Refused, match='invalid pilot configuration command'):
        configure(store, account, edited, revision, 401, actor=AGENCY)             # the managing agency is not an operator
    with pytest.raises(Refused, match='operator permission'):
        configure(store, account, edited, revision, 402, actor=OTHER)              # another operator session
    with pytest.raises(Refused, match='revision conflict'):
        configure(store, account, edited, revision - 1, 403)
    # Identity, qualification and lineage are retained.
    for patch in ({'provider_account': 'acct_other'}, {'creator_id': '999'}, {'qualification': {'mode': 'fixture', 'reference': 'changed'}},
                  {'transition': {**config['transition'], 'from_revision': 9}}):
        with pytest.raises(Refused, match='retained'):
            configure(store, account, {**edited, **patch}, revision, 404)
    with pytest.raises(Refused, match='another creator|invalid|promote'):
        configure(store, account, {k: v for k, v in edited.items() if k != 'transition'}, revision, 405)
    with pytest.raises(Refused, match='erase'):
        configure(store, account, {**edited, 'limits': {**edited['limits'], 'provider_requests': 1}}, revision, 406)
    # The legacy two-fan trial shape is not accepted for a commercial account, and vice versa.
    with pytest.raises(Refused):
        configure(store, account, {k: v for k, v in edited.items() if k not in ('contract_version', 'transition', 'provider_account', 'creator_id', 'delivery_mode')}, revision, 407)
    # Policy must be disabled while reconfiguring.
    with Session(engine) as s, s.begin():
        s.get(OfDeliveryPolicy, account).enabled = True
    with pytest.raises(Refused, match='disable'):
        configure(store, account, edited, revision, 408)
    with Session(engine) as s, s.begin():
        s.get(OfDeliveryPolicy, account).enabled = False
    # The operator may choose autonomy for its own scope; the promoting agency could not.
    auto = {**edited, 'authorization_mode': 'autonomous'}
    assert configure(store, account, auto, revision, 409)['status'] == 'configured'
    store.close()


def test_audience_removal_waits_for_unresolved_delivery_and_is_capped(database):
    store, account, cfg, promoted = launchable(database)
    engine = database[1]
    config = pilot_scope.describe(store, TENANT, account)['config']
    with pytest.raises(Refused):
        configure(store, account, {**config, 'fans': list(range(1, 102))}, promoted['revision'], 410)
    with Session(engine) as s, s.begin():
        runtime = store._runtime(s, TENANT, account)
        now = utcnow()
        s.add(OfTextIntent(account_id=account, intent_id=str(uuid.uuid4()), fan_id=102, binding_id=runtime.binding_id,
            command_key='9' * 64, actor=ACTOR, origin='manual', body='x', command_hash='1' * 64, payload_hash='2' * 64,
            control_revision=0, input_revision=0, policy_revision=0, status='unknown',
            created_at=now, updated_at=now, expires_at=now + timedelta(hours=1)))
    with pytest.raises(Refused, match='resolve retained'):
        configure(store, account, {**config, 'fans': [101]}, promoted['revision'], 411)
    assert configure(store, account, {**config, 'fans': [101, 102, 104]}, promoted['revision'], 412)['status'] == 'configured'
    store.close()


def test_launch_needs_every_prerequisite_and_only_a_text_only_commercial_scope_can_launch(database):
    store, account, cfg, promoted = launchable(database)
    engine = database[1]
    revision = promoted['revision']
    # Promotion alone never unblocked the link nor started anything: the state is still the prepared one.
    state = scope_state(engine, account)
    assert (state['status'], state['runtime'], state['policy'], state['active_since']) == ('configured', False, False, None)
    with pytest.raises(Refused, match='operator permission'):
        activate(store, account, revision, 420, actor=OTHER)
    with pytest.raises(Refused, match='invalid pilot configuration command'):
        activate(store, account, revision, 421, actor=AGENCY)
    with pytest.raises(Refused, match='revision conflict'):
        activate(store, account, revision + 1, 422)
    with pytest.MonkeyPatch.context() as outside_test:
        outside_test.delenv('TWIN_PLAN_C_FIXTURE_ONLY')
        with pytest.raises(Refused, match='fixture scope cannot activate'):
            activate(store, account, revision, 423)
    # Each prerequisite, broken on its own, refuses the launch and leaves the scope exactly as it was.
    with Session(engine) as s, s.begin():
        s.get(OfFansLink, (account, cfg['creator_id'])).send_blocked = True
    with pytest.raises(Refused, match='connection'):
        activate(store, account, revision, 424)
    unblock(engine, account, cfg['creator_id'])
    with Session(engine) as s, s.begin():
        job = s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == account))
        job.status = 'failed'
    with pytest.raises(Refused, match='import'):
        activate(store, account, revision, 425)
    with Session(engine) as s, s.begin():
        s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == account)).status = 'complete'
    with Session(engine) as s, s.begin():
        s.get(OfConversationState, (account, 102)).mode = 'paused'
    with pytest.raises(Refused, match='holds activation'):
        activate(store, account, revision, 426)
    with Session(engine) as s, s.begin():
        s.get(OfConversationState, (account, 102)).mode = 'automated'
    with Session(engine) as s, s.begin():
        s.execute(text("UPDATE of_recovery SET status='needed' WHERE account_id=:a AND fan_id=102"), {'a': account})
    with pytest.raises(Refused, match='history'):
        activate(store, account, revision, 427)
    recover(store, account, [102])
    assert scope_state(engine, account) == state
    # An empty audience and a full-delivery scope cannot launch (decision D2 / D4).
    config = pilot_scope.describe(store, TENANT, account)['config']
    empty = configure(store, account, {**config, 'fans': []}, revision, 428)
    with pytest.raises(Refused, match='audience'):
        activate(store, account, empty['revision'], 429)
    full = configure(store, account, {**config, 'delivery_mode': 'full',
        'catalogue': catalogue(account_id=account, binding_id='binding-' + account, valid_until=(utcnow() + timedelta(days=1)).replace(microsecond=0).isoformat())},
        empty['revision'], 430)
    with pytest.raises(Refused, match='commercial_product_contract_unavailable'):
        activate(store, account, full['revision'], 431)
    text_only = configure(store, account, config, full['revision'], 432)
    assert scope_state(engine, account)['status'] == 'configured' and scope_state(engine, account)['runtime'] is False
    live = activate(store, account, text_only['revision'], 433)
    assert (live['status'], live['revision'], live['replayed']) == ('active', text_only['revision'] + 1, False)
    assert activate(store, account, text_only['revision'], 433)['replayed'] is True
    state = scope_state(engine, account)
    assert state['status'] == 'active' and state['active_since'] is not None and state['runtime'] and state['policy'] and not state['media']
    with Session(engine) as s:
        proof = json.loads(s.get(OfLaunchCommand, (account, key(433))).before_json)['textActivation']
        assert {x['fan'] for x in proof['fanRecovery']} == set(AUDIENCE) and proof['providerAccountId'] == cfg['provider_account']
        row = s.scalar(select(OfEnginePilotScope))
        assert row.transition_command_key == key(100)
        pilot, active_config = pilot_scope.require(s, store._runtime(s, TENANT, account), active=True)
        assert active_config['contract_version'] == 'commercial.v1'
        store._delivery_runtime(s, TENANT, account)                                  # admission is open only now
    with pytest.raises(Refused, match='stop the pilot'):
        configure(store, account, config, live['revision'], 434)                       # active config is frozen
    store.close()


def test_a_promoted_row_that_was_never_launched_cannot_deliver_anything(database):
    store, account, cfg, promoted = launchable(database)
    engine = database[1]
    # Even if someone enables the runtime and the policy out of band, nothing is admitted without the scope launch.
    authority = TextAuthority(store)
    with Session(engine) as s:
        revision = s.get(OfDeliveryPolicy, account).revision
    with pytest.raises(Refused, match='operator permission'):
        authority.configure(TENANT, account, expected_revision=revision, enabled=True, media_enabled=False, limit_units=5,
                            throttle_seconds=5, transport_kind='fansapi', actor=OTHER, reason='not the operator')
    authority.configure(TENANT, account, expected_revision=revision, enabled=True, media_enabled=False, limit_units=5,
                        throttle_seconds=5, transport_kind='fansapi', actor=ACTOR, reason='operator enables a policy early')
    with Session(engine) as s, s.begin():
        s.get(OfRuntime, account).enabled = True
        s.flush()
        with pytest.raises(Refused, match='not active'):
            store._delivery_runtime(s, TENANT, account)
        with pytest.raises(Refused, match='not active'):
            pilot_scope.require(s, store._runtime(s, TENANT, account), active=True)
    with pytest.raises(Refused, match='not active'):
        authority.prepare(TENANT, account, 101, command_key=key(450), expected_revision=0, body='hello', origin='manual', actor=ACTOR)
    with pytest.raises(Refused, match='not active'):
        pilot_scope.reserve_provider_request(store, TENANT, account, expected_revision=promoted['revision'])
    with Session(engine) as s:
        assert s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == account)) is None
    store.close()


def test_preparation_and_vault_refresh_after_promotion_use_the_same_row_and_budget_and_never_launch(database):
    import httpx
    from test_commercial_preparation_pg import factory, identity, page
    from test_real_transport_pg import _item
    from onlyfans_runtime.vault_management import request_sync
    store, account, cfg, promoted = launchable(database)
    engine = database[1]
    with Session(engine) as s:
        before = s.scalar(select(OfEnginePilotScope))
        used, reserved = before.provider_requests_used, before.provider_credits_reserved
    queued = request_sync(store, TENANT, account, expected_connection_revision=1, command_key=key(460), actor=ACTOR)
    def wire(req):
        return identity(cfg) if req.url.path == '/api/accounts' else page([_item(900)])
    result = prep.prepare_one(store, TENANT, account, client_factory=factory(wire))
    assert result['state'] == 'complete' and result['vault']['jobId'] == queued['jobId']
    with Session(engine) as s:
        row = s.scalar(select(OfEnginePilotScope))
        assert row.scope_id == before.scope_id and row.provider_requests_used == used + 2 and row.provider_credits_reserved == reserved + 4
        assert (row.status, row.active_since, row.revision) == ('configured', None, promoted['revision'])
        assert json.loads(row.config_json)['contract_version'] == 'commercial.v1'
        assert s.get(OfFansLink, (account, cfg['creator_id'])).send_blocked is True       # the identity re-proof blocks sends again
    state = scope_state(engine, account)
    assert (state['runtime'], state['policy']) == (False, False)
    with pytest.raises(Refused, match='promoted'):
        prep.configure(store, TENANT, account, cfg, expected_revision=promoted['revision'], command_key=key(461), actor=ACTOR, reason='x')
    with pytest.raises(Refused, match='promoted'):
        prep.stop(store, TENANT, account, expected_revision=promoted['revision'], command_key=key(462), actor=ACTOR, reason='x')
    store.close()


def test_operator_agency_and_emergency_stop_end_in_the_same_state_and_leave_other_accounts_running(database):
    engine = database[1]
    fleet = {}
    try:
        for i in (1, 2, 3):
            fleet[i] = launchable(database, i, n=100)
        live = {}
        for i, (store, account, cfg, promoted) in fleet.items():
            live[i] = activate(store, account, promoted['revision'], 500 + i)
        assert all(scope_state(engine, f'prep-{i}')['status'] == 'active' for i in fleet)
        # 1: operator stop. 2: managing-agency stop. 3: emergency stop of the account.
        store1, a1, _, _ = fleet[1]
        assert stop(store1, a1, live[1]['revision'], 510)['status'] == 'stopped'
        store2, a2, _, _ = fleet[2]
        done = cs.stop(store2, TENANT, a2, expected_revision=live[2]['revision'], command_key=key(520), actor=ACTOR, reason='Agency stop')
        assert done['status'] == 'stopped' and done['replayed'] is False and cs.stop(store2, TENANT, a2, expected_revision=live[2]['revision'],
            command_key=key(520), actor=ACTOR, reason='Agency stop')['replayed'] is True
        store3, a3, _, _ = fleet[3]
        with Session(engine) as s, s.begin():
            runtime = store3._runtime(s, TENANT, a3)
            now = utcnow()
            policy = s.get(OfDeliveryPolicy, a3)
            s.add(OfTextIntent(account_id=a3, intent_id=str(uuid.uuid4()), fan_id=101, binding_id=runtime.binding_id,
                command_key='8' * 64, actor=ACTOR, origin='manual', body='x', command_hash='1' * 64, payload_hash='2' * 64,
                control_revision=0, input_revision=0, policy_revision=0, status='queued', created_at=now, updated_at=now,
                expires_at=now + timedelta(hours=1)))
            policy.queued_count = 1
            fingerprint = account_state(s, runtime)['fingerprint']
        emergency = control_account(store3, TENANT, a3, action='emergency_stop', command_key=key(530), actor=ACTOR,
                                    reason='Emergency stop', expected_fingerprint=fingerprint)
        assert emergency['recorded'] and emergency['scopeRevision'] == live[3]['revision'] + 1
        for i in (1, 2, 3):
            state = scope_state(engine, f'prep-{i}')
            assert (state['status'], state['active_since'], state['runtime'], state['policy']) == ('stopped', None, False, False), i
        with Session(engine) as s:
            intent = s.scalar(select(OfTextIntent).where(OfTextIntent.account_id == a3))
            assert (intent.status, intent.reason) == ('cancelled', 'account_stopped') and s.get(OfDeliveryPolicy, a3).queued_count == 0
            # The worker's per-tick gate fails on a stopped scope; admission is closed; the reservation path is closed.
            for account, store in ((a1, store1), (a2, store2), (a3, store3)):
                runtime = store._runtime(s, TENANT, account)
                with pytest.raises(Refused, match='not active'):
                    pilot_scope.require(s, runtime, active=True)
                with pytest.raises(Refused, match='not active'):
                    store._delivery_runtime(s, TENANT, account)
        # An emergency stop never resumes by itself: resume is refused until the scope is launched again.
        with Session(engine) as s:
            fingerprint = account_state(s, store3._runtime(s, TENANT, a3))['fingerprint']
        with pytest.raises(Refused, match='active commercial scope'):
            control_account(store3, TENANT, a3, action='resume', command_key=key(531), actor=ACTOR, reason='Resume attempt',
                            expected_fingerprint=fingerprint)
        assert scope_state(engine, a3)['runtime'] is False
        # Stopped -> active again is the operator's separate decision and works for each stop path.
        for i, (store, account, _, _) in fleet.items():
            with Session(engine) as s:
                revision = s.scalar(select(OfEnginePilotScope.revision).where(OfEnginePilotScope.account_id == account))
            assert activate(store, account, revision, 540 + i)['status'] == 'active'
        # Stopping one account leaves every other account running.
        stop(store1, a1, live[1]['revision'] + 2, 550)
        assert scope_state(engine, a1)['status'] == 'stopped'
        assert scope_state(engine, a2)['status'] == 'active' and scope_state(engine, a3)['status'] == 'active'
        assert scope_state(engine, a2)['runtime'] and scope_state(engine, a3)['runtime']
    finally:
        for store, _, _, _ in fleet.values():
            store.close()


def test_agency_stop_route_and_unauthorized_stop_paths(database, monkeypatch):
    from onlyfans_commands.configuration_api import router, HEADER, SCHEMA
    store, account, cfg, promoted = launchable(database)
    live = activate(store, account, promoted['revision'], 560)
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'api')
    monkeypatch.setenv('ONLYFANS_CONFIGURATION_TOKEN', 'fixture-commercial-token')
    monkeypatch.setenv('ONLYFANS_CONFIGURATION_TENANT', TENANT)
    app = FastAPI(); app.state.control_store = store; app.include_router(router)
    client = TestClient(app); url = '/onlyfans/configuration/v1/accounts/' + account + '/commercial'
    headers = {'Authorization': 'Bearer fixture-commercial-token', HEADER: SCHEMA}
    request = {'action': 'stop', 'expectedRevision': live['revision'], 'commandKey': key(561), 'actor': ACTOR, 'reason': 'Route stop'}
    assert client.post(url, headers=headers, json={**request, 'config': {}}).status_code == 400
    with pytest.raises(Refused, match='operator permission'):
        stop(store, account, live['revision'], 562, actor=OTHER)
    stale = client.post(url, headers=headers, json={**request, 'expectedRevision': live['revision'] + 5, 'commandKey': key(563)})
    assert stale.status_code == 409 and scope_state(database[1], account)['status'] == 'active'
    done = client.post(url, headers=headers, json=request)
    assert done.status_code == 200 and done.json()['commercial']['status'] == 'stopped'
    assert scope_state(database[1], account)['runtime'] is False and scope_state(database[1], account)['policy'] is False
    assert client.post(url, headers=headers, json=request).json()['commercial']['replayed'] is True
    store.close()
