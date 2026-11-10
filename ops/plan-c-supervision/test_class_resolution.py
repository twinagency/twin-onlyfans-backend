"""Class/evidence-based incident resolution in the existing incident owner.

Lab note: the host-root-only marker-sharing step (chown to 0:65532) cannot run as uid 1000, so
share_verified_marker is stubbed in these tests only; every other step (lock, audit ordering,
immutable history, atomic pointer replacement, replay) is the real code.
"""
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import delivery_history as h
import incident_state as m
import test_delivery_history as th

CLASS = 'provider_webhook_health_unavailable'
START = '2026-11-10T00:00:00+00:00'
END = '2026-11-10T00:40:00+00:00'
DONE = '2026-11-10T00:41:00+00:00'


def marker_bytes(**extra):
    return m.encoded({'incident_id': 'a' * 32, 'last_confirmed_healthy_utc': START, 'resolved': False,
                      'detected_utc': '2026-11-10T00:10:00+00:00', **extra})


class ClassResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.raw = marker_bytes(); self.marker = self.root / 'outage-open.json'; self.marker.write_bytes(self.raw)
        self.h = m.digest(self.raw); self.audit = []
        self.share = patch.object(m, 'share_verified_marker'); self.share.start()
        self.receipt = self.make()

    def tearDown(self):
        self.share.stop(); self.tmp.cleanup()

    def make(self, **over):
        r = {'version': m.V2, 'incident_sha256': self.h, 'incident_id': 'a' * 32, 'account_id': m.ACCOUNT,
             'binding_id': m.BINDING, 'connection_epoch': 1, 'scope_revision': 31, 'classification': CLASS,
             'affected_interval': {'start': START, 'end': END}, 'delivery_url_sha256': 'd' * 64,
             'config_version': 'e' * 64, 'actor': 'op-session:' + 'a' * 64, 'evidence_sha256': 'c' * 64,
             'resolved_at': DONE}
        r.update(over)
        if 'audit_command_key' not in over:
            r['audit_command_key'] = m.command_key_v2(r['incident_sha256'], r['evidence_sha256'],
                                                      r['affected_interval']['start'], r['affected_interval']['end'], r['scope_revision'])
        return r

    def test_unpinned_incident_resolves_with_audit_first_and_replays_idempotently(self):
        self.assertIsNone(m.incident_spec(self.h))                      # not one of the three pinned hashes
        self.assertTrue(m.unresolved(self.marker))
        self.assertEqual(m.resolve(self.root, self.receipt, self.audit.append), 'resolved')
        self.assertFalse(m.unresolved(self.marker)); self.assertEqual(self.marker.read_bytes(), self.raw)
        self.assertEqual(m.resolve(self.root, self.receipt, self.audit.append), 'replayed')
        self.assertEqual(len(self.audit), 1)

    def test_resolution_never_creates_a_permit_or_clears_other_holds(self):
        other = self.root / 'other-hold.json'; other.write_text('{"hold":"fan"}')
        m.resolve(self.root, self.receipt, self.audit.append)
        self.assertEqual(other.read_text(), '{"hold":"fan"}')
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.is_file()),
                         ['incident-resolution.json', 'incident.lock', 'other-hold.json', 'outage-open.json'])

    def test_crash_before_durable_audit_commit_stays_held_and_publishes_nothing(self):
        def reject(_): raise RuntimeError('database audit did not commit')
        with self.assertRaises(RuntimeError): m.resolve(self.root, self.receipt, reject)
        self.assertTrue(m.unresolved(self.marker)); self.assertFalse((self.root / 'incident-resolution.json').exists())

    def test_crash_after_durable_audit_commit_stays_held_then_retry_replays_same_audit(self):
        def crash(): raise RuntimeError('crash after audit commit')
        with self.assertRaises(RuntimeError): m.resolve(self.root, self.receipt, self.audit.append, after_audit=crash)
        self.assertEqual(len(self.audit), 1); self.assertTrue(m.unresolved(self.marker))
        self.assertEqual(m.resolve(self.root, self.receipt, self.audit.append), 'resolved')
        self.assertEqual({a['audit_command_key'] for a in self.audit}, {self.receipt['audit_command_key']})   # same command identity

    def test_replay_long_after_resolution_does_not_need_fresh_evidence_or_a_second_audit(self):
        m.resolve(self.root, self.receipt, self.audit.append)
        # ops owner has no wall-clock gate: replay is by committed identity (freshness is the DB owner's first-commit rule)
        self.assertEqual(m.resolve(self.root, self.receipt, self.audit.append), 'replayed')
        self.assertEqual(len(self.audit), 1)

    def test_changed_incident_rejects_old_evidence(self):
        m.record_failure(self.root, {'last_confirmed_healthy_utc': START, 'failure': 'new'})
        with self.assertRaises(RuntimeError): m.resolve(self.root, self.receipt, self.audit.append)
        self.assertTrue(m.unresolved(self.marker)); self.assertEqual(self.audit, [])

    def test_stale_receipt_fields_are_rejected(self):
        bad = [self.make(scope_revision=32, audit_command_key=self.receipt['audit_command_key']),   # revision changed, key not re-derived
               self.make(incident_id='b' * 32), self.make(classification='network_check_failed'),
               self.make(affected_interval={'start': '2026-11-10T00:05:00+00:00', 'end': END}),     # does not start at last healthy
               self.make(affected_interval={'start': START, 'end': '2026-11-10T00:50:00+00:00'}),   # ends after resolved_at
               self.make(affected_interval={'start': START, 'end': START}),
               self.make(affected_interval={'start': START, 'end': '2026-11-20T00:00:00+00:00'}, resolved_at='2026-11-21T00:00:00+00:00'),
               self.make(version=m.V1), self.make(actor='someone'), self.make(connection_epoch=2),
               self.make(config_version='nothex'), self.make(delivery_url_sha256='x'),
               {**self.receipt, 'extra': 1}]
        for i, receipt in enumerate(bad):
            with self.subTest(i=i):
                with self.assertRaises(RuntimeError): m.resolve(self.root, receipt, self.audit.append)
        self.assertEqual(self.audit, []); self.assertTrue(m.unresolved(self.marker))

    def test_marker_without_durable_identity_cannot_be_class_resolved(self):
        for raw in (b'{"resolved": false}', m.encoded({'incident_id': 'a' * 32, 'last_confirmed_healthy_utc': None}),
                    m.encoded({'incident_id': 'zz', 'last_confirmed_healthy_utc': START})):
            self.marker.write_bytes(raw); digest = m.digest(raw)
            self.assertIsNone(m.incident_spec(digest, raw))
            with self.assertRaises(RuntimeError): m.resolve(self.root, self.make(incident_sha256=digest), self.audit.append)
            self.assertTrue(m.unresolved(self.marker))

    def test_pinned_incidents_keep_exact_specs_and_cannot_use_v2(self):
        self.assertEqual(m.pinned_incidents(), {
            'c59fd2459671cc3e0d3a82a1a8de26b9675cc24a4e0567e3f712ea3b89016725': {'classification': 'credit_monitor_unavailable', 'scope_revision': 12},
            'c240c781574ad075939782179ab926c9f0a69cc3fc40a292f81f7d3f57e3ad14': {'classification': CLASS, 'scope_revision': 16},
            'df20805ad9525b3ccc54b5dba161940cbdca78bda053d916d0578bbb42a27f33': {'classification': CLASS, 'scope_revision': 20}})
        for pinned in m.pinned_incidents():
            spec = m.incident_spec(pinned)
            self.assertTrue(spec['pinned'])
            self.assertFalse(m._valid_resolution(self.make(incident_sha256=pinned), pinned, self.raw))

    def test_journal_class_requires_exactly_one_known_failure(self):
        self.assertEqual(m.journal_class(b'x pilot_supervisor_stopped:provider_webhook_health_unavailable\n'), CLASS)
        for raw in (b'', b'pilot_supervisor_stopped:network_check_failed',
                    b'pilot_supervisor_stopped:%s\npilot_supervisor_stopped:%s' % (CLASS.encode(), CLASS.encode())):
            with self.assertRaises(RuntimeError): m.journal_class(raw)

    def test_resolved_interval_stops_a_resolved_failure_from_holding_future_delivery_checks(self):
        m.resolve(self.root, self.receipt, self.audit.append)
        intervals = m.resolved_intervals(self.root)
        self.assertEqual(intervals, [(START, END)])
        base = datetime.datetime.fromisoformat(START)
        def at(minutes): return base + datetime.timedelta(minutes=minutes)
        failed = th.row(1, 0, ok=False); failed['created_at'] = h.fmt(at(20))
        good = th.row(2, 0); good['created_at'] = h.fmt(at(30))
        provider = th.Provider([failed, good])
        base_dir = self.root / 'h'; base_dir.mkdir()
        intent = base_dir / 'i.json'; intent.write_text('{}')
        import os; os.utime(intent, ((base - datetime.timedelta(hours=1)).timestamp(),) * 2)
        kw = dict(boundary_path=base_dir / 'b.json', checkpoint_path=base_dir / 'c.json', intent_path=intent,
                  webhook_id='wh_1', url=th.URL, subscription=th.SUBSCRIPTION, events=th.EVENTS, account_id=th.ACCT, now=at(60))
        with self.assertRaises(h.HistoryError):                     # without the audited resolution the failure holds
            h.verify_history(provider, resolved_intervals=[], **kw)
        h.verify_history(provider, resolved_intervals=intervals, **kw)
        later = th.row(3, 0); later['created_at'] = h.fmt(at(35)); provider.rows.append(later)
        h.verify_history(provider, resolved_intervals=intervals, **{**kw, 'now': at(61)})   # still not poisoned

    def test_unresolved_or_unpublished_resolution_contributes_no_interval(self):
        self.assertEqual(m.resolved_intervals(self.root), [])
        def crash(): raise RuntimeError('crash')
        with self.assertRaises(RuntimeError): m.resolve(self.root, self.receipt, self.audit.append, after_audit=crash)
        self.assertEqual(m.resolved_intervals(self.root), [])


if __name__ == '__main__':
    unittest.main()
