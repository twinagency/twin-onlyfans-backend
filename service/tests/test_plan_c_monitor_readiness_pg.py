"""readiness_unavailable monitor-incident class: database owner, and agreement with the ops owner.

The class resolves only through the v3 receipt: operator evidence reference, the exact set of unresolved
sends the operator names, and the unproven-cause caveat. The database owner checks the unresolved sends
inside the locked transaction (the ops host cannot see them atomically). Resolution never releases a hold,
never enables sending, and never settles a named send.
"""
import copy
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from conversation_engine import pilot_scope
from db.models import OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineIndependentAdmission
from db.onlyfans_models import OfRuntime
from onlyfans_runtime import monitor_incident_recovery as recovery
from onlyfans_runtime.store import Refused, utcnow
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_monitor_resolution_pg import incident, advance_stopped, record  # noqa: F401  (fixtures/helpers)
from test_plan_c_text_activation_pg import stopped_text, configured_draft, world, database  # noqa: F401  (fixtures)
from test_plan_c_scope_pg import ACTOR, OTHER

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ops' / 'plan-c-supervision'))
import incident_state as ops  # noqa: E402

READINESS = 'readiness_unavailable'
INCIDENT_V3 = 'cd' * 32
REF = 'operator:fansapi:journal 09:15:30 reviewed, sender container absent, scope stopped'


def iso(moment):
    return moment.isoformat() + '+00:00'


def v3(proof, named=(), **over):
    """A v3 receipt built from the fixture's stopped scope, bound to its revision."""
    now = utcnow()
    end, start = now - timedelta(minutes=2), now - timedelta(minutes=62)
    base = {**proof, 'version': recovery.V3, 'incident_sha256': INCIDENT_V3, 'incident_id': 'a' * 32,
            'classification': READINESS, 'affected_interval': {'start': iso(start), 'end': iso(end)},
            'delivery_url_sha256': 'd' * 64, 'config_version': 'e' * 64, 'resolved_at': iso(now),
            'evidence_reference': REF, 'named_sends': sorted(named), 'cause_caveat': recovery.READINESS_CAVEAT}
    base.update(over)
    if 'audit_command_key' not in over:
        base['audit_command_key'] = recovery.command_key_v3(
            base['incident_sha256'], base['evidence_sha256'], base['affected_interval']['start'],
            base['affected_interval']['end'], base['scope_revision'], base['evidence_reference'], base['named_sends'])
    return base


def add_intent(w, status, fan=FAN):
    intent_id = str(uuid.uuid4())
    now = utcnow()
    with Session(w.database[1]) as s, s.begin():
        runtime = s.get(OfRuntime, ACCOUNT)
        policy = s.get(OfDeliveryPolicy, ACCOUNT)
        state = s.get(OfConversationState, (ACCOUNT, fan))
        s.add(OfTextIntent(account_id=ACCOUNT, intent_id=intent_id, binding_id=runtime.binding_id, fan_id=fan,
            command_key=uuid.uuid4().hex * 2, actor='synthetic-planner', origin='automated', body='hello',
            command_hash='1' * 64, payload_hash='2' * 64, control_revision=state.revision, input_revision=state.input_revision,
            policy_revision=policy.revision, status=status, created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=5), wire_version=1))
    return intent_id


def set_status(w, intent_id, status):
    with Session(w.database[1]) as s, s.begin():
        s.get(OfTextIntent, (ACCOUNT, intent_id)).status = status


def audit_rows(w):
    from onlyfans_runtime.launch_operations_models import OfLaunchCommand
    with Session(w.database[1]) as s:
        return s.query(OfLaunchCommand).filter(OfLaunchCommand.kind == 'plan_c_monitor_resolution').count()


def test_class_and_version_constants_are_shared_with_the_ops_owner():
    assert READINESS in recovery.CLASSES and recovery.CLASSES == ops.CLASSES
    assert recovery.READINESS == ops.READINESS == READINESS
    assert recovery.V3 == ops.V3 and recovery.V3_FIELDS == ops.V3_FIELDS
    assert recovery.READINESS_CAVEAT == ops.READINESS_CAVEAT
    assert recovery.READINESS_MAX_INTERVAL == ops.READINESS_MAX_INTERVAL == timedelta(hours=24)
    assert recovery.V2_CLASSES == ops.V2_CLASSES and READINESS not in recovery.V2_CLASSES
    named = ['a-1', 'b-2']
    assert (recovery.command_key_v3('a' * 64, 'b' * 64, 's', 'e', 7, REF, named)
            == ops.command_key_v3('a' * 64, 'b' * 64, 's', 'e', 7, REF, named))


def test_resolution_with_no_unresolved_send_is_audited_replayed_and_changes_nothing_else(incident):
    w, proof = incident
    proof = v3(proof)
    with Session(w.database[1]) as s, s.begin():
        s.get(OfConversationState, (ACCOUNT, FAN)).mode = 'no_contact'
    before = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    first = record(w, proof)
    assert record(w, proof) == first
    assert first['recorded'] is True and first['sendingEnabled'] is False and first['holdsReleased'] is False
    assert first['proof']['classification'] == READINESS and first['namedSends'] == []
    assert first['causeCaveat'] == recovery.READINESS_CAVEAT and first['evidenceReference'] == REF
    assert pilot_scope.describe(w.store, TENANT, ACCOUNT) == before
    with Session(w.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).mode == 'no_contact'
        assert not s.get(OfRuntime, ACCOUNT).enabled and not s.get(OfDeliveryPolicy, ACCOUNT).enabled
    assert audit_rows(w) == 1


@pytest.mark.parametrize('status', ['unknown', 'dispatching', 'queued'])
def test_an_unresolved_send_the_operator_did_not_name_refuses_the_resolution(incident, status):
    w, proof = incident
    intent = add_intent(w, status)
    for named in ((), ('not-this-one',)):
        with pytest.raises(Refused):
            record(w, v3(proof, named=named))
    assert audit_rows(w) == 0
    # naming exactly the unresolved send is the only accepted form, and it settles nothing
    result = record(w, v3(proof, named=(intent,)))
    assert result['namedSends'] == [intent] and result['holdsReleased'] is False
    with Session(w.database[1]) as s:
        assert s.get(OfTextIntent, (ACCOUNT, intent)).status == status      # the send stays unresolved
    assert audit_rows(w) == 1


def test_naming_a_send_that_is_not_unresolved_is_refused(incident):
    w, proof = incident
    settled = add_intent(w, 'accepted')
    missing = str(uuid.uuid4())
    for named in ((settled,), (missing,)):
        with pytest.raises(Refused):
            record(w, v3(proof, named=named))
    unknown = add_intent(w, 'unknown')
    with pytest.raises(Refused):                                              # one real, one extra
        record(w, v3(proof, named=(unknown, missing)))
    assert audit_rows(w) == 0


def test_an_unresolved_independent_admission_must_also_be_named(incident):
    w, proof = incident
    anchor = add_intent(w, 'accepted')
    attempt = str(uuid.uuid4())
    now = utcnow()
    with Session(w.database[1]) as s, s.begin():
        s.add(OfEngineIndependentAdmission(account_id=ACCOUNT, attempt_id=attempt, intent_id=anchor, anchor_intent_id=anchor,
                                           fan_id=FAN, status='unknown', created_at=now, updated_at=now))
    with pytest.raises(Refused):
        record(w, v3(proof))
    assert record(w, v3(proof, named=(attempt,)))['namedSends'] == [attempt]


def test_committed_audit_replays_after_the_send_is_later_resolved_but_new_evidence_is_rechecked(incident):
    w, proof = incident
    intent = add_intent(w, 'unknown')
    receipt = v3(proof, named=(intent,))
    first = record(w, receipt)
    set_status(w, intent, 'not_accepted')                                     # settled afterwards by its own owner
    assert record(w, receipt) == first                                        # replay by command identity
    with pytest.raises(Refused):                                              # fresh evidence naming it again is refused
        record(w, v3(proof, named=(intent,), evidence_sha256='f' * 64))


def test_v2_form_can_not_carry_the_readiness_class_and_v3_can_not_carry_another(incident):
    w, proof = incident
    good = v3(proof)
    v2_like = {k: val for k, val in good.items() if k not in ('evidence_reference', 'named_sends', 'cause_caveat')}
    v2_like.update(version=recovery.V2, audit_command_key=recovery.command_key_v2(
        v2_like['incident_sha256'], v2_like['evidence_sha256'], v2_like['affected_interval']['start'],
        v2_like['affected_interval']['end'], v2_like['scope_revision']))
    with pytest.raises(Refused):
        record(w, v2_like)
    for cls in ('provider_webhook_health_unavailable', 'sender_not_ready', 'network_check_failed', 'credit_monitor_unavailable'):
        with pytest.raises(Refused):
            record(w, v3(proof, classification=cls))
    assert audit_rows(w) == 0


@pytest.mark.parametrize('name,change', [
    ('reference_missing', {'evidence_reference': ''}),
    ('reference_unprefixed', {'evidence_reference': 'reviewed it, trust me please'}),
    ('reference_short', {'evidence_reference': 'operator:fansapi:abc'}),
    ('caveat_dropped', {'cause_caveat': ''}),
    ('caveat_softened', {'cause_caveat': recovery.READINESS_CAVEAT.replace('UNPROVEN', 'PROVEN')}),
    ('named_unsorted', {'named_sends': ['b', 'a']}),
    ('named_duplicate', {'named_sends': ['a', 'a']}),
    ('named_malformed', {'named_sends': ['has space']}),
    ('interval_over_24h', {'affected_interval': {'start': '2026-01-01T00:00:00+00:00', 'end': '2026-01-02T00:00:01+00:00'}}),
    ('other_operator', {'actor': OTHER}),
    ('wrong_epoch', {'connection_epoch': 2}),
    ('wrong_key', {'audit_command_key': '9' * 64}),
])
def test_incomplete_or_weakened_readiness_receipts_are_refused_without_effect(incident, name, change):
    w, proof = incident
    bad = v3(proof)
    bad.update(change)
    if 'audit_command_key' not in change and type(bad['named_sends']) is list and type(bad['affected_interval']) is dict:
        bad['audit_command_key'] = recovery.command_key_v3(
            bad['incident_sha256'], bad['evidence_sha256'], bad['affected_interval']['start'], bad['affected_interval']['end'],
            bad['scope_revision'], bad['evidence_reference'], bad['named_sends'])
    before = pilot_scope.describe(w.store, TENANT, ACCOUNT)
    with pytest.raises(Refused):
        recovery.record_resolution(w.store, TENANT, ACCOUNT, proof=bad, actor=ACTOR, command_key=bad['audit_command_key'])
    assert pilot_scope.describe(w.store, TENANT, ACCOUNT) == before
    assert audit_rows(w) == 0


def test_changed_scope_revision_and_stale_evidence_are_refused(incident):
    w, proof = incident
    stale_revision = v3(proof)
    advance_stopped(w, 14)
    with pytest.raises(Refused):
        record(w, stale_revision)
    fresh = v3(proof, scope_revision=14)
    assert record(w, fresh)['sendingEnabled'] is False
    old = utcnow() - timedelta(minutes=30)
    stale = v3(proof, scope_revision=14, resolved_at=iso(old), evidence_sha256='f' * 64,
               affected_interval={'start': iso(old - timedelta(minutes=60)), 'end': iso(old - timedelta(minutes=5))})
    with pytest.raises(Refused):
        record(w, stale)


# --- the two owners agree --------------------------------------------------------------

START = '2026-11-12T10:00:00+00:00'
END = '2026-11-12T13:00:00+00:00'
DONE = '2026-11-12T13:01:00+00:00'
MARKER = ops.encoded({'incident_id': 'a' * 32, 'last_confirmed_healthy_utc': START, 'resolved': False})


def ops_v3(**over):
    h = ops.digest(MARKER)
    named = over.pop('named_sends', [])
    ref = over.pop('evidence_reference', REF)
    r = {'version': ops.V3, 'incident_sha256': h, 'incident_id': 'a' * 32, 'account_id': ops.ACCOUNT, 'binding_id': ops.BINDING,
         'connection_epoch': 1, 'scope_revision': 40, 'classification': READINESS, 'affected_interval': {'start': START, 'end': END},
         'delivery_url_sha256': 'd' * 64, 'config_version': 'e' * 64, 'actor': 'op-session:' + 'a' * 64,
         'evidence_sha256': 'c' * 64, 'resolved_at': DONE, 'evidence_reference': ref, 'named_sends': named,
         'cause_caveat': ops.READINESS_CAVEAT}
    r.update(over)
    interval = r['affected_interval']
    if 'audit_command_key' not in over:
        ok = (type(interval) is dict and 'start' in interval and 'end' in interval and type(r['scope_revision']) is int
              and type(r['named_sends']) is list and all(type(x) is str for x in r['named_sends']) and type(r['evidence_reference']) is str)
        r['audit_command_key'] = (ops.command_key_v3(r['incident_sha256'], r['evidence_sha256'], interval['start'], interval['end'],
                                                     abs(int(r['scope_revision'])), r['evidence_reference'], r['named_sends'])
                                  if ok else '8' * 64)
    return r


def test_ops_and_service_validators_agree_on_every_v3_receipt_in_the_corpus():
    base = ops_v3()
    cases = [base, ops_v3(named_sends=['x-1']), ops_v3(named_sends=['x-1', 'y-2'])]
    cases += [ops_v3(evidence_reference=ref) for ref in ('', 'operator:fansapi:', 'operator:fansapi:abc', 'no prefix at all ok',
              'operator:fansapi:' + 'x' * 109, 'operator:fansapi:' + 'x' * 108, 'operator:fansapi:ctl\x07char ok', 5)]
    cases += [ops_v3(cause_caveat=c) for c in ('', ops.READINESS_CAVEAT + '.', ops.READINESS_CAVEAT.lower(), None)]
    cases += [ops_v3(named_sends=n) for n in (['b', 'a'], ['a', 'a'], 'a', ['has space'], [1], [''], ['x' * 37], None)]
    cases += [ops_v3(classification=c) for c in ('provider_webhook_health_unavailable', 'sender_not_ready', 'network_check_failed',
              'credit_monitor_unavailable', 'unsupported_monitor_failure')]
    cases += [ops_v3(affected_interval={'start': START, 'end': e}, resolved_at=d) for e, d in (
        ('2026-11-13T10:00:00+00:00', '2026-11-13T10:01:00+00:00'),      # exactly 24h: allowed
        ('2026-11-13T10:00:01+00:00', '2026-11-13T10:01:00+00:00'),      # 24h + 1s: refused
        (START, DONE), ('2026-11-12T14:00:00+00:00', DONE))]
    cases += [ops_v3(connection_epoch=2), ops_v3(binding_id='x'), ops_v3(actor='someone'), ops_v3(scope_revision=True),
              ops_v3(scope_revision=-1), ops_v3(audit_command_key='9' * 64), ops_v3(version=ops.V2), {**base, 'extra': 1}]
    cases += [{k: v for k, v in base.items() if k != field} for field in base]
    valid = 0
    for receipt in cases:
        h = receipt.get('incident_sha256', '')
        raw = MARKER if h == ops.digest(MARKER) else None
        ops_ok = ops._valid_resolution(copy.deepcopy(receipt), h, raw)
        service_ok = recovery.proof_kind(copy.deepcopy(receipt), recovery.ACCOUNT, recovery.BINDING) is not None
        assert ops_ok == service_ok, receipt
        valid += ops_ok
    assert valid == 5        # base, two named-send forms, the 108-char reference and the exactly-24h interval; nothing else
