"""Recovery tools: scoped identity agreement, honest modes, and the review-before-interval order.

The service owner reads the persisted runtime/connection/scope; the ops owner reads the reviewed supervision
configuration. These tests pin how the two agree (configure(), the scope-identity SQL and its comparison, the
identity handed to the database audit) and that the resolver never calls a preview a dry run.
"""
import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import incident_state as m
import operation_config
import resolve_monitor_incident as r
import test_validate_interval as tvi
import validate_interval as v
from test_operation_config import fixture_config

READINESS = 'readiness_unavailable'
START = '2026-11-12T10:00:00+00:00'
END = '2026-11-12T13:00:00+00:00'
DONE = '2026-11-12T13:01:00+00:00'
REF = 'operator:fansapi:synthetic reviewer statement'


def scoped_config(root='/tmp/twin-supervisor-fixture-w3'):
    """A reviewed configuration for a synthetic, non-legacy creator."""
    value = fixture_config(root, 'creator-w3')
    value.update(binding='binding-w3', epoch=3, providerAccount='acct_synthetic_w3', fans=[11, 22, 33],
                 teamAccounts=['creator-w3'])
    return operation_config.validate(value)


def marker():
    return m.encoded({'incident_id': 'a' * 32, 'last_confirmed_healthy_utc': START, 'resolved': False})


def v3_receipt(account, binding, epoch, raw):
    h, evidence = m.digest(raw), 'f' * 64
    receipt = {'version': m.V3, 'incident_sha256': h, 'incident_id': 'a' * 32, 'account_id': account, 'binding_id': binding,
               'connection_epoch': epoch, 'scope_revision': 5, 'classification': READINESS,
               'affected_interval': {'start': START, 'end': END}, 'delivery_url_sha256': 'd' * 64, 'config_version': 'e' * 64,
               'actor': 'op-session:' + 'a' * 64, 'evidence_sha256': evidence, 'resolved_at': DONE,
               'evidence_reference': REF, 'named_sends': [], 'cause_caveat': m.READINESS_CAVEAT}
    receipt['audit_command_key'] = m.command_key_v3(h, evidence, START, END, 5, REF, [])
    return receipt


class Caveat(unittest.TestCase):
    def test_the_caveat_states_the_loss_and_never_a_status(self):
        caveat = m.READINESS_CAVEAT
        for word in ('503', 'loop_stale', 'HTTP 5'):
            self.assertNotIn(word, caveat)
        for word in ('UNPROVEN', 'status', 'not retained', 'does not prove', 'grants no send', 'settles no send'):
            self.assertIn(word, caveat)


class Modes(unittest.TestCase):
    def test_dry_run_is_not_offered_because_the_first_run_collects_and_retains_evidence(self):
        with self.assertRaises(RuntimeError):
            r.parse_args(['resolver', '--dry-run'])

    def test_preview_and_apply_are_explicit_exclusive_and_not_repeated(self):
        self.assertEqual(r.mode(['resolver']), 'preview')
        self.assertEqual(r.mode(['resolver', '--preview']), 'preview')
        self.assertEqual(r.mode(['resolver', '--apply']), 'apply')
        for argv in (['resolver', '--preview', '--apply'], ['resolver', '--apply', '--apply'],
                     ['resolver', '--preview', '--preview']):
            with self.subTest(argv=argv):
                with self.assertRaises(RuntimeError):
                    r.mode(argv)
        self.assertEqual(r.parse_args(['resolver', '--preview', '--resume', '/x'])[0], '/x')
        self.assertEqual(r.parse_args(['resolver', '--apply', '--evidence-ref', REF])[1], REF)


class Configure(unittest.TestCase):
    def setUp(self):
        self.saved = (m.ACCOUNT, m.BINDING, m.EPOCH)

    def tearDown(self):
        m.ACCOUNT, m.BINDING, m.EPOCH = self.saved

    def test_configure_binds_receipts_to_the_reviewed_identity_and_drops_the_legacy_pins(self):
        config = scoped_config()
        m.configure(config)
        self.assertEqual((m.ACCOUNT, m.BINDING, m.EPOCH), ('creator-w3', 'binding-w3', 3))
        self.assertEqual(m.pinned_incidents(), {})
        raw = marker()
        good = v3_receipt('creator-w3', 'binding-w3', 3, raw)
        self.assertTrue(m._valid_resolution(good, m.digest(raw), raw))
        for field, value in (('account_id', m.LEGACY_ACCOUNT), ('binding_id', m.LEGACY_BINDING), ('connection_epoch', 1)):
            with self.subTest(field=field):
                self.assertFalse(m._valid_resolution({**good, field: value}, m.digest(raw), raw))

    def test_an_unconfigured_process_still_means_the_legacy_identity_only(self):
        self.assertEqual((m.ACCOUNT, m.BINDING, m.EPOCH), (m.LEGACY_ACCOUNT, m.LEGACY_BINDING, 1))
        raw = marker()
        self.assertFalse(m._valid_resolution(v3_receipt('creator-w3', 'binding-w3', 3, raw), m.digest(raw), raw))


class ScopeIdentity(unittest.TestCase):
    def observed(self, **over):
        row = {'binding': 'binding-w3', 'epoch': 3, 'provider': 'acct_synthetic_w3', 'fans': [33, 11, 22]}
        row.update(over)
        return [row]

    def test_agreeing_persisted_state_passes_whatever_the_fan_order(self):
        m.check_scope_identity(scoped_config(), self.observed())

    def test_every_disagreement_between_configuration_and_persisted_state_refuses(self):
        cases = (self.observed(binding='binding-other'), self.observed(epoch=2), self.observed(provider='acct_other'),
                 self.observed(fans=[11, 22]), self.observed(fans=[11, 22, 33, 44]), self.observed(fans=[]),
                 self.observed(fans=None), [], self.observed() + self.observed(), None, {'binding': 'binding-w3'})
        for observed in cases:
            with self.subTest(observed=observed):
                with self.assertRaises(RuntimeError):
                    m.check_scope_identity(scoped_config(), observed)

    def test_the_query_names_only_the_configured_account_and_refuses_an_unsafe_one(self):
        sql = m.scope_identity_sql('creator-w3')
        self.assertIn("'creator-w3'", sql)
        for table in ('of_runtime', 'of_fans_connection', 'of_engine_pilot_scope'):
            self.assertIn(table, sql)
        for bad in ("x' or 1=1 --", '', 'a b', None):
            with self.subTest(account=bad):
                with self.assertRaises(ValueError):
                    m.scope_identity_sql(bad)


class ContainerIdentity(unittest.TestCase):
    """The validator and the database audit receive the same reviewed identity."""

    def test_audit_and_validator_receive_the_full_reviewed_context(self):
        config = scoped_config()
        with patch.object(r, 'CONFIG', config), patch.object(r, 'ROOT', Path('/tmp/root-w3')):
            context = r.container_context()
            self.assertEqual(json.loads(context), {k: config[k] for k in
                ('account', 'binding', 'epoch', 'providerAccount', 'fans', 'publicUrl')})
            for args in (r.validator_args('sha256:' + '1' * 64, Path('/tmp/e')), r.audit_args('sha256:' + '1' * 64, Path('/tmp/e'))):
                self.assertIn('TWIN_SUPERVISION_CONTEXT=' + context, args)
                self.assertIn('TWIN_SUPERVISION_ACCOUNT=creator-w3', args)


class ValidatorEntryPoint(unittest.TestCase):
    """The validator runs in a container that receives only the identity context, never TWIN_SUPERVISOR_CONFIG."""

    def test_the_script_takes_its_identity_from_the_context_only(self):
        import os
        import subprocess
        import sys
        config = scoped_config()
        env = {k: val for k, val in os.environ.items() if k != 'TWIN_SUPERVISOR_CONFIG'}
        env['TWIN_SUPERVISION_CONTEXT'] = json.dumps({k: config[k] for k in
            ('account', 'binding', 'epoch', 'providerAccount', 'fans', 'publicUrl')}, sort_keys=True)
        done = subprocess.run([sys.executable, str(Path(v.__file__))], env=env, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(done.returncode, 0)                       # there is no /proof here
        self.assertNotIn('TWIN_SUPERVISOR_CONFIG', done.stderr)
        self.assertIn('manifest.json', done.stderr)                   # it got as far as reading the retained proof


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class ReviewBeforeInterval(unittest.TestCase):
    """An eligible conflicting key is inspected and reviewed first; only then can the interval be validated."""
    setUp = tvi.IntervalTests.setUp
    tearDown = tvi.IntervalTests.tearDown
    run_case = tvi.IntervalTests.run_case
    OUT = 900000001

    def variants(self, fan=None):
        a = {'event': 'messages.sent', 'account_id': v.PROVIDER, 'payload': {
            'id': 777001, 'toUser': {'id': fan or self.OUT}, 'text': 'first', 'createdAt': '2026-11-12T09:00:00+00:00',
            'isFromQueue': True, 'isSentByMe': True}}
        b = copy.deepcopy(a)
        b['payload']['text'] = 'second'
        return a, b

    def conflict(self, reviewed, fan=None, extra_digest=False):
        a, b = self.variants(fan)
        raws = [json.dumps(x, separators=(',', ':')).encode() for x in (a, b)]
        self.rows.append({'idempotency_key': 'dup', 'event': 'messages.sent', 'payload': b, 'succeeded': True,
                          'status_code': 202, 'url': v.URL})
        hashes = [sha(x) for x in raws]
        self.local['digests'] = [{'key': 'dup', 'hash': h, 'hold': True} for h in hashes]
        if extra_digest:
            self.local['digests'].append({'key': 'dup', 'hash': 'c' * 64, 'hold': True})
        if reviewed:
            self.local['conflict_reviews'] = [{'before': {
                'eventKey': 'dup', 'providerAccount': v.PROVIDER, 'payloadSha256s': sorted(hashes),
                'rawBodies': [x.decode() for x in raws], 'trialFans': sorted(v.FANS)},
                'result': {'outcome': 'conflicting_event_group_reviewed', 'resolved': True}}]

    def test_an_unreviewed_conflict_names_the_key_and_says_to_review_it_first(self):
        self.conflict(reviewed=False)
        with self.assertRaises(RuntimeError) as caught:
            self.run_case()
        text = str(caught.exception)
        self.assertIn('dup', text)
        self.assertIn('review', text)
        self.assertIn('first', text)
        self.assertIn('resolve_conflicting_webhook', text)

    def test_after_the_audited_review_the_same_interval_validates_and_reports_it(self):
        self.conflict(reviewed=True)
        result = self.run_case()
        self.assertTrue(result['interval_valid_for_bounded_scope'])
        self.assertIn('audited_conflicting_event_review', {f['disposition'] for f in result['outside_scope_limitations']})

    def test_a_variant_that_arrived_after_the_review_is_unreviewed_again(self):
        self.conflict(reviewed=True, extra_digest=True)
        with self.assertRaises(RuntimeError):
            self.run_case()

    def test_a_review_cannot_cover_a_variant_that_touches_a_trial_fan(self):
        self.conflict(reviewed=True, fan=sorted(v.FANS)[0])
        with self.assertRaises(RuntimeError):
            self.run_case()


if __name__ == '__main__':
    unittest.main()
