"""Regressions: denied provider slots must not consume archive queue turns."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
import threading
import unittest
from contextlib import nullcontext

from onlyfans_runtime.launch_history_memory.recovery import HistoryRecovery, archive_equivalent
from onlyfans_runtime.store import Event
from onlyfans_runtime.native_worker import NativeWorker
from onlyfans_runtime.provider_read_budget import ReadDeferred


class HistoryCapacityTests(unittest.TestCase):
    def test_archive_accepts_html_equivalence_but_preserves_content_and_timestamp_checks(self):
        event=Event(7,99,'2026-10-31T18:44:12+00:00','in','hello',(),0).normalized()
        message=SimpleNamespace(account_id='account',fan_id=7,message_id=99,direction='in',price_cents=0,
            media_ids='[]',body='<p>hello</p>',created_at=datetime(2026,10,31,18,44,12))
        self.assertTrue(archive_equivalent(Mock(),message,event))
        message.body='<p>different</p>'
        self.assertFalse(archive_equivalent(Mock(),message,event))
        message.body='<p>hello</p>';message.created_at+=timedelta(seconds=1)
        self.assertFalse(archive_equivalent(Mock(),message,event))
        message.created_at-=timedelta(seconds=1);message.price_cents=100
        self.assertFalse(archive_equivalent(Mock(),message,event))

    def setUp(self):
        # These scheduling unit tests mock claims, not a database-backed worker.
        # Telemetry has separate PostgreSQL coverage and grants no read capacity.
        metrics = patch('onlyfans_runtime.history_telemetry.measure', return_value=nullcontext())
        metrics.start()
        self.addCleanup(metrics.stop)
        self.recovery = HistoryRecovery(SimpleNamespace())
        self.reader = SimpleNamespace(account_id='account', messages=Mock(), chats=Mock())
        for name in ['claim_scan', 'claim', 'fail', 'page', 'scan_page']:
            setattr(self.recovery, name, Mock())
        self.recovery.claim_scan.return_value = None
        self.recovery.claim.return_value = SimpleNamespace(fan_id=7, before_id=99, page_limit=20)
        self.reader.messages.return_value = SimpleNamespace(events=[], has_more=False, observations=[])
        self.recovery.page.return_value = 'complete'

    def run_step(self, acquire):
        with patch('onlyfans_runtime.worker._provider_claim', acquire), patch('onlyfans_runtime.worker._release_provider') as release:
            result = self.recovery.step('tenant', 'account', self.reader, connection_revision=3)
            return result, release

    def test_busy_capacity_does_not_take_or_fail_any_archive_claim(self):
        result, _ = self.run_step(Mock(side_effect=ReadDeferred(datetime.now()+timedelta(seconds=2))))
        self.assertEqual(result, 0)
        self.recovery.claim_scan.assert_not_called()
        self.recovery.claim.assert_not_called()
        self.recovery.fail.assert_not_called()
        self.reader.messages.assert_not_called()

    def test_available_capacity_reads_saved_boundary_with_small_page(self):
        acquire=Mock(return_value='fenced-provider-claim')
        result, release = self.run_step(acquire)
        self.assertEqual(result, 1)
        self.reader.messages.assert_called_once_with(fan_id=7, before_id=99, limit=20)
        self.assertEqual(self.recovery.page.call_args.kwargs['provider_read_claim'], 'fenced-provider-claim')
        self.assertEqual(release.call_count, 2)

    def test_empty_queue_releases_both_provider_slots(self):
        self.recovery.claim.return_value=None
        result, release=self.run_step(Mock(return_value='lease'))
        self.assertEqual(result, 0)
        self.assertEqual(release.call_count, 2)
        self.reader.messages.assert_not_called()

    def test_read_failure_persists_retry_and_releases_capacity(self):
        self.reader.messages.side_effect=TimeoutError('synthetic')
        with patch('onlyfans_runtime.worker._provider_claim', return_value='lease'), patch('onlyfans_runtime.worker._release_provider') as release:
            with self.assertRaises(TimeoutError):
                self.recovery.step('tenant','account',self.reader,connection_revision=3)
        self.recovery.fail.assert_called_once()
        self.recovery.page.assert_not_called()
        self.assertEqual(release.call_count,2)

    def test_discovery_failure_does_not_skip_known_fan(self):
        self.recovery.claim_scan.return_value=SimpleNamespace(cursor='10')
        self.reader.chats.side_effect=TimeoutError('synthetic')
        with self.assertRaises(TimeoutError):self.run_step(Mock(return_value='lease'))
        self.reader.messages.assert_called_once()
        self.recovery.page.assert_called_once()

    def test_paused_account_history_runs_even_when_recent_read_defers(self):
        worker=object.__new__(NativeWorker)
        worker.store=SimpleNamespace()
        worker.tenant_id='tenant'
        worker.accounts=[SimpleNamespace(account_id='account',read_connection_revision=3)]
        worker.accounts_per_cycle=1
        worker._cursor=0
        worker._lock=threading.Lock()
        worker._role=Mock()
        worker._heartbeat=Mock()
        order=[]
        worker._history_turn=Mock(side_effect=lambda *a,**kw:order.append('history'))
        worker._read_progress=Mock(side_effect=lambda *a,**kw:order.append('recent'))
        worker._deliver=Mock()
        with patch('onlyfans_runtime.delivery_resolution_jobs.poll'):
            report=worker.step()
        self.assertEqual(order,['history','recent'])
        self.assertEqual(report.held,1)
        worker._deliver.assert_not_called()

    def test_active_account_history_runs_before_a_deferred_read(self):
        worker=object.__new__(NativeWorker)
        worker.store=SimpleNamespace()
        worker.tenant_id='tenant'
        worker.accounts=[SimpleNamespace(account_id='account',read_connection_revision=None,reader=object())]
        worker.accounts_per_cycle=1
        worker._cursor=0
        worker._lock=threading.Lock()
        worker._role=Mock();worker._heartbeat=Mock()
        worker.authority=SimpleNamespace(expire_dispatches=Mock(return_value=0),repair=Mock(return_value=0))
        worker.facts_reader=None
        order=[]
        worker._history_turn=Mock(side_effect=lambda *a,**kw:order.append('history'))
        worker._read_progress=Mock(side_effect=lambda *a,**kw:order.append('recent'))
        worker._deliver=Mock()
        with patch('onlyfans_runtime.delivery_resolution_jobs.poll'):
            report=worker.step()
        self.assertEqual(order,['history','recent'])
        self.assertEqual(report.held,1)
        worker._deliver.assert_not_called()


if __name__=='__main__':unittest.main()
