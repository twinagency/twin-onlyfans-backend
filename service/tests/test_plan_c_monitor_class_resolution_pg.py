"""Class/evidence-based monitor incident resolution: database owner and agreement with the ops owner."""
import copy
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from db.models import OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_models import OfRuntime
from onlyfans_runtime import monitor_incident_recovery as recovery
from onlyfans_runtime.store import Refused, utcnow
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_monitor_resolution_pg import incident, advance_stopped, record  # noqa: F401  (fixtures/helpers)
from test_plan_c_text_activation_pg import stopped_text, configured_draft, world, database  # noqa: F401  (fixtures)
from test_plan_c_scope_pg import ACTOR, OTHER

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ops' / 'plan-c-supervision'))
import incident_state as ops  # noqa: E402

CLASS = 'provider_webhook_health_unavailable'
INCIDENT_V2 = 'ab' * 32


def iso(moment):
    return moment.isoformat() + '+00:00'


def v2(proof, **over):
    """A v2 receipt built from the fixture's stopped scope, bound to its revision."""
    now = utcnow()
    end, start = now - timedelta(minutes=2), now - timedelta(minutes=62)
    base = {**proof, 'version': recovery.V2, 'incident_sha256': INCIDENT_V2, 'incident_id': 'a' * 32,
            'classification': CLASS, 'affected_interval': {'start': iso(start), 'end': iso(end)},
            'delivery_url_sha256': 'd' * 64, 'config_version': 'e' * 64, 'resolved_at': iso(now)}
    base.update(over)
    if 'audit_command_key' not in over:
        base['audit_command_key'] = recovery.command_key_v2(base['incident_sha256'], base['evidence_sha256'],
                                                            base['affected_interval']['start'], base['affected_interval']['end'],
                                                            base['scope_revision'])
    return base


def test_unpinned_class_resolution_is_audited_replayed_and_changes_nothing_else(incident):
    w, proof = incident
    proof = v2(proof)
    with Session(w.database[1]) as s, s.begin():
        s.get(OfConversationState, (ACCOUNT, FAN)).mode = 'no_contact'
    before = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    first = record(w, proof)
    assert record(w, proof) == first
    assert first['recorded'] is True and first['sendingEnabled'] is False and first['holdsReleased'] is False
    assert first['incidentSha256'] == INCIDENT_V2 and first['proof']['classification'] == CLASS
    assert pilot_scope.describe(w.store, TENANT, ACCOUNT) == before
    with Session(w.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'no_contact'
        assert not s.get(OfRuntime, ACCOUNT).enabled and not s.get(OfDeliveryPolicy, ACCOUNT).enabled


def test_committed_audit_replays_after_five_minutes_but_fresh_stale_evidence_is_refused(incident, monkeypatch):
    w, proof = incident
    proof = v2(proof)
    first = record(w, proof)                                   # committed audit
    later = utcnow() + timedelta(minutes=6)
    monkeypatch.setattr(recovery, 'utcnow', lambda: later)
    assert record(w, proof) == first                           # replay by command identity, before the freshness check
    stale = v2(proof, resolved_at=iso(utcnow() - timedelta(minutes=30)),
               affected_interval={'start': iso(utcnow() - timedelta(minutes=90)), 'end': iso(utcnow() - timedelta(minutes=40))},
               evidence_sha256='f' * 64)
    with pytest.raises(Refused):                               # never-committed evidence cannot be replayed into existence
        record(w, stale)


def test_changed_scope_revision_rejects_stale_class_evidence(incident):
    w, proof = incident
    proof = v2(proof)
    advance_stopped(w, 14)
    before = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    with pytest.raises(Refused):
        record(w, proof)
    assert pilot_scope.describe(w.store, TENANT, ACCOUNT) == before
    fresh = v2(proof, scope_revision=14)
    assert record(w, fresh)['sendingEnabled'] is False


@pytest.mark.parametrize('name,change', [
    ('classification', {'classification': 'network_check_failed'}),
    ('incident_id', {'incident_id': 'nope'}),
    ('v1_form_for_unpinned_incident', {'version': recovery.V1}),
    ('wrong_command_key', {'audit_command_key': '9' * 64}),
    ('interval_after_resolution', {'affected_interval': {'start': '2026-01-01T00:00:00+00:00', 'end': '2999-01-01T00:00:00+00:00'}}),
    ('interval_reversed', {'affected_interval': {'start': '2026-01-02T00:00:00+00:00', 'end': '2026-01-01T00:00:00+00:00'}}),
    ('interval_too_long', {'affected_interval': {'start': '2026-01-01T00:00:00+00:00', 'end': '2026-03-01T00:00:00+00:00'}}),
    ('other_operator', {'actor': OTHER}),
    ('wrong_epoch', {'connection_epoch': 2}),
    ('wrong_binding', {'binding_id': 'wrong'}),
    ('url_digest', {'delivery_url_sha256': 'x'}),
    ('config_version', {'config_version': 'x'}),
    ('missing_revision', {'scope_revision': 99}),
])
def test_unsupported_or_stale_class_resolutions_are_refused_without_effect(incident, name, change):
    w, proof = incident
    bad = v2(proof)
    bad.update(change)
    interval = bad.get('affected_interval')
    if 'audit_command_key' not in change and type(interval) is dict and type(bad['scope_revision']) is int:
        # re-derive the identity so only the named field is wrong
        bad['audit_command_key'] = recovery.command_key_v2(bad['incident_sha256'], bad['evidence_sha256'],
                                                           interval['start'], interval['end'], bad['scope_revision'])
    before = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    with pytest.raises(Refused):
        recovery.record_resolution(w.store, TENANT, ACCOUNT, proof=bad, actor=ACTOR, command_key=bad['audit_command_key'])
    assert pilot_scope.describe(w.store, TENANT, ACCOUNT) == before


def test_pinned_incident_cannot_use_the_class_form(incident):
    w, proof = incident
    with pytest.raises(Refused):
        record(w, v2(proof, incident_sha256=recovery.INCIDENT))


# --- the two owners agree --------------------------------------------------------------

START = '2026-11-10T00:00:00+00:00'
END = '2026-11-10T00:40:00+00:00'
DONE = '2026-11-10T00:41:00+00:00'
MARKER = ops.encoded({'incident_id': 'a' * 32, 'last_confirmed_healthy_utc': START, 'resolved': False})


def ops_v2(**over):
    h = ops.digest(MARKER)
    r = {'version': ops.V2, 'incident_sha256': h, 'incident_id': 'a' * 32, 'account_id': ops.ACCOUNT, 'binding_id': ops.BINDING,
         'connection_epoch': 1, 'scope_revision': 31, 'classification': CLASS, 'affected_interval': {'start': START, 'end': END},
         'delivery_url_sha256': 'd' * 64, 'config_version': 'e' * 64, 'actor': 'op-session:' + 'a' * 64,
         'evidence_sha256': 'c' * 64, 'resolved_at': DONE}
    r.update(over)
    interval = r['affected_interval']
    if 'audit_command_key' not in over:
        ok = type(interval) is dict and 'start' in interval and 'end' in interval and type(r['scope_revision']) is int
        r['audit_command_key'] = (ops.command_key_v2(r['incident_sha256'], r['evidence_sha256'], interval['start'], interval['end'],
                                                     abs(int(r['scope_revision']))) if ok else '8' * 64)
    return r


def ops_v1(pinned):
    spec = ops.pinned_incidents()[pinned]
    return {'version': ops.V1, 'incident_sha256': pinned, 'account_id': ops.ACCOUNT, 'binding_id': ops.BINDING, 'connection_epoch': 1,
            'scope_revision': spec['scope_revision'], 'classification': spec['classification'], 'actor': 'op-session:' + 'a' * 64,
            'audit_command_key': 'b' * 64, 'evidence_sha256': 'c' * 64, 'resolved_at': DONE}


def corpus():
    out = [ops_v2()]
    out += [ops_v1(p) for p in ops.pinned_incidents()]
    base = ops_v2()
    out += [ops_v2(classification='network_check_failed'), ops_v2(connection_epoch=2), ops_v2(binding_id='x'), ops_v2(account_id='x'),
            ops_v2(actor='someone'), ops_v2(evidence_sha256='nothex'), ops_v2(config_version='x'), ops_v2(delivery_url_sha256='x'),
            ops_v2(resolved_at='not a time'), ops_v2(resolved_at='2026-11-10T00:30:00+00:00'),
            ops_v2(affected_interval={'start': END, 'end': START}), ops_v2(affected_interval={'start': START, 'end': START}),
            ops_v2(affected_interval={'start': START, 'end': '2026-12-30T00:00:00+00:00'}, resolved_at='2026-12-31T00:00:00+00:00'),
            ops_v2(affected_interval={'start': START}), ops_v2(affected_interval='x'),
            ops_v2(version='plan-c-monitor-resolution.v3'), ops_v2(audit_command_key='9' * 64),
            ops_v2(scope_revision=True), ops_v2(scope_revision=-1), {**base, 'extra': 1}]
    out += [{k: v for k, v in base.items() if k != field} for field in base]
    out += [ops_v2(incident_sha256=p, audit_command_key=ops.command_key_v2(p, 'c' * 64, START, END, 31)) for p in ops.pinned_incidents()]
    for p in ops.pinned_incidents():
        pr = ops_v1(p)
        out += [{**pr, 'scope_revision': pr['scope_revision'] + 1}, {**pr, 'classification': 'other'}, {**pr, 'extra': 1},
                {**pr, 'version': ops.V2}]
    return out


def test_ops_and_service_owners_share_pinned_incidents_classes_fields_and_key_derivation():
    assert ops.pinned_incidents() == recovery.pinned_incidents()
    assert ops.CLASSES == recovery.CLASSES
    assert (ops.V1, ops.V2) == (recovery.V1, recovery.V2)
    assert ops.V1_FIELDS == recovery.V1_FIELDS and ops.V2_FIELDS == recovery.V2_FIELDS
    assert ops.MAX_INTERVAL == recovery.MAX_INTERVAL
    assert (ops.ACCOUNT, ops.BINDING) == (recovery.ACCOUNT, recovery.BINDING)
    assert ops.command_key_v2('a' * 64, 'b' * 64, START, END, 7) == recovery.command_key_v2('a' * 64, 'b' * 64, START, END, 7)
    assert ops.APPROVED_INCIDENT == recovery.INCIDENT
    assert ops.WEBHOOK_INCIDENT == recovery.WEBHOOK_INCIDENT and ops.REPEATED_WEBHOOK_INCIDENT == recovery.REPEATED_WEBHOOK_INCIDENT


def test_ops_and_service_validators_agree_on_every_receipt_in_the_corpus():
    cases = corpus()
    valid = 0
    for receipt in cases:
        h = receipt.get('incident_sha256', '')
        raw = MARKER if h == ops.digest(MARKER) else None
        ops_ok = ops._valid_resolution(copy.deepcopy(receipt), h, raw)
        service_ok = recovery.proof_kind(copy.deepcopy(receipt), recovery.ACCOUNT, recovery.BINDING) is not None
        assert ops_ok == service_ok, receipt
        valid += ops_ok
    assert valid == 1 + len(ops.pinned_incidents())     # exactly the intended valid receipts, nothing more


def test_ops_is_never_looser_than_the_database_owner_on_marker_bound_fields():
    for change in ({'incident_id': 'b' * 32}, {'affected_interval': {'start': '2026-11-10T00:05:00+00:00', 'end': END}}):
        receipt = ops_v2(**change)
        assert recovery.proof_kind(receipt, recovery.ACCOUNT, recovery.BINDING) == 'v2'      # DB cannot see the marker
        assert ops._valid_resolution(receipt, receipt['incident_sha256'], MARKER) is False   # ops binds identity and start
