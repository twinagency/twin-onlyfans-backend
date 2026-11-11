"""The audited preparation -> commercial promotion: in place, idempotent, account-scoped, and it grants nothing."""
import json
import uuid
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from conversation_engine import commercial_scope as cs, pilot_scope, preparation as prep, scope_contract as sc
from db.models import VaultItem
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEnginePilotScope, OfFansConnection
from db.onlyfans_fans_intake_models import OfFansLink
from db.onlyfans_models import OfRuntime
from db.onlyfans_vault_models import OfVaultSyncJob
from onlyfans_read.agency_scope import AgencyScope, current_scope
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow
from test_commercial_preparation_pg import ACTOR, TENANT, factory, identity, page, setup
from test_commercial_scope_contract import body, catalogue
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_real_transport_pg import _item

AGENCY = 'agency-user:' + '1' * 8 + '-' + '1' * 4 + '-' + '1' * 4 + '-' + '1' * 4 + '-' + '1' * 12 + ':' + '2' * 8 + '-' + '2' * 4 + '-' + '2' * 4 + '-' + '2' * 4 + '-' + '2' * 12


def key(n):
    return f'{n:064x}'


def prepared(database, i=1, **limits):
    """A prepared account: verified connection, completed metadata import, runtime and policy disabled."""
    store, account, cfg = setup(database, i, **limits)
    def wire(req):
        return identity(cfg) if req.url.path == '/api/accounts' else page([_item(500 + i)])
    assert prep.prepare_one(store, TENANT, account, client_factory=factory(wire))['state'] == 'complete'
    return store, account, cfg


def promote(store, account, n=100, expected=1, actor=ACTOR, **changes):
    return cs.promote(store, TENANT, account, body(**changes), expected_revision=expected, command_key=key(n),
                      actor=actor, reason='Promote prepared account to a commercial scope')


def snapshot(engine, account):
    with Session(engine) as s:
        row = s.scalar(select(OfEnginePilotScope).where(OfEnginePilotScope.account_id == account))
        policy = s.get(OfDeliveryPolicy, account)
        return {'scope': {c.name: getattr(row, c.name) for c in row.__table__.columns},
                'runtime': s.get(OfRuntime, account).enabled,
                'policy': (policy.enabled, policy.media_enabled, policy.limit_units, policy.revision, policy.transport_kind),
                'link': {c.name: getattr(l, c.name) for l in [s.get(OfFansLink, (account, str(100 + int(account.split('-')[1]))))] for c in l.__table__.columns},
                'vault': s.scalar(select(func.count()).select_from(VaultItem).where(VaultItem.account_id == account)),
                'jobs': sorted((j.job_id, j.status, j.pages_read) for j in s.scalars(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id == account))),
                'connections': sorted((c.epoch, c.status) for c in s.scalars(select(OfFansConnection).where(OfFansConnection.account_id == account)))}


def test_promotion_updates_the_row_in_place_keeps_everything_and_grants_nothing(database):
    store, account, cfg = prepared(database)
    engine = database[1]
    before = snapshot(engine, account)
    result = promote(store, account)
    after = snapshot(engine, account)
    assert result['replayed'] is False and result['status'] == 'configured' and result['contractVersion'] == 'commercial.v1'
    assert result['revision'] == before['scope']['revision'] + 1 and result['automaticRepliesEnabled'] is False
    for same in ('scope_id', 'account_id', 'binding_id', 'created_at', 'provider_requests_used', 'provider_credits_reserved', 'last_attempt_json'):
        assert after['scope'][same] == before['scope'][same], same
    assert before['scope']['provider_requests_used'] == 2 and before['scope']['provider_credits_reserved'] == 4
    assert (after['scope']['status'], after['scope']['active_since']) == ('configured', None)
    assert after['scope']['transition_command_key'] == key(100) and after['scope']['commercial_since'] is not None
    # Nothing around the row moved: runtime and policy stay disabled, the link stays send-blocked, the import is untouched.
    for same in ('runtime', 'policy', 'link', 'vault', 'jobs', 'connections'):
        assert after[same] == before[same], same
    assert after['runtime'] is False and after['policy'][0] is False and after['link']['send_blocked'] is True
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfEnginePilotScope)) == 1
        audit = s.get(OfLaunchCommand, (account, key(100)))
        assert audit.kind == 'commercial_scope' and audit.actor == ACTOR and json.loads(audit.before_json)['fingerprint'] == before['scope']['fingerprint']
        assert json.loads(audit.result_json)['fingerprint'] == after['scope']['fingerprint']
        config = json.loads(after['scope']['config_json'])
        assert config['transition'] == {'from': 'preparation.v1', 'from_revision': 1, 'from_fingerprint': before['scope']['fingerprint'], 'command_key': key(100)}
        assert config['authorization_mode'] == 'supervised' and config['qualification'] == cfg['qualification']
        assert (config['provider_account'], config['creator_id']) == (cfg['provider_account'], cfg['creator_id'])
    # It cannot deliver: admission is closed until the operator's separate launch decision.
    with Session(engine) as s, s.begin():
        s.get(OfRuntime, account).enabled = True
        s.flush()
        with pytest.raises(Refused, match='not active'):
            store._delivery_runtime(s, TENANT, account)
    store.close()


def test_promotion_is_idempotent_by_command_key_and_conflicts_are_refused(database):
    store, account, cfg = prepared(database)
    first = promote(store, account)
    again = promote(store, account)
    assert again['replayed'] is True and {k: v for k, v in again.items() if k != 'replayed'} == {k: v for k, v in first.items() if k != 'replayed'}
    with Session(database[1]) as s:
        assert s.get(OfEnginePilotScope, first['scopeId']).revision == first['revision']
        assert s.scalar(select(func.count()).select_from(OfLaunchCommand).where(OfLaunchCommand.kind == 'commercial_scope')) == 1
    with pytest.raises(Refused, match='command conflict'):
        promote(store, account, fans=[11])                                        # same key, different request
    with pytest.raises(Refused, match='prepared account'):
        promote(store, account, n=101, expected=2)                                # already promoted: a new key cannot repeat it
    store.close()


def test_only_the_managing_agency_actor_can_promote_and_the_operator_cannot_promote_itself(database):
    store, account, cfg = prepared(database)
    grant = AgencyScope(creator_ids=('creator-' + account,), actor=AGENCY, can_manage=True)
    token = current_scope.set(grant)
    try:
        with pytest.raises(Refused, match='managing preparation command'):
            promote(store, account, actor=ACTOR)                                 # an operator session is not the managing agency
        current_scope.set(AgencyScope(creator_ids=('creator-' + account,), actor=AGENCY, can_manage=False))
        with pytest.raises(Refused, match='managing preparation command'):
            promote(store, account, actor=AGENCY)                                # a viewer cannot manage
        current_scope.set(AgencyScope(creator_ids=('creator-someone-else',), actor=AGENCY, can_manage=True))
        with pytest.raises(Refused, match='scope unavailable'):
            promote(store, account, actor=AGENCY)                                # not this creator's agency
        current_scope.set(grant)
        assert promote(store, account, actor=AGENCY)['status'] == 'configured'
        # Product approval / runtime authority stay with the named operator: the agency can neither launch nor reconfigure.
        with pytest.raises(Refused, match='invalid pilot configuration command'):    # an agency identity is not an operator session
            pilot_scope.set_status(store, TENANT, account, 'active', expected_revision=2, command_key=key(7), actor=AGENCY, reason='no')
    finally:
        current_scope.reset(token)
    other = 'op-session:' + 'b' * 64
    with pytest.raises(Refused, match='operator permission'):
        pilot_scope.set_status(store, TENANT, account, 'active', expected_revision=2, command_key=key(8), actor=other, reason='not the named operator')
    with Session(database[1]) as s:
        assert s.scalar(select(OfEnginePilotScope.status)) == 'configured'
    store.close()


def test_promotion_refusals_leave_the_row_untouched(database):
    store, account, cfg = prepared(database)
    engine = database[1]
    before = snapshot(engine, account)
    forbidden = {'authorization_mode': 'autonomous', 'status': 'active', 'provider_account': 'acct_x', 'transition': {}, 'product': {}}
    for extra in forbidden.items():
        with pytest.raises(Refused, match='promotion fields'):
            cs.promote(store, TENANT, account, {**body(), extra[0]: extra[1]}, expected_revision=1, command_key=key(9), actor=ACTOR, reason='x')
    with pytest.raises(Refused, match='revision conflict'):
        promote(store, account, expected=2)
    with pytest.raises(Refused, match='erase'):
        promote(store, account, limits={**body()['limits'], 'provider_requests': 1})
    with pytest.raises(Refused, match='erase'):
        promote(store, account, limits={**body()['limits'], 'provider_credit_budget': 3})
    with pytest.raises(Refused, match='expired'):
        promote(store, account, delivery_mode='full', catalogue=catalogue(account_id=account, binding_id='binding-' + account, valid_until='2001-01-01T00:00:00'))
    with pytest.raises(Refused, match='another creator'):
        promote(store, account, delivery_mode='full', catalogue=catalogue())
    with pytest.raises(Refused, match='boundaries|profile'):
        promote(store, account, creator_profile={'name': 'x'})
    with Session(engine) as s, s.begin():
        s.get(OfRuntime, account).enabled = True
    with pytest.raises(Refused, match='disabled'):
        promote(store, account)
    with Session(engine) as s, s.begin():
        s.get(OfRuntime, account).enabled = False
        s.get(OfDeliveryPolicy, account).enabled = True
    with pytest.raises(Refused, match='disabled'):
        promote(store, account)
    with Session(engine) as s, s.begin():
        s.get(OfDeliveryPolicy, account).enabled = False
        s.get(OfFansLink, (account, cfg['creator_id'])).state = 'disconnected'
    with pytest.raises(Refused, match='connection'):
        promote(store, account)
    with Session(engine) as s, s.begin():
        s.get(OfFansLink, (account, cfg['creator_id'])).state = 'connected'
    after = snapshot(engine, account)
    assert after['scope'] == before['scope']
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfLaunchCommand).where(OfLaunchCommand.kind == 'commercial_scope')) == 0
    store.close()


def test_unresolved_delivery_unprepared_accounts_and_the_legacy_row_are_not_promotable(database):
    from test_onlyfans_foundation_pg import seed
    store, account, cfg = prepared(database)
    unprepared_store, unprepared, _ = setup(database, 2)                          # configured, never connected
    fresh = seed(database[0], database[1], account='fresh-p', tenant=TENANT)
    try:
        with pytest.raises(Refused, match='connection'):
            promote(unprepared_store, unprepared)
        with pytest.raises(Refused, match='prepared account'):
            cs.promote(fresh, TENANT, 'fresh-p', body(), expected_revision=1, command_key=key(5), actor=ACTOR, reason='x')
        with Session(database[1]) as s, s.begin():
            runtime = store._runtime(s, TENANT, account)
            now = utcnow()
            s.add(OfTextIntent(account_id=account, intent_id=str(uuid.uuid4()), fan_id=101, binding_id=runtime.binding_id,
                command_key='9' * 64, actor=ACTOR, origin='manual', body='x', command_hash='1' * 64, payload_hash='2' * 64,
                control_revision=0, input_revision=0, policy_revision=0, status='unknown',
                created_at=now, updated_at=now, expires_at=now + timedelta(hours=1)))
        with pytest.raises(Refused, match='unresolved'):
            promote(store, account)
        with Session(database[1]) as s, s.begin():
            s.execute(text("ALTER TABLE of_engine_pilot_scope DISABLE TRIGGER of_engine_pilot_scope_guard"))
            s.execute(text("UPDATE of_engine_pilot_scope SET scope_id=1 WHERE account_id=:a"), {'a': unprepared})
            s.execute(text("ALTER TABLE of_engine_pilot_scope ENABLE TRIGGER of_engine_pilot_scope_guard"))
        with pytest.raises(Refused, match='prepared account'):
            promote(unprepared_store, unprepared)
    finally:
        unprepared_store.close(); fresh.close(); store.close()


def test_ten_accounts_promote_with_identical_fan_ids_and_a_refused_account_does_not_hold_the_others(database):
    stores = {}
    try:
        for i in range(1, 11):
            stores[i] = prepared(database, i)
        engine = database[1]
        with Session(engine, expire_on_commit=False) as s, s.begin():
            s.get(OfRuntime, 'prep-4').enabled = True                              # account 4 is held (runtime enabled)
        for i, (store, account, cfg) in stores.items():
            if i == 4:
                with pytest.raises(Refused, match='disabled'):
                    promote(store, account, n=200 + i)
            else:
                promote(store, account, n=200 + i, fans=[11, 12, 13])
        with Session(engine) as s:
            rows = s.scalars(select(OfEnginePilotScope).order_by(OfEnginePilotScope.scope_id)).all()
            assert len(rows) == 10 and len({r.scope_id for r in rows}) == 10 and len({r.account_id for r in rows}) == 10
            kinds = {r.account_id: json.loads(r.config_json)['contract_version'] for r in rows}
            assert kinds['prep-4'] == 'preparation.v1' and [k for k, v in kinds.items() if v == 'commercial.v1'].__len__() == 9
            # Every promoted account sees only its own row, with the same audience ids and its own counters/lineage.
            for i, (store, account, cfg) in stores.items():
                if i == 4:
                    continue
                shown = cs.view(store, TENANT, account)
                assert shown['accountId'] == account and shown['config']['fans'] == [11, 12, 13] and shown['config']['provider_account'] == cfg['provider_account']
                assert shown['config']['transition']['command_key'] == key(200 + i) and shown['providerRequestsUsed'] == 2
        held = cs.view(stores[4][0], TENANT, 'prep-4')
        assert held['state'] == 'preparation' and held['eligible'] is False
    finally:
        for store, _, _ in stores.values():
            store.close()


def test_commercial_route_promotes_and_reads_through_the_configuration_credential(database, monkeypatch):
    from onlyfans_commands.configuration_api import router, HEADER, SCHEMA
    store, account, cfg = prepared(database)
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'api')
    monkeypatch.setenv('ONLYFANS_CONFIGURATION_TOKEN', 'fixture-commercial-token')
    monkeypatch.setenv('ONLYFANS_CONFIGURATION_TENANT', TENANT)
    app = FastAPI(); app.state.control_store = store; app.include_router(router)
    client = TestClient(app); url = '/onlyfans/configuration/v1/accounts/' + account + '/commercial'
    headers = {'Authorization': 'Bearer fixture-commercial-token', HEADER: SCHEMA}
    assert client.get(url).status_code == 401
    read = client.get(url, headers=headers)
    assert read.status_code == 200 and read.json()['commercial']['state'] == 'preparation' and read.json()['commercial']['eligible'] is True
    request = {'action': 'promote', 'expectedRevision': 1, 'commandKey': key(300), 'actor': ACTOR, 'reason': 'Route promotion', 'config': body()}
    assert client.post(url, headers=headers, json={**request, 'extra': 1}).status_code == 400
    assert client.post(url, headers=headers, json={k: v for k, v in request.items() if k != 'config'}).status_code == 400
    assert client.post(url, headers=headers, json={**request, 'action': 'activate'}).status_code == 400
    done = client.post(url, headers=headers, json=request)
    assert done.status_code == 200 and done.json()['commercial']['status'] == 'configured' and done.json()['commercial']['replayed'] is False
    assert client.post(url, headers=headers, json=request).json()['commercial']['replayed'] is True
    refused = client.post(url, headers=headers, json={**request, 'commandKey': key(301), 'expectedRevision': 2})
    assert refused.status_code == 409 and refused.json()['code'] == 'commercial_conflict'
    assert client.get(url, headers=headers).json()['commercial']['state'] == 'commercial'
    assert client.get('/onlyfans/configuration/v1/accounts/unknown-acct/commercial', headers=headers).status_code == 404
    assert client.get(url, headers={**headers, HEADER: 'of-configuration.v0'}).status_code == 400
    store.close()
