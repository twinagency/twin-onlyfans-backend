"""Class-based manual recovery command and interval validation (provider/Docker mocked explicitly).

Lab note: the resolver chowns evidence to root:65532 and the marker-sharing step needs root; both
are stubbed here only (uid 1000 cannot chown, same as the 18 pre-existing failures).
"""
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import incident_state as st
import resolve_monitor_incident as r
import validate_interval as v

CLASS = 'provider_webhook_health_unavailable'
START = '2026-11-10T00:00:00+00:00'
END = '2026-11-10T00:40:00+00:00'


class ClassResolver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.sup = self.root / 'supervision-v11'; self.sup.mkdir()
        (self.root / 'evidence').mkdir(); (self.root / 'config').mkdir()
        self.raw = st.encoded({'incident_id': 'a' * 32, 'last_confirmed_healthy_utc': START, 'resolved': False})
        self.h = st.digest(self.raw); (self.sup / 'outage-open.json').write_bytes(self.raw)
        self.image = 'sha256:' + '1' * 64; self.commit = '2' * 40
        self.write(self.sup / 'reviewed-recovery-release.json', {'image': self.image, 'commit': self.commit})
        self.write(self.sup / 'budget-state.json', {'prior_estimate': 207, 'scope_status': 'stopped', 'last_ok_monotonic': 0})
        self.write(self.root / 'config/pilot-operator-identity.json', {'actor': 'op-session:' + 'a' * 64})
        self.calls = []; self.manifest = {}
        self.ps = [patch.object(r, 'ROOT', self.root), patch.object(r, 'SUP', self.sup),
                   patch.object(r, 'assert_sender_stopped'), patch.object(r, 'command', side_effect=self.fake),
                   patch.object(r.sys, 'argv', ['resolver']), patch.object(r.os, 'chown'),
                   patch.object(st, 'share_verified_marker')]
        for p in self.ps: p.start()

    def tearDown(self):
        for p in reversed(self.ps): p.stop()
        self.tmp.cleanup()

    def write(self, path, value): path.write_text(json.dumps(value))

    def good_manifest(self):
        return {'resolution_kind': 'class_based', 'incident_sha256': self.h, 'incident_id': 'a' * 32, 'classification': CLASS,
                'scope_revision': 31, 'delivery_url_sha256': 'd' * 64, 'config_version': 'e' * 64, 'collected_at': END,
                'credit_estimate': 208}

    def fake(self, args, **kw):
        self.calls.append(args)
        if args[:3] == ['docker', 'image', 'inspect']:
            return json.dumps([{'Id': self.image, 'Config': {'Labels': {'org.opencontainers.image.revision': self.commit}}}]).encode()
        if args[0] == r.sys.executable:
            target = Path(args[2]); target.mkdir()
            self.write(target / 'manifest.json', self.manifest or self.good_manifest())
            (target / 'journal.txt').write_text('fixture')
            return b'{}'
        if '/opt/validate.py' in args: return b'{"interval_valid_for_bounded_scope":true}'
        if '/opt/audit.py' in args:
            evidence = next(p for p in (self.root / 'evidence').iterdir() if p.is_dir())
            proof = json.loads((evidence / 'receipt.json').read_bytes())
            self.audited = proof
            return json.dumps({'recorded': True, 'sendingEnabled': False, 'evidenceSha256': proof['evidence_sha256']}).encode()
        raise AssertionError('unexpected external operation')

    def test_unpinned_incident_resolves_with_v2_receipt_stays_stopped_and_replays_without_recollecting(self):
        r.main()
        self.assertFalse(st.unresolved(self.sup / 'outage-open.json'))
        receipt = self.audited
        self.assertEqual((receipt['version'], receipt['scope_revision'], receipt['classification']), (st.V2, 31, CLASS))
        self.assertEqual(receipt['affected_interval'], {'start': START, 'end': END})
        self.assertEqual(receipt['audit_command_key'], st.command_key_v2(self.h, receipt['evidence_sha256'], START, END, 31))
        budget = json.loads((self.sup / 'budget-state.json').read_bytes())
        self.assertEqual((budget['prior_estimate'], budget['scope_status']), (208, 'stopped'))
        count = len([c for c in self.calls if c[0] == r.sys.executable])
        r.main()
        self.assertEqual(len([c for c in self.calls if c[0] == r.sys.executable]), count)

    def test_evidence_for_a_different_incident_class_or_identity_is_refused(self):
        for change in ({'incident_sha256': 'f' * 64}, {'incident_id': 'b' * 32}, {'classification': 'network_check_failed'},
                       {'resolution_kind': 'pinned'}, {'scope_revision': 'x'}):
            self.manifest = {**self.good_manifest(), **change}
            with self.subTest(change=change):
                for p in (self.root / 'evidence').iterdir():
                    import shutil; shutil.rmtree(p)
                with self.assertRaises(RuntimeError): r.main()
                self.assertTrue(st.unresolved(self.sup / 'outage-open.json'))


class IntervalValidation(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory(); self.p = Path(self.t.name)
        payload = {'event': 'messages.received', 'account_id': v.PROVIDER, 'payload': {'fromUser': {'id': 318022380}, 'text': 'x'}}
        raw = json.dumps(payload, separators=(',', ':')).encode()
        self.row = {'idempotency_key': 'k1', 'event': 'messages.received', 'payload': payload, 'succeeded': True,
                    'status_code': 202, 'url': v.URL}
        self.local = {'receipts': [{'key': 'k1', 'raw': base64.b64encode(raw).decode(), 'hash': hashlib.sha256(raw).hexdigest(),
                                    'status': 'accepted', 'projection': 'projected'}], 'digests': []}
        self.rows = [self.row]; self.pages = {'next_page': None, 'pages': 1, 'complete': True}
        self.manifest = {'resolution_kind': 'class_based', 'incident_sha256': 'f' * 64, 'incident_id': 'a' * 32,
                         'classification': CLASS, 'scope_revision': 31, 'delivery_url': v.URL,
                         'delivery_url_sha256': hashlib.sha256(v.URL.encode()).hexdigest(), 'config_version': 'e' * 64,
                         'delivery_page_count': 1}
        (self.p / 'deliveries-page-1.json').write_text('{}')

    def tearDown(self): self.t.cleanup()

    def run_case(self):
        for name, obj in [('manifest.json', {'provider_delivery_count': len(self.rows), **self.manifest}),
                          ('deliveries.json', {'data': self.rows, '_pagination': self.pages}), ('local-receipts.json', self.local)]:
            (self.p / name).write_text(json.dumps(obj))
        return v.validate(self.p)

    def test_class_based_interval_validates_without_the_pinned_anchor(self):
        self.assertTrue(self.run_case()['interval_valid_for_bounded_scope'])

    def test_changed_url_subscription_pages_or_class_reject_stale_evidence(self):
        for change in ({'delivery_url': 'https://old.example/x'}, {'delivery_url_sha256': 'a' * 64}, {'config_version': 'zz'},
                       {'delivery_page_count': 2}, {'classification': 'network_check_failed'}, {'incident_id': 'bad'},
                       {'incident_sha256': next(iter(st.pinned_incidents()))}):
            with self.subTest(change=change):
                saved = dict(self.manifest); self.manifest.update(change)
                with self.assertRaises(RuntimeError): self.run_case()
                self.manifest = saved

    def test_incomplete_or_duplicated_page_evidence_is_rejected(self):
        self.pages = {'next_page': None, 'pages': 1, 'complete': False}
        with self.assertRaises(RuntimeError): self.run_case()
        self.pages = {'next_page': None, 'pages': 1, 'complete': True}
        self.rows = [self.row, dict(self.row)]
        with self.assertRaises(RuntimeError): self.run_case()
        self.rows = []
        with self.assertRaises(RuntimeError): self.run_case()      # empty interval is not evidence

    def test_failed_attempt_needs_a_verified_successful_retry_of_the_same_key(self):
        failed = {**self.row, 'succeeded': False, 'status_code': 500, 'id': 'attempt-1'}
        self.rows = [failed, {**self.row, 'id': 'attempt-2'}]
        out = self.run_case()
        self.assertEqual(out['superseded_failed_attempts'], ['k1'])
        self.rows = [failed]
        with self.assertRaises(RuntimeError): self.run_case()
        self.rows = [{**failed, 'idempotency_key': 'other'}, self.row]
        with self.assertRaises(RuntimeError): self.run_case()


if __name__ == '__main__':
    unittest.main()
