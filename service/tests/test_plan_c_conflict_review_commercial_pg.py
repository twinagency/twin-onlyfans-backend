"""The conflicting-event review together with the F2 durable queue classification, in a commercial coexistence scope.

pilot_scope's shared hold view now treats the conflicting-event review marker like the queue review marker. It must not
disturb the F2 classification (exact, non-conflicting queue notifications stay classified, never re-held, never rewritten)
and must release only the reviewed key: another conflicting key keeps holding every fan, and a later variant re-holds.
Synthetic identities only.
"""
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conversation_engine import conflicting_webhook as cw, pilot_scope
from conversation_engine.operator_v2 import Conflict
from db.onlyfans_engine_models import OfEnginePilotWebhookDigest
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from test_coexistence_staff_pg import make  # noqa: F401  (fixture)
from test_commercial_preparation_pg import TENANT
from test_onlyfans_foundation_pg import database  # noqa: F401  (fixture)
from coexistence_fixture import FAN_A, FAN_B, OPERATOR, key

OUT = 900000001
EVIDENCE = 'operator:fansapi:synthetic reviewer statement'
HELD = 'pilot_evidence_pending'


def conflicted(w, event_key, third=False):
    """Two payloads under one key for a fan outside the audience; returns the exact bytes of the first (not retained)."""
    w.mid += 1
    first, second = w.sent(w.mid, OUT, 'first', queued=True), w.sent(w.mid, OUT, 'second', queued=True)
    w.deliver(first, event_key=event_key)
    w.deliver(second, event_key=event_key)
    if third:
        w.deliver(w.sent(w.mid, OUT, 'third', queued=True), event_key=event_key)
    return w.raw(first)


def stop(w):
    view = pilot_scope.describe(w.store, TENANT, w.account)
    return pilot_scope.set_status(w.store, TENANT, w.account, 'stopped', expected_revision=view['revision'],
                                  command_key=key(0x9101), actor=OPERATOR, reason='stop to review one conflicting key')['revision']


def review(w, revision, event_key, raw, n, apply=False, fan=OUT):
    return cw.review(w.store, TENANT, w.account, event_key=event_key, event_epoch=1, expected_fan=fan, scope_revision=revision,
                     command_key=key(n), actor=OPERATOR, evidence_ref=EVIDENCE, variant_bodies=[raw], apply=apply)


def unresolved(w):
    with Session(w.database[1]) as s:
        runtime = w.store._runtime(s, TENANT, w.account, lock=False)
        return {(row.epoch, row.event_key) for row in s.scalars(pilot_scope.unresolved_webhook_digests(runtime))}


def observations(w):
    with Session(w.database[1]) as s:
        return s.scalar(select(func.count()).select_from(OfLaunchCommand).where(
            OfLaunchCommand.account_id == w.account, OfLaunchCommand.kind == 'plan_c_queue_observation'))


def test_review_releases_only_its_key_and_leaves_f2_classified_queue_events_untouched(make):
    w = make()
    w.launch(coexist=True)
    for i in range(3):
        w.deliver(w.queue(queue_id=910000 + i), event_key='queue-%d' % i)
    assert w.block(FAN_A) is None and w.block(FAN_B) is None          # F2: classified, no hold
    classified = observations(w)
    assert classified == 3
    one, two = conflicted(w, 'dup-one'), conflicted(w, 'dup-two')
    assert w.block(FAN_A) == w.block(FAN_B) == HELD
    assert unresolved(w) == {(1, 'dup-one'), (1, 'dup-two')}           # queue observations are not in the hold view
    revision = stop(w)
    dry = review(w, revision, 'dup-one', one, 9201)
    assert dry['mode'] == 'dry_run' and dry['applied'] is False and unresolved(w) == {(1, 'dup-one'), (1, 'dup-two')}
    applied = review(w, revision, 'dup-one', one, 9201, apply=True)
    assert applied['applied'] is True and applied['sendAuthorized'] is False
    assert unresolved(w) == {(1, 'dup-two')}                           # only the reviewed key left the hold view
    assert w.block(FAN_A) == w.block(FAN_B) == HELD                    # the other conflict still holds every fan
    review(w, revision, 'dup-two', two, 9202, apply=True)
    assert unresolved(w) == set()
    assert w.block(FAN_A) is None and w.block(FAN_B) is None
    assert observations(w) == classified                              # F2 audit records were not rewritten
    with Session(w.database[1]) as s:                                  # both variants of a reviewed key stay retained
        kept = s.scalars(select(OfEnginePilotWebhookDigest.payload_sha256).where(OfEnginePilotWebhookDigest.event_key == 'dup-one')).all()
    assert len(set(kept)) == 2


def test_a_later_variant_re_holds_a_reviewed_key_and_the_f2_classification_is_not_a_substitute(make):
    w = make()
    w.launch(coexist=True)
    w.deliver(w.queue(), event_key='queue-ok')
    raw = conflicted(w, 'dup-one')
    revision = stop(w)
    review(w, revision, 'dup-one', raw, 9211, apply=True)
    assert w.block(FAN_A) is None
    w.mid += 1
    w.deliver(w.sent(w.mid, OUT, 'third', queued=True), event_key='dup-one')
    assert unresolved(w) == {(1, 'dup-one')} and w.block(FAN_A) == w.block(FAN_B) == HELD


def test_a_conflict_for_an_audience_fan_or_a_money_event_is_never_reviewable_here(make):
    w = make()
    w.launch(coexist=True)
    raw = conflicted(w, 'dup-one')
    w.mid += 1
    w.deliver(w.tip(OUT, tid='tip-a'), event_key='money')
    w.deliver(w.tip(OUT, tid='tip-a', gross=9), event_key='money')
    revision = stop(w)
    with pytest.raises(Conflict):
        review(w, revision, 'dup-one', raw, 9221, apply=True, fan=FAN_A)
    with pytest.raises(Exception):
        review(w, revision, 'money', raw, 9222, apply=True)
    assert (1, 'money') in unresolved(w) and (1, 'dup-one') in unresolved(w)
    assert w.block(FAN_A) == w.block(FAN_B) == HELD


def test_a_commercial_scope_audits_monitor_recovery_through_its_own_persisted_identity(make):
    """Monitor recovery for a promoted commercial account: its binding, epoch, provider account and fan list, no legacy constants."""
    from datetime import timedelta
    from db.onlyfans_engine_models import OfFansConnection
    from db.onlyfans_models import OfRuntime
    from onlyfans_runtime import monitor_incident_recovery as recovery
    from onlyfans_runtime.store import Refused, utcnow
    w = make()
    w.launch(coexist=True)
    revision = stop(w)
    assert w.account != recovery.ACCOUNT
    with Session(w.database[1]) as s:
        binding = s.get(OfRuntime, w.account).binding_id
        connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == w.account,
                                                             OfFansConnection.status == 'active'))
        epoch, provider = connection.epoch, connection.provider_account_id
    now = utcnow()
    start, end = (now - timedelta(minutes=10)).isoformat() + '+00:00', (now - timedelta(minutes=1)).isoformat() + '+00:00'
    reference = 'operator:fansapi:synthetic reviewer statement'
    proof = {'version': recovery.V3, 'incident_sha256': 'cd' * 32, 'incident_id': 'a' * 32, 'account_id': w.account,
             'binding_id': binding, 'connection_epoch': epoch, 'scope_revision': revision, 'classification': recovery.READINESS,
             'affected_interval': {'start': start, 'end': end}, 'delivery_url_sha256': 'd' * 64, 'config_version': 'e' * 64,
             'actor': OPERATOR, 'evidence_sha256': 'f' * 64, 'resolved_at': now.isoformat() + '+00:00',
             'evidence_reference': reference, 'named_sends': [], 'cause_caveat': recovery.READINESS_CAVEAT}
    proof['audit_command_key'] = recovery.command_key_v3(proof['incident_sha256'], proof['evidence_sha256'], start, end, revision,
                                                         reference, [])
    args = dict(proof=proof, actor=OPERATOR, command_key=proof['audit_command_key'])
    for wrong in ({'provider_account': 'acct_other', 'fans': list(w.fans)}, {'provider_account': provider, 'fans': [w.fans[0]]}):
        with pytest.raises(Refused):
            recovery.record_resolution(w.store, TENANT, w.account, expected_identity=wrong, **args)
    result = recovery.record_resolution(w.store, TENANT, w.account, expected_identity={'provider_account': provider,
                                                                                       'fans': list(w.fans)}, **args)
    assert result['recorded'] and not result['sendingEnabled'] and not result['holdsReleased'] and result['namedSends'] == []
    assert pilot_scope.describe(w.store, TENANT, w.account)['status'] == 'stopped'
