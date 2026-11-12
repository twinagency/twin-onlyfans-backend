"""The ops identity and the service identity agree through the persisted scope, not through patched constants.

The ops owner's configuration (account, binding, connection epoch, provider account, explicit fans) is compared
with the database rows the service audit actually locks: the runtime binding, the active connection and the
scope's fan list. Nothing here assigns recovery.ACCOUNT / BINDING / PROVIDER / FANS.
"""
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from db.onlyfans_engine_models import OfFansConnection
from db.onlyfans_models import OfRuntime
from onlyfans_runtime import monitor_incident_recovery as recovery
from onlyfans_runtime.store import Refused
from test_plan_c_monitor_scoped_identity_pg import prepare, proof_for
from test_plan_c_text_activation_pg import stopped_text, configured_draft, world, database  # noqa: F401  (fixtures)
from test_plan_c_scope_pg import ACTOR
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ops' / 'plan-c-supervision'))
import incident_state as ops  # noqa: E402
import operation_config  # noqa: E402


def persisted(w, epoch):
    """What the database actually holds for this account."""
    with Session(w.database[1]) as s:
        runtime = s.get(OfRuntime, ACCOUNT)
        conn = s.query(OfFansConnection).filter(OfFansConnection.account_id == ACCOUNT, OfFansConnection.status == 'active').one()
        assert conn.epoch == epoch
        binding, provider = runtime.binding_id, conn.provider_account_id
    return binding, provider, sorted(pilot_scope.describe(w.store, TENANT, ACCOUNT)['config']['fans'])


def reviewed_config(w, persisted_epoch, **over):
    binding, provider, fans = persisted(w, persisted_epoch)
    value = {'version': 'twin-supervision.v1', 'root': '/tmp/twin-supervisor-w3', 'tenant': TENANT, 'account': ACCOUNT,
             'binding': binding, 'epoch': persisted_epoch, 'fans': fans, 'providerAccount': provider, 'webhookId': 'wh_synthetic',
             'publicUrl': 'https://fixture.invalid/onlyfans/webhooks/plan-c', 'receiverUrl': 'http://127.0.0.1:55462',
             'senderUrl': 'http://127.0.0.1:55464', 'databaseContainer': 'fixture-pg', 'databaseRole': 'fixture',
             'databaseName': 'fixture', 'supervisorUnit': 'fixture-supervisor.service', 'senderUnit': 'fixture-sender.service',
             'senderContainer': 'fixture-sender', 'teamAccounts': [ACCOUNT], 'creditStop': 300, 'initialExternalReserved': 0,
             'reservations': []}
    value.update(over)
    return operation_config.validate(value)


def ops_observation(w):
    """Run the ops owner's own scope-identity query against the real schema."""
    with Session(w.database[1]) as s:
        raw = s.execute(text(ops.scope_identity_sql(ACCOUNT).rstrip().rstrip(';'))).scalar_one()
    return json.loads(raw) if isinstance(raw, str) else raw


@pytest.mark.parametrize('epoch', [1, 2])
def test_ops_query_and_comparison_agree_with_the_persisted_scope(stopped_text, epoch):
    w = prepare(stopped_text, epoch)
    ops.check_scope_identity(reviewed_config(w, epoch), ops_observation(w))


@pytest.mark.parametrize('field,change', [('binding', lambda b, p, f: ('other-binding', p, f)),
                                          ('epoch', None),
                                          ('provider', lambda b, p, f: (b, 'acct_other', f)),
                                          ('fans_subset', lambda b, p, f: (b, p, f[:-1])),
                                          ('fans_superset', lambda b, p, f: (b, p, f + [987654321]))])
def test_a_configuration_that_disagrees_with_the_persisted_scope_is_refused(stopped_text, field, change):
    w = prepare(stopped_text, 2)
    binding, provider, fans = persisted(w, 2)
    if field == 'epoch':
        config = reviewed_config(w, 2, epoch=1)
    else:
        binding, provider, fans = change(binding, provider, fans)
        config = reviewed_config(w, 2, binding=binding, providerAccount=provider, fans=fans)
    with pytest.raises(RuntimeError):
        ops.check_scope_identity(config, ops_observation(w))


def test_service_audit_checks_the_identity_the_ops_configuration_claims(stopped_text):
    w = prepare(stopped_text, 1)
    _, provider, fans = persisted(w, 1)
    proof = proof_for(w, 1, recovery.V3)
    args = dict(proof=proof, actor=ACTOR, command_key=proof['audit_command_key'])
    for wrong in ({'provider_account': 'acct_other', 'fans': fans}, {'provider_account': provider, 'fans': fans[:-1]},
                  {'provider_account': provider, 'fans': fans + [987654321]}):
        with pytest.raises(Refused):
            recovery.record_resolution(w.store, TENANT, ACCOUNT, expected_identity=wrong, **args)
    with pytest.raises(Refused):
        recovery.record_resolution(w.store, TENANT, ACCOUNT, expected_identity={'provider_account': provider}, **args)
    before = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    result = recovery.record_resolution(w.store, TENANT, ACCOUNT, expected_identity={'provider_account': provider, 'fans': list(reversed(fans))}, **args)
    assert result['recorded'] and not result['sendingEnabled'] and not result['holdsReleased']
    assert pilot_scope.describe(w.store, TENANT, ACCOUNT) == before


@pytest.mark.parametrize('epoch', [1, 2])
def test_a_receipt_built_through_the_ops_configuration_is_accepted_by_both_owners(stopped_text, epoch):
    """configure() is the resolver's own path; the same receipt validates in the ops owner and in the service audit."""
    w = prepare(stopped_text, epoch)
    config = reviewed_config(w, epoch)
    saved = (ops.ACCOUNT, ops.BINDING, ops.EPOCH)
    try:
        ops.configure(config)
        proof = proof_for(w, epoch, recovery.V3)
        marker = ops.encoded({'incident_id': proof['incident_id'], 'last_confirmed_healthy_utc': proof['affected_interval']['start'],
                              'resolved': False})
        proof['incident_sha256'] = ops.digest(marker)
        proof['audit_command_key'] = ops.command_key_v3(proof['incident_sha256'], proof['evidence_sha256'],
            proof['affected_interval']['start'], proof['affected_interval']['end'], proof['scope_revision'],
            proof['evidence_reference'], proof['named_sends'])
        assert ops._valid_resolution(proof, proof['incident_sha256'], marker)
        assert recovery.proof_kind(proof, ACCOUNT, config['binding'], epoch=epoch) == 'v3'
        result = recovery.record_resolution(w.store, TENANT, ACCOUNT, proof=proof, actor=ACTOR,
            command_key=proof['audit_command_key'],
            expected_identity={'provider_account': config['providerAccount'], 'fans': config['fans']})
        assert result['recorded'] and not result['sendingEnabled']
        # The same receipt is not valid for a different reviewed identity.
        ops.configure(reviewed_config(w, epoch, epoch=epoch + 1))
        assert not ops._valid_resolution(proof, proof['incident_sha256'], marker)
    finally:
        ops.ACCOUNT, ops.BINDING, ops.EPOCH = saved
