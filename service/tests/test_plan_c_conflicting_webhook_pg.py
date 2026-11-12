"""Operator review of ONE conflicting provider event key that concerns a fan outside the trial.

The incident: one `messages.sent` key arrived twice with different payloads (a mass-send to a non-test fan).
The conflict re-holds the whole account (`pilot_evidence_pending` for both trial fans) and no existing tool
resolves a conflicting key. The review below is dry-run by default, keeps every variant retained, releases only
that key's account-wide hold, and refuses when any variant could touch a trial fan, money or ownership evidence.
"""
import hashlib
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conversation_engine import conflicting_webhook as cw
from conversation_engine import operator_v2, pilot_scope
from db.onlyfans_engine_models import OfEnginePilotReconnectReceipt, OfEnginePilotWebhookDigest
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_models import OfRuntime
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow
from test_plan_c_events_stage3_pg import (rig, stopped_text, configured_draft, world, database,  # noqa: F401
    ev, K, EVIDENCE, ACTOR, ACCOUNT, TENANT, FAN, FAN2, PROVIDER, iso)
from test_plan_c_scope_pg import OTHER

OUT = 900000001          # synthetic non-test fan id (stands in for the real incident fan)
KEY = 'evt_synthetic_conflict'
KINDS = ('plan_c_conflict_review', 'plan_c_conflict_marker')


def sent(fan, text, mid=777001, **extra):
    return ev('messages.sent', {'id': mid, 'toUser': {'id': fan}, 'text': text, 'createdAt': iso(),
                                'isFromQueue': True, 'isSentByMe': True, **extra})


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def stop(rig, n=98000):
    d = pilot_scope.describe(rig.w.store, TENANT, ACCOUNT)
    return pilot_scope.set_status(rig.w.store, TENANT, ACCOUNT, 'stopped', expected_revision=d['revision'],
                                  command_key=K(n), actor=ACTOR, reason='review one conflicting provider key')['revision']


def conflicted(rig, a=None, b=None, key=KEY):
    """Two different payloads under one key: the first (outside fan, not held) leaves a digest only; the second is held and retained."""
    a = a or sent(OUT, 'first mass message')
    b = b or sent(OUT, 'second mass message')
    raws = [rig.post(a, key=key), rig.post(b, key=key)]
    return raws, stop(rig)


def review(rig, revision, raws=(), apply=False, **over):
    args = dict(event_key=KEY, event_epoch=1, expected_fan=OUT, scope_revision=revision, command_key=K(98100),
                actor=ACTOR, evidence_ref=EVIDENCE, variant_bodies=list(raws), apply=apply)
    args.update(over)
    return cw.review(rig.w.store, TENANT, ACCOUNT, **args)


def blocks(rig):
    return rig.block(FAN), rig.block(FAN2)




def review_rows(rig):
    with Session(rig.w.database[1]) as s:
        return s.scalar(select(func.count()).select_from(OfLaunchCommand).where(OfLaunchCommand.kind.in_(KINDS)))


def digest_state(rig):
    with Session(rig.w.database[1]) as s:
        return sorted((d.event_key, d.payload_sha256, d.safety_hold) for d in s.scalars(select(OfEnginePilotWebhookDigest))), \
            sorted((r.event_key, r.payload_sha256, bytes(r.raw_body)) for r in s.scalars(select(OfEnginePilotReconnectReceipt)))


def test_the_incident_shape_freezes_both_trial_fans_until_reviewed(rig):
    raws, _ = conflicted(rig)
    assert blocks(rig) == ('pilot_evidence_pending', 'pilot_evidence_pending')
    digests, receipts = digest_state(rig)
    assert len(digests) == 2 and sorted(h for _, h, _ in digests) == sorted(sha(r) for r in raws)
    assert [r[1] for r in receipts] == [sha(raws[1])], 'only the held (second) variant bytes were retained'
    with Session(rig.w.database[1]) as s:
        runtime = rig.w.store._runtime(s, TENANT, ACCOUNT, lock=False)
        assert pilot_scope.digest_hold_scope(s, runtime) == (True, frozenset())


def test_dry_run_is_the_default_reports_everything_and_writes_nothing(rig):
    raws, revision = conflicted(rig)
    before = digest_state(rig)
    result = review(rig, revision, raws[:1])               # the operator supplies the variant whose bytes were never retained
    assert result['mode'] == 'dry_run' and result['wouldRelease'] is True and result['applied'] is False
    assert result['sendAuthorized'] is False and result['activationAuthorized'] is False
    assert result['eventKey'] == KEY and result['fan'] == OUT
    assert sorted(v['payloadSha256'] for v in result['variants']) == sorted(sha(r) for r in raws)
    assert {v['bytesSource'] for v in result['variants']} == {'retained', 'operator_supplied'}
    assert blocks(rig) == ('pilot_evidence_pending', 'pilot_evidence_pending')
    assert review_rows(rig) == 0 and digest_state(rig) == before


def test_apply_releases_only_this_key_keeps_both_variants_and_grants_nothing(rig):
    raws, revision = conflicted(rig)
    other = rig.post(ev('posts.new', {'id': 123}), key='unrelated-unknown')      # a different unresolved key
    before = digest_state(rig)
    result = review(rig, revision, raws[:1], apply=True)
    assert result['mode'] == 'applied' and result['applied'] is True and result['replayed'] is False
    assert result['sendAuthorized'] is False and result['activationAuthorized'] is False
    # only this key's hold is released: the unrelated unknown event still freezes both fans
    assert blocks(rig) == ('pilot_evidence_pending', 'pilot_evidence_pending')
    assert result['remainingUnresolvedKeys'] == 1
    with Session(rig.w.database[1]) as s:
        runtime = rig.w.store._runtime(s, TENANT, ACCOUNT, lock=False)
        assert [d.event_key for d in s.scalars(pilot_scope.unresolved_webhook_digests(runtime))] == ['unrelated-unknown']
        assert not s.get(OfRuntime, ACCOUNT).enabled and not s.get(OfDeliveryPolicy, ACCOUNT).enabled
        group = s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.kind == 'plan_c_conflict_review'))
        before_json = json.loads(group.before_json)
        assert sorted(sha(b.encode()) for b in before_json['rawBodies']) == sorted(sha(r) for r in raws), 'both variants retained in the audit'
        assert group.command_key == K(98100) and group.actor == ACTOR and group.reason == EVIDENCE
    assert digest_state(rig) == before, 'digests and receipts are never merged, edited or deleted'
    assert review_rows(rig) == 3                                              # one group record and one marker per variant
    del other


def test_apply_with_nothing_else_unresolved_unfreezes_both_fans_without_any_send(rig):
    raws, revision = conflicted(rig)
    review(rig, revision, raws[:1], apply=True)
    assert blocks(rig) == (None, None)
    assert rig.w.fans.posts == [] and rig.seen == []
    with Session(rig.w.database[1]) as s:
        runtime = rig.w.store._runtime(s, TENANT, ACCOUNT, lock=False)
        assert pilot_scope.digest_hold_scope(s, runtime) == (False, frozenset())


def test_apply_replays_by_command_key_and_a_reused_key_for_another_command_conflicts(rig):
    raws, revision = conflicted(rig)
    first = review(rig, revision, raws[:1], apply=True)
    again = review(rig, revision, raws[:1], apply=True)
    assert again['replayed'] is True and again['mode'] == 'replayed' and again['variants'] == first['variants']
    assert review_rows(rig) == 3
    with pytest.raises(operator_v2.Conflict):
        review(rig, revision, raws[:1], apply=True, evidence_ref=EVIDENCE + ' different')


def test_a_new_variant_after_the_review_re_holds_both_fans(rig):
    raws, revision = conflicted(rig)
    review(rig, revision, raws[:1], apply=True)
    assert blocks(rig) == (None, None)
    rig.post(sent(OUT, 'third mass message 12:10'), key=KEY)
    assert blocks(rig) == ('pilot_evidence_pending', 'pilot_evidence_pending')


def test_missing_or_wrong_variant_bytes_refuse_because_exact_bytes_are_never_assumed(rig):
    raws, revision = conflicted(rig)
    for supplied in ((), (raws[1],), (raws[0].replace(b'first', b'forst'),), (b'{"event":"messages.sent"}',), (raws[0], raws[0])):
        with pytest.raises((operator_v2.BadRequest, operator_v2.Conflict)):
            review(rig, revision, supplied, apply=True)
    assert review_rows(rig) == 0 and blocks(rig) == ('pilot_evidence_pending', 'pilot_evidence_pending')


@pytest.mark.parametrize('case', ['trial_fan_recipient', 'trial_fan_embedded', 'trial_fan_in_first_variant',
                                  'money', 'ppv_unlock', 'ownership', 'wrong_expected_fan', 'mixed_fans',
                                  'other_provider', 'unknown_event'])
def test_any_variant_touching_a_trial_fan_money_or_ownership_is_refused(rig, case):
    a, b, expected = sent(OUT, 'first'), sent(OUT, 'second'), OUT
    if case == 'trial_fan_embedded':
        b = sent(OUT, 'second', recipients=[{'id': FAN}])
    if case == 'trial_fan_in_first_variant':
        a = sent(OUT, 'first', also={'chat_id': str(FAN2)})
    if case == 'money':
        a, b = (ev('transactions.new', {'id': 'tx1', 'fan': {'id': OUT}, 'amount': n}) for n in (5, 6))
    if case == 'ppv_unlock':
        a, b = (ev('messages.ppv.unlocked', {'id': 1, 'chat_id': OUT, 'price': n}) for n in (5, 6))
    if case == 'ownership':
        a, b = ev('accounts.disconnected', {'n': 1}), ev('accounts.disconnected', {'n': 2})
    if case == 'wrong_expected_fan':
        expected = OUT + 1
    if case == 'mixed_fans':
        b = sent(OUT + 5, 'second')
    if case == 'other_provider':
        b = {**sent(OUT, 'second'), 'account_id': 'acct_someone_else'}
    if case == 'unknown_event':
        a, b = ev('posts.new', {'id': 1}), ev('posts.new', {'id': 2})
    if case == 'trial_fan_recipient':
        b = sent(FAN, 'second')
    try:
        raws = [rig.post(a, key=KEY), rig.post(b, key=KEY)]
    except AssertionError:
        pytest.skip('provider route itself refused this shape')
    revision = stop(rig)
    held = blocks(rig)
    with pytest.raises((operator_v2.BadRequest, operator_v2.Conflict, Refused)):
        review(rig, revision, raws, apply=True, expected_fan=expected)
    assert review_rows(rig) == 0 and blocks(rig) == held, 'a refusal changes nothing'


def test_a_single_variant_is_not_a_conflict_and_an_unknown_key_is_not_found(rig):
    raws, revision = conflicted(rig, key=KEY)
    rig.post(sent(OUT, 'only once'), key='single-variant')
    with pytest.raises(operator_v2.Conflict):
        review(rig, revision, raws[:1], apply=True, event_key='single-variant')
    with pytest.raises(operator_v2.NotFound):
        review(rig, revision, raws[:1], apply=True, event_key='no-such-key')
    assert review_rows(rig) == 0


def test_a_conflict_that_was_projected_or_quarantined_through_the_pilot_path_belongs_to_its_existing_owner(rig):
    # The same key first reaches the pilot path for a trial fan (stored in of_fans_webhook), then conflicts.
    raws = [rig.post(sent(FAN, 'to the trial fan'), key='projected-key'), rig.post(sent(OUT, 'to the outside fan'), key='projected-key')]
    revision = stop(rig)
    with pytest.raises((operator_v2.Conflict, operator_v2.BadRequest)):
        review(rig, revision, raws, apply=True, event_key='projected-key')
    assert review_rows(rig) == 0


def test_actor_revision_stopped_scope_and_audit_fields_are_required(rig):
    raws, revision = conflicted(rig)
    with pytest.raises(Refused):
        review(rig, revision, raws[:1], apply=True, actor=OTHER)
    with pytest.raises(operator_v2.BadRequest):
        review(rig, revision, raws[:1], apply=True, actor='automation')
    with pytest.raises(operator_v2.Conflict):
        review(rig, revision - 1, raws[:1], apply=True)
    for bad in ({'command_key': 'short'}, {'command_key': None}, {'evidence_ref': 'trust me'}, {'evidence_ref': ''},
                {'event_epoch': 0}, {'event_epoch': '1'}, {'expected_fan': True}, {'expected_fan': 'x'}, {'scope_revision': None},
                {'event_key': ''}, {'event_key': 'has space'}, {'apply': 'yes'}):
        with pytest.raises((operator_v2.BadRequest, TypeError)):
            review(rig, revision, raws[:1], **{'apply': True, **bad})
    assert review_rows(rig) == 0
    assert blocks(rig) == ('pilot_evidence_pending', 'pilot_evidence_pending')


def test_an_active_scope_cannot_use_the_review(rig):
    raws = [rig.post(sent(OUT, 'first'), key=KEY), rig.post(sent(OUT, 'second'), key=KEY)]
    revision = pilot_scope.describe(rig.w.store, TENANT, ACCOUNT)['revision']
    with pytest.raises(operator_v2.Conflict, match='stopped'):
        review(rig, revision, raws[:1], apply=True)
    with pytest.raises(operator_v2.Conflict, match='stopped'):
        review(rig, revision, raws[:1], apply=False)       # the dry run applies the same preconditions
    assert review_rows(rig) == 0


def test_a_variant_without_retained_or_supplied_bytes_and_a_marker_conflict_are_refused(rig):
    raws, revision = conflicted(rig)
    # A different resolution kind already owns this variant's marker key: never overwrite another owner's record.
    first_marker = pilot_scope.digest_resolution_key(
        rig.w.store._runtime(Session(rig.w.database[1]), TENANT, ACCOUNT, lock=False).binding_id, 1, KEY, sha(raws[0]))
    with Session(rig.w.database[1]) as s, s.begin():
        s.add(OfLaunchCommand(account_id=ACCOUNT, command_key=first_marker, binding_id=s.get(OfRuntime, ACCOUNT).binding_id,
                              kind='plan_c_webhook_resolution', request_hash='0' * 64, actor=ACTOR, reason='x',
                              before_json='{}', result_json='{}', created_at=utcnow()))
    with pytest.raises(operator_v2.Conflict):
        review(rig, revision, raws[:1], apply=True)
    assert review_rows(rig) == 0 and blocks(rig) == ('pilot_evidence_pending', 'pilot_evidence_pending')


# ----------------------------------------------------------------------------- the operator command line
def cli_args(rig, revision, path, **over):
    args = {'--tenant': TENANT, '--account': ACCOUNT, '--event-key': KEY, '--event-epoch': '1', '--expected-fan': str(OUT),
            '--scope-revision': str(revision), '--command-key': K(98200), '--actor': ACTOR, '--evidence-ref': EVIDENCE,
            '--variant-file': str(path)}
    args.update(over)
    return [x for pair in args.items() if pair[1] is not None for x in pair]


def test_command_line_is_dry_run_unless_apply_is_given(rig, tmp_path, capsys):
    import resolve_conflicting_webhook as cli
    raws, revision = conflicted(rig)
    first = tmp_path / 'variant-1.json'
    first.write_bytes(raws[0])
    argv = cli_args(rig, revision, first)
    assert cli.main(argv, open_store=lambda args: rig.w.store) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry['mode'] == 'dry_run' and dry['wouldRelease'] is True and review_rows(rig) == 0
    assert blocks(rig) == ('pilot_evidence_pending', 'pilot_evidence_pending')
    assert cli.main(argv + ['--apply'], open_store=lambda args: rig.w.store) == 0
    done = json.loads(capsys.readouterr().out)
    assert done['mode'] == 'applied' and review_rows(rig) == 3 and blocks(rig) == (None, None)


def test_command_line_requires_actor_revision_command_key_and_evidence(rig, tmp_path, capsys):
    import resolve_conflicting_webhook as cli
    raws, revision = conflicted(rig)
    first = tmp_path / 'variant-1.json'
    first.write_bytes(raws[0])
    for flag in ('--actor', '--scope-revision', '--command-key', '--evidence-ref', '--event-key', '--event-epoch', '--expected-fan'):
        with pytest.raises(SystemExit) as exit_info:
            cli.main(cli_args(rig, revision, first, **{flag: None}), open_store=lambda args: rig.w.store)
        assert exit_info.value.code == 2, flag
    assert review_rows(rig) == 0


def test_command_line_refusal_exits_nonzero_with_a_reason_and_changes_nothing(rig, tmp_path, capsys):
    import resolve_conflicting_webhook as cli
    raws, revision = conflicted(rig)
    wrong = tmp_path / 'wrong.json'
    wrong.write_bytes(raws[0].replace(b'first', b'forst'))
    code = cli.main(cli_args(rig, revision, wrong) + ['--apply'], open_store=lambda args: rig.w.store)
    captured = capsys.readouterr()
    assert code == 1 and json.loads(captured.out)['refused'] is True and 'reason' in json.loads(captured.out)
    assert review_rows(rig) == 0 and blocks(rig) == ('pilot_evidence_pending', 'pilot_evidence_pending')
