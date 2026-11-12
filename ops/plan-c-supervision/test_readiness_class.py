"""readiness_unavailable monitor-incident class (ops incident owner and resolver).

The 11 Nov precedent added classes by name; this class is different. A stopped sender loop can leave
an unresolved or unknown send behind, and its cause is unproven, so it resolves only through its own
receipt version (v3) that carries: an operator evidence reference, the exact set of unresolved sends
the operator names, and the unproven-cause caveat. Fail closed everywhere else.

Lab note: the host-root-only marker-sharing step and the resolver chown cannot run as uid 1000, so
share_verified_marker and os.chown are stubbed in these tests only, exactly as in test_class_resolution.py
and test_class_flow.py. Every other step is the real code.
"""
import base64
import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import incident_state as m
import resolve_monitor_incident as r
import validate_interval as v

READINESS = 'readiness_unavailable'
START = '2026-11-12T10:00:00+00:00'
END = '2026-11-12T13:00:00+00:00'
DONE = '2026-11-12T13:01:00+00:00'
REF = 'operator:fansapi:journal 09:15:30 reviewed, sender container absent, scope stopped'
STOP = b'Nov 12 09:15:30 pilot_supervisor_stopped:readiness_unavailable\n'


def marker_bytes():
    return m.encoded({'incident_id': 'a' * 32, 'last_confirmed_healthy_utc': START, 'resolved': False,
                      'detected_utc': '2026-11-12T09:15:31+00:00'})


class Constants(unittest.TestCase):
    def test_class_is_registered_and_the_caveat_names_the_unproven_cause(self):
        self.assertIn(READINESS, m.CLASSES)
        self.assertEqual(m.READINESS, READINESS)
        self.assertNotIn(READINESS, m.V2_CLASSES)           # the thin v2 receipt can never carry it
        self.assertEqual(set(m.V2_CLASSES) | {READINESS}, set(m.CLASSES))
        for word in ('UNPROVEN', 'not retained', 'does not prove', 'grants no send'):
            self.assertIn(word, m.READINESS_CAVEAT)
        # The original HTTP status/code/body were lost: never state or suggest one as fact.
        for word in ('503', 'loop_stale'):
            self.assertNotIn(word, m.READINESS_CAVEAT)
        self.assertTrue(m.READINESS_CAVEAT.isascii())

    def test_journal_class_accepts_exactly_one_readiness_stop_and_nothing_else(self):
        self.assertEqual(m.journal_class(STOP), READINESS)
        refused = (b'', b'pilot_supervisor_stopped:readiness_malformed',
                   STOP + STOP,                                                        # repeated stop
                   STOP + b'pilot_supervisor_stopped:network_check_failed\n',          # a second, different class
                   b'pilot_supervisor_stopped:unexpected_failure\n' + STOP,            # unknown failure alongside
                   STOP + b'pilot_supervisor_stopped:credit_early_stop\n')
        for raw in refused:
            with self.subTest(raw=raw):
                with self.assertRaises(RuntimeError):
                    m.journal_class(raw)

    def test_evidence_reference_rules(self):
        self.assertTrue(m.evidence_reference_ok(REF))
        for bad in ('', ' ', 'operator:fansapi:', 'operator:fansapi:     ', 'operator:fansapi:abc',
                    'journal reviewed, nothing else', 'operator:fansapi:' + 'x' * 109,
                    'operator:fansapi:line\nbreak here', 'operator:fansapi:tab\there ok', None, 5, b'operator:fansapi:bytes ref'):
            with self.subTest(bad=bad):
                self.assertFalse(m.evidence_reference_ok(bad))

    def test_named_send_rules(self):
        self.assertTrue(m.named_sends_ok([]))
        self.assertTrue(m.named_sends_ok(['0a1b2c3d-0000-0000-0000-000000000001', 'b-2']))
        for bad in (['b', 'a'], ['a', 'a'], ('a',), 'a', [1], [''], ['has space'], ['x' * 37], [None], {'a': 1}, None):
            with self.subTest(bad=bad):
                self.assertFalse(m.named_sends_ok(bad))

    def test_command_key_v3_binds_reference_and_named_sends_and_differs_from_v2(self):
        base = ('i' * 64, 'e' * 64, START, END, 40, REF, ['a-1'])
        key = m.command_key_v3(*base)
        self.assertRegex(key, '^[a-f0-9]{64}$')
        for i, change in enumerate((('j' * 64,), ('f' * 64,), ('2026-11-12T10:01:00+00:00',), ('2026-11-12T13:01:00+00:00',), (41,),
                                    (REF + ' more',), (['a-1', 'a-2'],))):
            args = list(base); args[i] = change[0]
            self.assertNotEqual(m.command_key_v3(*args), key, i)
        self.assertNotEqual(key, m.command_key_v2(*base[:5]))


class ReadinessReceipt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.raw = marker_bytes(); self.marker = self.root / 'outage-open.json'; self.marker.write_bytes(self.raw)
        self.h = m.digest(self.raw); self.audit = []
        self.share = patch.object(m, 'share_verified_marker'); self.share.start()
        self.receipt = self.make()

    def tearDown(self):
        self.share.stop(); self.tmp.cleanup()

    def make(self, **over):
        named = over.pop('named_sends', [])
        ref = over.pop('evidence_reference', REF)
        r3 = {'version': m.V3, 'incident_sha256': self.h, 'incident_id': 'a' * 32, 'account_id': m.ACCOUNT,
              'binding_id': m.BINDING, 'connection_epoch': 1, 'scope_revision': 40, 'classification': READINESS,
              'affected_interval': {'start': START, 'end': END}, 'delivery_url_sha256': 'd' * 64,
              'config_version': 'e' * 64, 'actor': 'op-session:' + 'a' * 64, 'evidence_sha256': 'c' * 64,
              'resolved_at': DONE, 'evidence_reference': ref, 'named_sends': named, 'cause_caveat': m.READINESS_CAVEAT}
        r3.update(over)
        if 'audit_command_key' not in over:
            r3['audit_command_key'] = m.command_key_v3(r3['incident_sha256'], r3['evidence_sha256'],
                r3['affected_interval']['start'], r3['affected_interval']['end'], r3['scope_revision'],
                r3['evidence_reference'], r3['named_sends'])
        return r3

    def test_valid_receipt_resolves_audit_first_replays_and_keeps_the_caveat_in_both_records(self):
        self.assertTrue(m.unresolved(self.marker))
        self.assertEqual(m.resolve(self.root, self.receipt, self.audit.append), 'resolved')
        self.assertFalse(m.unresolved(self.marker))
        self.assertEqual(m.resolve(self.root, self.receipt, self.audit.append), 'replayed')
        self.assertEqual(len(self.audit), 1)
        saved = json.loads((self.root / 'incident-resolution.json').read_bytes())
        archived = json.loads((self.root / 'incident-history' / ('resolution-' + self.receipt['audit_command_key'] + '.json')).read_bytes())
        for record in (saved, archived, self.audit[0]):
            self.assertEqual(record['cause_caveat'], m.READINESS_CAVEAT)
            self.assertEqual(record['evidence_reference'], REF)
        self.assertEqual(m.resolved_intervals(self.root), [(START, END)])

    def test_named_sends_are_part_of_the_valid_receipt(self):
        named = self.make(named_sends=['4f1c-unknown-send'])
        self.assertTrue(m._valid_resolution(named, self.h, self.raw))
        self.assertEqual(m.resolve(self.root, named, self.audit.append), 'resolved')

    def test_v2_receipt_can_never_carry_the_readiness_class(self):
        v2 = {k: val for k, val in self.receipt.items() if k not in ('evidence_reference', 'named_sends', 'cause_caveat')}
        v2.update(version=m.V2, audit_command_key=m.command_key_v2(self.h, 'c' * 64, START, END, 40))
        self.assertFalse(m._valid_resolution(v2, self.h, self.raw))
        with self.assertRaises(RuntimeError):
            m.resolve(self.root, v2, self.audit.append)

    def test_v3_receipt_can_never_carry_another_class(self):
        for cls in ('credit_monitor_unavailable', 'provider_webhook_health_unavailable', 'sender_not_ready',
                    'network_check_failed', 'unsupported_monitor_failure'):
            with self.subTest(cls=cls):
                self.assertFalse(m._valid_resolution(self.make(classification=cls), self.h, self.raw))

    def test_every_missing_weakened_or_unbound_field_is_refused(self):
        stale_key = self.receipt['audit_command_key']
        bad = {
            'no reference': self.make(evidence_reference=''),
            'unprefixed reference': self.make(evidence_reference='reviewed it, trust me please'),
            'short reference': self.make(evidence_reference='operator:fansapi:abc'),
            'control char reference': self.make(evidence_reference='operator:fansapi:ok\nok ok ok'),
            'caveat dropped': self.make(cause_caveat=''),
            'caveat softened': self.make(cause_caveat=m.READINESS_CAVEAT.replace('UNPROVEN', 'PROVEN')),
            'caveat padded': self.make(cause_caveat=m.READINESS_CAVEAT + ' Cause confirmed.'),
            'named sends unsorted': self.make(named_sends=['b', 'a']),
            'named sends duplicated': self.make(named_sends=['a', 'a']),
            'named sends not a list': self.make(named_sends='a'),
            'named send malformed': self.make(named_sends=['has space']),
            'reference swapped after keying': self.make(evidence_reference=REF + ' (edited)', audit_command_key=stale_key),
            'named sends added after keying': self.make(named_sends=['late-add'], audit_command_key=stale_key),
            'revision changed after keying': self.make(scope_revision=41, audit_command_key=stale_key),
            'interval over 24h': self.make(affected_interval={'start': START, 'end': '2026-11-13T10:00:01+00:00'}, resolved_at='2026-11-13T10:01:00+00:00'),
            'interval not starting at last healthy': self.make(affected_interval={'start': '2026-11-12T10:05:00+00:00', 'end': END}),
            'interval empty': self.make(affected_interval={'start': START, 'end': START}),
            'interval ends after resolution': self.make(affected_interval={'start': START, 'end': '2026-11-12T14:00:00+00:00'}),
            'wrong incident id': self.make(incident_id='b' * 32),
            'extra field': {**self.receipt, 'extra': 1},
            'wrong actor': self.make(actor='someone'),
            'wrong epoch': self.make(connection_epoch=2),
        }
        for name, receipt in bad.items():
            with self.subTest(name):
                self.assertFalse(m._valid_resolution(receipt, self.h, self.raw))
                with self.assertRaises(RuntimeError):
                    m.resolve(self.root, receipt, self.audit.append)
        for field in ('evidence_reference', 'named_sends', 'cause_caveat'):
            partial = {k: val for k, val in self.receipt.items() if k != field}
            with self.subTest('missing ' + field):
                self.assertFalse(m._valid_resolution(partial, self.h, self.raw))
        self.assertEqual(self.audit, []); self.assertTrue(m.unresolved(self.marker))

    def test_a_later_failure_invalidates_old_readiness_evidence(self):
        m.record_failure(self.root, {'last_confirmed_healthy_utc': START, 'failure': 'second stop'})
        with self.assertRaises(RuntimeError):
            m.resolve(self.root, self.receipt, self.audit.append)
        self.assertEqual(self.audit, [])

    def test_crash_between_audit_and_publication_stays_held(self):
        def crash():
            raise RuntimeError('crash')
        with self.assertRaises(RuntimeError):
            m.resolve(self.root, self.receipt, self.audit.append, after_audit=crash)
        self.assertTrue(m.unresolved(self.marker)); self.assertEqual(m.resolved_intervals(self.root), [])


class ReadinessResolver(unittest.TestCase):
    """The resolver command, with the provider, Docker and root-only steps mocked explicitly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.sup = self.root / 'supervision-v11'; self.sup.mkdir()
        (self.root / 'evidence').mkdir(); (self.root / 'config').mkdir()
        self.raw = marker_bytes(); self.h = m.digest(self.raw); (self.sup / 'outage-open.json').write_bytes(self.raw)
        self.image = 'sha256:' + '1' * 64; self.commit = '2' * 40
        self.put(self.sup / 'reviewed-recovery-release.json', {'image': self.image, 'commit': self.commit})
        self.put(self.sup / 'budget-state.json', {'prior_estimate': 207, 'scope_status': 'stopped', 'last_ok_monotonic': 0})
        self.put(self.root / 'config/pilot-operator-identity.json', {'actor': 'op-session:' + 'a' * 64})
        self.calls = []; self.unresolved_sends = []; self.audited = None; self.classification = READINESS
        self.argv = ['resolver', '--apply', '--evidence-ref', REF]
        self.ps = [patch.object(r, 'ROOT', self.root), patch.object(r, 'SUP', self.sup),
                   patch.object(r, 'assert_sender_stopped'), patch.object(r, 'command', side_effect=self.fake),
                   patch.object(r.os, 'chown'), patch.object(m, 'share_verified_marker')]
        for p in self.ps: p.start()

    def tearDown(self):
        for p in reversed(self.ps): p.stop()
        self.tmp.cleanup()

    def put(self, path, value): path.write_text(json.dumps(value))

    def manifest(self):
        return {'resolution_kind': 'class_based', 'incident_sha256': self.h, 'incident_id': 'a' * 32,
                'classification': self.classification, 'scope_revision': 40, 'delivery_url_sha256': 'd' * 64,
                'config_version': 'e' * 64, 'collected_at': END, 'credit_estimate': 208,
                'sender_absent': True, 'scope_status': 'stopped'}

    def fake(self, args, **kw):
        self.calls.append(args)
        if args[:3] == ['docker', 'image', 'inspect']:
            return json.dumps([{'Id': self.image, 'Config': {'Labels': {'org.opencontainers.image.revision': self.commit}}}]).encode()
        if args[0] == r.sys.executable:
            target = Path(args[2]); target.mkdir()
            self.put(target / 'manifest.json', self.manifest())
            (target / 'journal.txt').write_bytes(STOP)
            self.put(target / 'unresolved-sends.json', self.unresolved_sends)
            return b'{}'
        if '/opt/validate.py' in args: return b'{"interval_valid_for_bounded_scope":true}'
        if '/opt/audit.py' in args:
            evidence = next(p for p in (self.root / 'evidence').iterdir() if p.is_dir())
            self.audited = json.loads((evidence / 'receipt.json').read_bytes())
            return json.dumps({'recorded': True, 'sendingEnabled': False, 'evidenceSha256': self.audited['evidence_sha256']}).encode()
        raise AssertionError('unexpected external operation')

    def run_main(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(r.sys, 'argv', self.argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            r.main()
        return out.getvalue(), err.getvalue()

    def evidence_dir(self):
        return next(p for p in (self.root / 'evidence').iterdir() if p.is_dir())

    def assert_nothing_happened(self):
        self.assertIsNone(self.audited)
        self.assertTrue(m.unresolved(self.sup / 'outage-open.json'))
        self.assertFalse((self.sup / 'incident-resolution.json').exists())

    def test_operator_reference_is_required_and_nothing_is_audited_without_it(self):
        for argv in (['resolver'], ['resolver', '--evidence-ref', 'not-a-reference'], ['resolver', '--evidence-ref'],
                     ['resolver', '--evidence-ref', REF, '--evidence-ref', REF], ['resolver', '--named-send', 'a-1'],
                     ['resolver', '--evidence-ref', REF, '--unknown-flag']):
            with self.subTest(argv=argv):
                self.argv = argv
                for p in (self.root / 'evidence').iterdir(): shutil.rmtree(p)
                with self.assertRaises(RuntimeError):
                    self.run_main()
                self.assert_nothing_happened()

    def test_resolves_with_v3_receipt_prints_the_caveat_and_binds_the_attestation_into_the_evidence_hash(self):
        out, err = self.run_main()
        self.assertFalse(m.unresolved(self.sup / 'outage-open.json'))
        receipt = self.audited
        self.assertEqual((receipt['version'], receipt['classification'], receipt['scope_revision']), (m.V3, READINESS, 40))
        self.assertEqual((receipt['evidence_reference'], receipt['named_sends'], receipt['cause_caveat']), (REF, [], m.READINESS_CAVEAT))
        self.assertEqual(receipt['audit_command_key'], m.command_key_v3(self.h, receipt['evidence_sha256'], START, END, 40, REF, []))
        self.assertIn(m.READINESS_CAVEAT, err)                       # printed for the operator
        self.assertEqual(json.loads(out)['cause_caveat'], m.READINESS_CAVEAT)
        evidence = self.evidence_dir()
        attestation = json.loads((evidence / 'operator-attestation.json').read_bytes())
        self.assertEqual(attestation, {'cause_caveat': m.READINESS_CAVEAT, 'evidence_reference': REF, 'named_sends': []})
        sources = json.loads((evidence / 'source-hashes.json').read_bytes())
        self.assertEqual(sources['operator-attestation.json'], m.digest((evidence / 'operator-attestation.json').read_bytes()))
        self.assertEqual(receipt['evidence_sha256'], m.digest(m.encoded(sources)))
        self.assertTrue(json.loads(out)['sendingEnabled'] is False)

    def test_unresolved_sends_must_be_named_exactly_before_anything_is_audited(self):
        self.unresolved_sends = ['intent-unknown-1']
        with self.assertRaises(RuntimeError):
            self.run_main()                                             # not named at all
        self.assert_nothing_happened()
        for p in (self.root / 'evidence').iterdir(): shutil.rmtree(p)
        self.argv = ['resolver', '--evidence-ref', REF, '--named-send', 'intent-unknown-1', '--named-send', 'intent-extra-9']
        with self.assertRaises(RuntimeError):
            self.run_main()                                             # names a send that is not unresolved
        self.assert_nothing_happened()
        for p in (self.root / 'evidence').iterdir(): shutil.rmtree(p)
        self.argv = ['resolver', '--apply', '--evidence-ref', REF, '--named-send', 'intent-unknown-1']
        self.run_main()
        self.assertEqual(self.audited['named_sends'], ['intent-unknown-1'])
        self.assertFalse(m.unresolved(self.sup / 'outage-open.json'))

    def test_attestation_flags_are_refused_for_every_other_class(self):
        self.classification = 'sender_not_ready'
        with self.assertRaises(RuntimeError):
            self.run_main()
        self.assert_nothing_happened()

    def test_resume_keeps_the_attested_receipt_and_replay_makes_no_new_collection(self):
        def crash(_):
            raise RuntimeError('crash after audit')
        with patch.object(m, 'resolve', side_effect=lambda root, receipt, audit, **kw: (audit(receipt), crash(1))):
            with self.assertRaises(RuntimeError):
                self.run_main()
        evidence = self.evidence_dir()
        self.assertTrue(m.unresolved(self.sup / 'outage-open.json'))
        self.argv = ['resolver', '--apply', '--resume', str(evidence)]
        out, err = self.run_main()
        self.assertFalse(m.unresolved(self.sup / 'outage-open.json'))
        self.assertIn(m.READINESS_CAVEAT, err)
        count = len([c for c in self.calls if c[0] == r.sys.executable])
        self.argv = ['resolver']
        self.run_main()                                                 # already resolved: replay, no recollection
        self.assertEqual(len([c for c in self.calls if c[0] == r.sys.executable]), count)


    def test_default_preview_does_not_audit_publish_or_change_budget_and_applies_only_explicitly(self):
        self.argv=['resolver','--evidence-ref',REF]
        before=(self.sup/'budget-state.json').read_bytes()
        out,_=self.run_main()
        shown=json.loads(out)
        # Collecting and validating evidence is a preview, not a dry run; nothing is audited or published.
        self.assertEqual(shown['result'],'preview')
        self.assertIs(shown['audited'],False);self.assertIs(shown['published'],False);self.assertIs(shown['applied'],False)
        self.assert_nothing_happened()
        self.assertEqual((self.sup/'budget-state.json').read_bytes(),before)
        evidence=self.evidence_dir()
        self.argv=['resolver','--apply','--resume',str(evidence)]
        out,_=self.run_main()
        self.assertIs(json.loads(out)['applied'],True)
        self.assertIsNotNone(self.audited)
        self.assertFalse(m.unresolved(self.sup/'outage-open.json'))

    def test_changed_preview_evidence_refuses_before_apply_audit(self):
        self.argv=['resolver','--evidence-ref',REF]
        self.run_main();evidence=self.evidence_dir()
        (evidence/'journal.txt').write_text('changed retained source')
        self.argv=['resolver','--apply','--resume',str(evidence)]
        with self.assertRaises(RuntimeError):self.run_main()
        self.assert_nothing_happened()


class ReadinessIntervalValidation(unittest.TestCase):
    """validate_interval runs in the reviewed image with no network; it re-proves the journal and the sends."""

    def setUp(self):
        self.t = tempfile.TemporaryDirectory(); self.p = Path(self.t.name)
        payload = {'event': 'messages.received', 'account_id': v.PROVIDER, 'payload': {'fromUser': {'id': sorted(v.FANS)[0]}, 'text': 'x'}}
        raw = json.dumps(payload, separators=(',', ':')).encode()
        self.row = {'idempotency_key': 'k1', 'event': 'messages.received', 'payload': payload, 'succeeded': True,
                    'status_code': 202, 'url': v.URL}
        self.local = {'receipts': [{'key': 'k1', 'raw': base64.b64encode(raw).decode(), 'hash': hashlib.sha256(raw).hexdigest(),
                                    'status': 'accepted', 'projection': 'projected'}], 'digests': []}
        self.manifest = {'resolution_kind': 'class_based', 'incident_sha256': 'f' * 64, 'incident_id': 'a' * 32,
                         'classification': READINESS, 'scope_revision': 40, 'delivery_url': v.URL,
                         'delivery_url_sha256': hashlib.sha256(v.URL.encode()).hexdigest(), 'config_version': 'e' * 64,
                         'delivery_page_count': 1, 'sender_absent': True, 'scope_status': 'stopped'}
        self.journal = STOP; self.sends = []
        self.attestation = {'cause_caveat': m.READINESS_CAVEAT, 'evidence_reference': REF, 'named_sends': []}
        (self.p / 'deliveries-page-1.json').write_text('{}')

    def tearDown(self): self.t.cleanup()

    def run_case(self, drop=()):
        files = {'manifest.json': {'provider_delivery_count': 1, **self.manifest},
                 'deliveries.json': {'data': [self.row], '_pagination': {'next_page': None, 'pages': 1, 'complete': True}},
                 'local-receipts.json': self.local, 'unresolved-sends.json': self.sends,
                 'operator-attestation.json': self.attestation}
        for name, obj in files.items():
            if name not in drop: (self.p / name).write_text(json.dumps(obj))
            elif (self.p / name).exists(): (self.p / name).unlink()
        if 'journal.txt' not in drop: (self.p / 'journal.txt').write_bytes(self.journal)
        elif (self.p / 'journal.txt').exists(): (self.p / 'journal.txt').unlink()   # test-helper fix: a dropped file must really be absent
        return v.validate(self.p)

    def test_good_readiness_evidence_validates_and_reports_the_caveat(self):
        out = self.run_case()
        self.assertTrue(out['interval_valid_for_bounded_scope'])
        self.assertEqual(out['readiness']['cause_caveat'], m.READINESS_CAVEAT)
        self.assertEqual(out['readiness']['unresolved_sends'], [])
        self.sends = ['intent-1']; self.attestation['named_sends'] = ['intent-1']
        self.assertEqual(self.run_case()['readiness']['unresolved_sends'], ['intent-1'])

    def test_journal_must_show_exactly_the_readiness_stop(self):
        for journal in (b'', STOP + STOP, STOP + b'pilot_supervisor_stopped:network_check_failed\n',
                        b'pilot_supervisor_stopped:sender_not_ready\n'):
            with self.subTest(journal=journal):
                self.journal = journal
                with self.assertRaises(RuntimeError): self.run_case()

    def test_sender_absent_and_stopped_scope_must_be_recorded_true(self):
        for change in ({'sender_absent': False}, {'sender_absent': None}, {'scope_status': 'active'}, {'scope_status': 'configured'}):
            with self.subTest(change=change):
                saved = dict(self.manifest); self.manifest.update(change)
                with self.assertRaises(RuntimeError): self.run_case()
                self.manifest = saved
        for key in ('sender_absent', 'scope_status'):
            saved = dict(self.manifest); del self.manifest[key]
            with self.subTest(missing=key):
                with self.assertRaises(RuntimeError): self.run_case()
            self.manifest = saved

    def test_unresolved_sends_must_equal_the_named_sends(self):
        self.sends = ['intent-1']                                         # not named
        with self.assertRaises(RuntimeError): self.run_case()
        self.sends = []; self.attestation['named_sends'] = ['intent-1']   # named but not unresolved
        with self.assertRaises(RuntimeError): self.run_case()
        self.sends = ['intent-1', 'intent-2']; self.attestation['named_sends'] = ['intent-1']
        with self.assertRaises(RuntimeError): self.run_case()

    def test_missing_or_altered_operator_evidence_is_refused(self):
        for name in ('operator-attestation.json', 'unresolved-sends.json', 'journal.txt'):
            with self.subTest(missing=name):
                with self.assertRaises(RuntimeError): self.run_case(drop=(name,))
        for change in ({'evidence_reference': 'no prefix here at all'}, {'evidence_reference': ''},
                       {'cause_caveat': 'cause confirmed'}, {'cause_caveat': ''}):
            with self.subTest(change=change):
                saved = dict(self.attestation); self.attestation.update(change)
                with self.assertRaises(RuntimeError): self.run_case()
                self.attestation = saved

    def test_other_classes_do_not_need_or_accept_readiness_files(self):
        self.manifest['classification'] = 'sender_not_ready'
        out = self.run_case()
        self.assertNotIn('readiness', out)


if __name__ == '__main__':
    unittest.main()
