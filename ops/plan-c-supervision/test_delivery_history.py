"""Complete bounded delivery-history pagination and the durable verified checkpoint.

The provider is a stateful in-memory fake that pages like the real listing (offset/limit,
date_start filter, provider total). Everything else (files, atomic replacement, restart) is real.
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import delivery_history as h
import incident_state as st

URL = 'https://tunnel.example/onlyfans/webhooks/plan-c'
ACCT = 'acct_test'
EVENTS = {'messages.received', 'counter.updated'}
T0 = datetime.datetime(2026, 11, 10, 1, 0, 0, tzinfo=datetime.timezone.utc)
SUBSCRIPTION = {'id': 'wh_1', 'url': URL, 'account_ids': [ACCT], 'account_scope': 'inclusive', 'enabled': True,
                'has_signing_secret': True, 'events': sorted(EVENTS)}


def at(minutes):
    return T0 + datetime.timedelta(minutes=minutes)


def row(i, minutes, ok=True, key=None):
    return {'id': 'd%d' % i, 'idempotency_key': key or 'k%d' % i, 'created_at': h.fmt(at(minutes)),
            'event': 'messages.received', 'succeeded': ok, 'status_code': 202 if ok else 500, 'url': URL,
            'payload': {'account_id': ACCT, 'n': i}}


class Provider:
    """Newest-first paging with provider-reported total, an offset window and optional hooks."""
    def __init__(self, rows, total=True, newest_first=True):
        self.rows, self.total, self.newest_first = list(rows), total, newest_first
        self.calls, self.hook = [], None

    def __call__(self, start, limit, offset):
        self.calls.append((start, limit, offset))
        if self.hook:
            self.hook(len(self.calls), self)
        keep = [r for r in self.rows if h.parse_ts(r['created_at']) >= h.parse_ts(start)]
        keep.sort(key=lambda r: r['created_at'], reverse=self.newest_first)
        page = keep[offset:offset + limit]
        nxt = 'more' if offset + limit < len(keep) else None
        pagination = {'next_page': nxt}
        if self.total:
            pagination['total'] = len(keep)
        return {'data': json.loads(json.dumps(page)), '_pagination': pagination, '_meta': {'_credits': {'used': 0}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.intent = self.root / 'put-intent.json'
        self.intent.write_text('{"intent":1}')
        os.utime(self.intent, (T0.timestamp(), T0.timestamp()))
        self.boundary = self.root / 'delivery-enable-boundary.json'
        self.checkpoint = self.root / 'delivery-checkpoint.json'

    def tearDown(self):
        self.tmp.cleanup()

    def verify(self, provider, now_min=60, **kw):
        args = dict(boundary_path=self.boundary, checkpoint_path=self.checkpoint, intent_path=self.intent,
                    webhook_id='wh_1', url=URL, subscription=SUBSCRIPTION, events=EVENTS, account_id=ACCT,
                    now=at(now_min))
        args.update(kw)
        return h.verify_history(provider, **args)

    def cp(self):
        return json.loads(self.checkpoint.read_bytes())


class PaginationTests(Base):
    def test_230_deliveries_over_three_pages_pass_and_checkpoint_advances(self):
        provider = Provider([row(i, 1 + i * 0.1) for i in range(230)])
        out = self.verify(provider)
        self.assertEqual((out['pages'], out['rows']), (3, 230))
        self.assertEqual([c[2] for c in provider.calls], [0, 100, 200])
        self.assertEqual(self.cp()['accounted_count'], 230)
        self.assertEqual(self.cp()['cursor_utc'], h.fmt(at(1 + 229 * 0.1)))

    def test_next_check_reads_only_the_overlap_window_not_history(self):
        provider = Provider([row(i, 1 + i) for i in range(230)])
        self.verify(provider, now_min=300)
        provider.calls.clear()
        provider.rows += [row(1000 + i, 250 + i) for i in range(5)]
        out = self.verify(provider, now_min=301)
        self.assertEqual(out['pages'], 1)
        self.assertEqual(provider.calls[0][0], h.fmt(at(230) - h.OVERLAP))
        self.assertEqual(self.cp()['accounted_count'], 235)

    def test_page_bound_fails_closed_without_advancing(self):
        provider = Provider([row(i, 1 + i * 0.01) for i in range(230)])
        with self.assertRaisesRegex(h.HistoryError, 'page_bound'):
            self.verify(provider, max_pages=2)
        self.assertFalse(self.checkpoint.exists())

    def test_duplicates_across_pages_fold_and_conflicting_duplicate_fails(self):
        provider = Provider([row(i, 1 + i * 0.1) for i in range(150)], total=False)
        raw_call = provider.__call__
        def dup(start, limit, offset):
            page = raw_call(start, limit, offset)
            if offset == 100:   # page 2 repeats the final row of page 1
                page['data'].insert(0, provider.rows_for_dup)
            return page
        provider.rows_for_dup = None
        first = raw_call('2000-01-01T00:00:00Z', 100, 0)['data'][-1]
        provider.rows_for_dup = first
        out = self.verify(dup)
        self.assertEqual(out['rows'], 150)
        self.assertEqual(out['duplicates_folded'], 1)
        provider.rows_for_dup = {**first, 'status_code': 500, 'succeeded': False}   # same identity, different bytes
        self.checkpoint.unlink()
        with self.assertRaisesRegex(h.HistoryError, 'conflicting_delivery_evidence'):
            self.verify(dup)

    def test_pages_shifting_under_the_reader_are_rescanned_not_skipped(self):
        provider = Provider([row(i, 1 + i * 0.1) for i in range(150)])
        def insert_once(n, p):
            if n == 2:   # a new newest delivery lands between page 1 and page 2: offsets shift
                p.rows.append(row(900, 50))
                p.hook = None
        provider.hook = insert_once
        out = self.verify(provider)
        self.assertEqual(out['rows'], 151)       # nothing skipped: the late arrival is included
        self.assertGreater(len(provider.calls), 2)  # a second full scan ran

    def test_without_provider_total_page_zero_reread_detects_head_insertion(self):
        provider = Provider([row(i, 1 + i * 0.1) for i in range(150)], total=False)
        def insert_once(n, p):
            if n == 2:
                p.rows.append(row(901, 50)); p.hook = None
        provider.hook = insert_once
        self.assertEqual(self.verify(provider)['rows'], 151)

    def test_never_stable_listing_fails_closed(self):
        provider = Provider([row(i, 1 + i * 0.1) for i in range(150)])
        counter = [1000]
        def always_insert(n, p):
            counter[0] += 1
            p.rows.append(row(counter[0], 50))
        provider.hook = always_insert
        with self.assertRaisesRegex(h.HistoryError, 'delivery_pages_unstable'):
            self.verify(provider)
        self.assertFalse(self.checkpoint.exists())

    def test_malformed_pagination_and_cost_fail_closed(self):
        good = Provider([row(1, 1)])
        for mutate in (lambda o: o.pop('_pagination'), lambda o: o['_meta']['_credits'].update(used=1),
                       lambda o: o.update(data={}), lambda o: o['_pagination'].pop('next_page')):
            def bad(start, limit, offset, mutate=mutate):
                o = good(start, limit, offset); mutate(o); return o
            with self.assertRaises(h.HistoryError):
                self.verify(bad)
        self.assertFalse(self.checkpoint.exists())


class AccountingTests(Base):
    def test_old_unresolved_failure_stays_held_and_cursor_never_passes_it(self):
        provider = Provider([row(1, 2, ok=False), row(2, 3), row(3, 4)])
        with self.assertRaisesRegex(h.HistoryError, 'unresolved_failed_delivery'):
            self.verify(provider)
        self.assertFalse(self.checkpoint.exists())
        provider.rows += [row(4 + i, 30 + i) for i in range(10)]   # later healthy traffic does not launder it
        with self.assertRaisesRegex(h.HistoryError, 'unresolved_failed_delivery'):
            self.verify(provider, now_min=90)

    def test_failure_backfilled_older_than_the_cursor_is_not_ignorable(self):
        provider = Provider([row(i, 1 + i) for i in range(5)])
        self.verify(provider)
        self.assertEqual(self.cp()['cursor_utc'], h.fmt(at(5)))
        provider.rows.append(row(77, 2, ok=False))   # older than the cursor, inside the overlap
        with self.assertRaisesRegex(h.HistoryError, 'unresolved_failed_delivery'):
            self.verify(provider, now_min=61)
        self.assertEqual(self.cp()['seq'], 1)         # checkpoint did not advance

    def test_late_old_success_is_accounted_and_ties_have_stable_order(self):
        rows = [row(i, 10) for i in range(5)]          # five deliveries share one timestamp
        provider = Provider(rows)
        self.verify(provider)
        first = self.cp()['recent']
        provider.rows.append(row(50, 3))               # late success older than the cursor
        provider.rows.reverse()
        self.verify(provider, now_min=61)
        self.assertEqual([r[1] for r in first], sorted(r[1] for r in first))
        self.assertEqual(self.cp()['cursor_utc'], h.fmt(at(10)))
        self.assertEqual(self.cp()['accounted_count'], 6)

    def test_failure_with_later_verified_retry_is_resolved_and_does_not_poison_future_checks(self):
        provider = Provider([row(1, 2, ok=False, key='retry-me'), row(2, 3, key='retry-me'), row(3, 4)])
        self.verify(provider)
        for n in range(3):                              # failed attempt keeps appearing in every overlap window
            provider.rows.append(row(10 + n, 5 + n))
            self.verify(provider, now_min=61 + n)
        self.assertEqual(self.cp()['seq'], 4)

    def test_failed_retry_that_never_succeeds_is_not_resolved_by_another_keys_success(self):
        provider = Provider([row(1, 2, ok=False, key='a'), row(2, 3, key='b')])
        with self.assertRaises(h.HistoryError):
            self.verify(provider)

    def test_failure_inside_an_audited_resolved_interval_is_accounted_outside_is_not(self):
        provider = Provider([row(1, 5, ok=False), row(2, 6)])
        interval = [(h.fmt(at(4)), h.fmt(at(7)))]
        self.verify(provider, resolved_intervals=interval)
        provider2 = Provider([row(1, 20, ok=False), row(2, 21)])
        self.checkpoint.unlink()
        with self.assertRaises(h.HistoryError):
            self.verify(provider2, resolved_intervals=interval)

    def test_new_failure_prevents_permission_and_leaves_checkpoint_untouched(self):
        provider = Provider([row(i, 1 + i) for i in range(5)])
        self.verify(provider)
        before = self.checkpoint.read_bytes()
        provider.rows.append(row(60, 8, ok=False))
        with self.assertRaisesRegex(h.HistoryError, 'unresolved_failed_delivery'):
            self.verify(provider, now_min=61)
        self.assertEqual(self.checkpoint.read_bytes(), before)

    def test_wrong_url_event_or_account_is_a_failure(self):
        for field, value in (('url', 'https://other.example/x'), ('event', 'other.event')):
            bad = {**row(1, 2), field: value}
            with self.assertRaises(h.HistoryError):
                self.verify(Provider([bad, row(2, 3)]))
            self.assertFalse(self.checkpoint.exists())
        bad = row(1, 2); bad['payload']['account_id'] = 'acct_other'
        with self.assertRaises(h.HistoryError):
            self.verify(Provider([bad, row(2, 3)]))

    def test_provider_dropping_an_accounted_delivery_is_conflicting_evidence(self):
        provider = Provider([row(i, 1 + i) for i in range(5)])
        self.verify(provider)
        provider.rows = [r for r in provider.rows if r['id'] != 'd4']
        with self.assertRaisesRegex(h.HistoryError, 'accounted_delivery_missing'):
            self.verify(provider, now_min=61)

    def test_empty_listing_is_refused_before_first_evidence_and_after_accounted_ones(self):
        with self.assertRaisesRegex(h.HistoryError, 'no_delivery_evidence'):
            self.verify(Provider([]))
        provider = Provider([row(1, 2)])
        self.verify(provider)
        with self.assertRaisesRegex(h.HistoryError, 'accounted_delivery_missing'):
            self.verify(Provider([]), now_min=61)   # the cursor delivery vanished: not a quiet period


class DurableStateTests(Base):
    def test_boundary_is_recorded_once_and_survives_a_changed_mtime(self):
        self.verify(Provider([row(1, 2)]))
        recorded = json.loads(self.boundary.read_bytes())
        self.assertEqual(recorded['boundary_utc'], h.fmt(T0))
        os.utime(self.intent, (T0.timestamp() + 9999, T0.timestamp() + 9999))   # mutable mtime
        provider = Provider([row(1, 2), row(2, 3)])
        self.verify(provider, now_min=61)
        self.assertEqual(json.loads(self.boundary.read_bytes()), recorded)

    def test_changed_intent_content_or_url_rejects_recorded_boundary(self):
        self.verify(Provider([row(1, 2)]))
        with self.assertRaisesRegex(h.HistoryError, 'enable_boundary_identity_changed'):
            self.verify(Provider([row(1, 2)]), url='https://new-tunnel.example/onlyfans/webhooks/plan-c')
        self.intent.write_text('{"intent":2}')
        with self.assertRaisesRegex(h.HistoryError, 'enable_intent_changed'):
            self.verify(Provider([row(1, 2)]))

    def test_changed_config_version_rejects_stale_checkpoint(self):
        self.verify(Provider([row(1, 2)]))
        changed = {**SUBSCRIPTION, 'events': ['messages.received']}
        with self.assertRaisesRegex(h.HistoryError, 'checkpoint_identity_changed:config_version'):
            self.verify(Provider([row(1, 2)]), subscription=changed)

    def test_paused_or_disabled_subscription_changes_config_version(self):
        base = h.config_version(SUBSCRIPTION)
        self.assertNotEqual(base, h.config_version({**SUBSCRIPTION, 'paused': True}))
        self.assertNotEqual(base, h.config_version({**SUBSCRIPTION, 'enabled': False}))
        self.assertEqual(base, h.config_version({**SUBSCRIPTION, 'events': list(reversed(SUBSCRIPTION['events']))}))

    def test_tampered_or_corrupt_checkpoint_fails_closed(self):
        self.verify(Provider([row(1, 2)]))
        body = json.loads(self.checkpoint.read_bytes())
        body['cursor_utc'] = h.fmt(at(1000))               # skip ahead past unaccounted evidence
        self.checkpoint.write_bytes(json.dumps(body).encode())
        with self.assertRaisesRegex(h.HistoryError, 'durable_delivery_state_corrupt'):
            self.verify(Provider([row(1, 2)]), now_min=61)
        self.checkpoint.write_bytes(b'{not json')
        with self.assertRaisesRegex(h.HistoryError, 'durable_delivery_state_corrupt'):
            self.verify(Provider([row(1, 2)]), now_min=61)

    def test_crash_during_atomic_replacement_leaves_previous_verified_checkpoint(self):
        self.verify(Provider([row(1, 2)]))
        before = self.checkpoint.read_bytes()
        provider = Provider([row(1, 2), row(2, 3)])
        with patch.object(st.os, 'replace', side_effect=OSError('crash before rename')):
            with self.assertRaises(OSError):
                self.verify(provider, now_min=61)
        self.assertEqual(self.checkpoint.read_bytes(), before)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith('.tmp')], [])
        self.verify(provider, now_min=62)                    # restart: replays the same window, then advances
        self.assertEqual(self.cp()['seq'], 2)

    def test_process_restart_resumes_from_the_durable_checkpoint(self):
        rows_file = self.root / 'rows.json'
        rows_file.write_text(json.dumps([row(i, 1 + i) for i in range(230)]))
        script = '''
import json,sys,datetime
from pathlib import Path
import delivery_history as h
root=Path(sys.argv[1]);rows=json.loads((root/'rows.json').read_text())
sub=json.loads(sys.argv[2]);calls=[]
def page(start,limit,offset):
    calls.append(offset)
    keep=sorted([r for r in rows if h.parse_ts(r['created_at'])>=h.parse_ts(start)],key=lambda r:r['created_at'],reverse=True)
    return {'data':keep[offset:offset+limit],'_pagination':{'next_page':'m' if offset+limit<len(keep) else None,'total':len(keep)},'_meta':{'_credits':{'used':0}}}
out=h.verify_history(page,boundary_path=root/'b.json',checkpoint_path=root/'c.json',intent_path=root/'i.json',webhook_id='wh_1',
  url=sys.argv[3],subscription=sub,events={'messages.received'},account_id='acct_test',now=datetime.datetime(2026,11,10,9,tzinfo=datetime.timezone.utc))
print(json.dumps({'pages':out['pages'],'seq':out['seq'],'calls':calls}))
'''
        (self.root / 'i.json').write_text('{}')
        os.utime(self.root / 'i.json', (T0.timestamp(), T0.timestamp()))
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parent), 'PYTHONDONTWRITEBYTECODE': '1'}
        def run():
            out = subprocess.run([sys.executable, '-c', script, str(self.root), json.dumps(SUBSCRIPTION), URL],
                                 capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            return json.loads(out.stdout)
        first, second = run(), run()
        self.assertEqual((first['seq'], first['pages']), (1, 3))
        self.assertEqual((second['seq'], second['pages']), (2, 1))   # a new process starts at the checkpoint


class CollectorPagesTests(unittest.TestCase):
    def test_collect_pages_retains_unique_pages_across_rescans_and_merges_complete_listing(self):
        provider = Provider([row(i, 1 + i * 0.1) for i in range(150)])
        provider.hook = lambda n, p: (p.rows.append(row(800, 50)), setattr(p, 'hook', None)) if n == 2 else None
        names = []
        def get_json(start, limit, offset, name):
            names.append(name)
            return provider(start, limit, offset)
        merged, window = h.collect_pages(get_json, start='2000-01-01T00:00:00Z', prefix='deliveries-page')
        self.assertEqual(len(merged['data']), 151)
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(merged['_pagination']['complete'])
        self.assertIsNone(merged['_pagination']['next_page'])
        self.assertEqual(merged['_pagination']['pages'], window['pages'])


if __name__ == '__main__':
    unittest.main()
