"""Private bounded delivery Sessions, cancellation drain and pacing rechecks."""
import asyncio
from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_pilot.delivery_pool import DeliveryClients, configured_delivery_concurrency
from native_pilot.ledger import Refused
from native_pilot.runner import Gateway
from native_pilot.service import ServiceGateway

def client():
    http = SimpleNamespace(closed=False)
    def close():
        http.closed = True
    http.close = close
    return SimpleNamespace(http=http, account_id="100", platform="onlyfans")

def gateway(pool, fan):
    g = ServiceGateway.__new__(ServiceGateway)
    g.parallel_admission = True
    g._fan_send_lock = asyncio.Lock()
    g.service = SimpleNamespace(delivery_clients=lambda: pool)
    g.scope = SimpleNamespace(provider_id="100")
    g.pilot = SimpleNamespace(fan_id=fan)
    g.client, g.reader = object(), object()
    g.cancelled, g.stopped_reason = False, None
    g.stopped = lambda: False
    g.run_blocking = asyncio.to_thread
    return g

class DeliveryPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_separate_fans_overlap_with_private_sessions_and_same_fan_stays_sequential(self):
        pool = DeliveryClients(2, client)
        a, b = gateway(pool, 1), gateway(pool, 2)
        original = (a.client, a.reader)
        active, peak, sessions = {}, {}, set()
        entered, release = asyncio.Event(), asyncio.Event()
        async def wire(g, text, terms=None):
            fid = g.pilot.fan_id
            active[fid] = active.get(fid, 0) + 1
            peak[fid] = max(peak.get(fid, 0), active[fid])
            sessions.add(id(g.client.http))
            if sum(active.values()) == 2:
                entered.set()
            await release.wait()
            active[fid] -= 1
            return text
        with patch.object(Gateway, "_send", wire):
            jobs = [asyncio.create_task(g._send(text)) for g, text in [(a,"one"),(a,"two"),(b,"other")]]
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(pool.active, 2)
            self.assertEqual(len(sessions), 2)
            self.assertEqual(peak, {1: 1, 2: 1})
            with self.assertRaisesRegex(RuntimeError, "drain"):
                pool.close()
            release.set()
            self.assertEqual(await asyncio.gather(*jobs), ["one", "two", "other"])
        self.assertEqual((a.client, a.reader), original)
        self.assertEqual(pool.active, 0)
        objects = list(pool._all)
        pool.close()
        self.assertTrue(all(c.http.closed for c in objects))

    async def test_cancellation_keeps_session_borrowed_until_outcome_finishes(self):
        pool, entered, finish = DeliveryClients(1, client), asyncio.Event(), asyncio.Event()
        g = gateway(pool, 1)
        async def wire(*args):
            entered.set()
            await finish.wait()
            return "confirmed"
        with patch.object(Gateway, "_send", wire):
            job = asyncio.create_task(g._send("test"))
            await entered.wait()
            job.cancel()
            await asyncio.sleep(0)
            self.assertEqual(pool.active, 1)
            self.assertFalse(job.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await job
        self.assertTrue(g.cancelled)
        self.assertEqual(pool.active, 0)
        pool.close()

    async def test_cancelled_waiter_never_starts_delivery(self):
        pool, calls = DeliveryClients(1, client), []
        g = gateway(pool, 1)
        async def wire(*args):
            calls.append(True)
        with patch.object(Gateway, "_send", wire):
            async with pool.borrow():
                job = asyncio.create_task(g._send("test"))
                await asyncio.sleep(0)
                job.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await job
                self.assertEqual(calls, [])
        pool.close()

    async def test_connection_replacement_drains_then_closes_lane_sessions(self):
        import argparse
        from datetime import timezone
        from native_pilot.service import Service
        events, entered, release = [], asyncio.Event(), asyncio.Event()
        def fresh(label):
            value = client()
            value.me = lambda: {"id":100, "username":"creator"}
            original_close = value.http.close
            def close():
                events.append(label + "-closed")
                original_close()
            value.http.close = close
            return value
        with patch.dict(os.environ, {"FASTT_MAX_CONCURRENT_FANS":"2", "FASTT_MAX_CONCURRENT_SENDS":"2"}):
            service = Service(argparse.Namespace(account="of-100", fan=None, all_fans=True, handle="creator"))
            old, new = fresh("old-root"), fresh("new-root")
            expiry = datetime(2099,1,1,tzinfo=timezone.utc)
            service.client = old
            service.conn = SimpleNamespace(revision=1, expires_at=expiry)
            replacement = SimpleNamespace(revision=2, expires_at=expiry)
            service._delivery_clients = DeliveryClients(2, lambda: fresh("lane"))
            lane_pool = service._delivery_clients
            async def loan():
                async with lane_pool.borrow():
                    entered.set()
                    await release.wait()
                    events.append("loan-finished")
            service.fan_tasks.start(1, loan)
            await entered.wait()
            async def load(scope):
                events.append("load-new")
                return replacement
            async def sentinels():
                return SimpleNamespace()
            async def stop_intake():
                events.append("intake-stopped")
            service.intake = SimpleNamespace(stop=stop_intake)
            service.start_intake = lambda conn: events.append("intake-started")
            with (patch("native_pilot.connection.load_verified_connection", load),
                  patch("native_pilot.connection.build_client", lambda conn: new),
                  patch("native_pilot.attribution.require_sentinels", sentinels)):
                refresh = asyncio.create_task(service.ensure_session())
                await asyncio.sleep(0)
                self.assertFalse(old.http.closed)
                self.assertNotIn("load-new", events)
                release.set()
                await asyncio.wait_for(refresh, 2)
            self.assertLess(events.index("loan-finished"), events.index("lane-closed"))
            self.assertTrue(old.http.closed)
            self.assertIs(service.client, new)
            self.assertIs(service.conn, replacement)
            self.assertIsNone(service._delivery_clients)
            self.assertEqual(lane_pool.active, 0)
            new.http.close()

    async def test_factory_must_not_reuse_mutable_session(self):
        shared = client()
        pool = DeliveryClients(2, lambda: shared)
        async with pool.borrow():
            with self.assertRaisesRegex(RuntimeError, "reused"):
                async with pool.borrow():
                    pass
            self.assertFalse(shared.http.closed)
        pool.close()
        self.assertTrue(shared.http.closed)

    async def test_flag_defaults_and_serial_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configured_delivery_concurrency(), 1)
            os.environ["FASTT_MAX_CONCURRENT_SENDS"] = "16"
            self.assertEqual(configured_delivery_concurrency(), 1)
            os.environ["FASTT_MAX_CONCURRENT_FANS"] = "64"
            self.assertEqual(configured_delivery_concurrency(), 16)
            for bad in ["0", "17", "1.5", "bad"]:
                os.environ["FASTT_MAX_CONCURRENT_SENDS"] = bad
                with self.assertRaises(ValueError):
                    configured_delivery_concurrency()

    def prepare_gateway(self, begin):
        g = Gateway.__new__(Gateway)
        g.cancelled, g.parallel_admission, g.bubble = False, True, 0
        g.pilot = SimpleNamespace(fan_id=1, account_id="of-100", pilot_id="p", owner_token="owner", revision=1)
        g.scope = SimpleNamespace(provider_id="100")
        g.sources, g.input_key = {1: {"created_at":"2026-10-25T00:00:00"}}, "key"
        g.ledger = SimpleNamespace(path=None, receipts=lambda **kw: [], begin_attempt=begin)
        g.content, g.hold_check, g.followup = None, None, False
        g.stopped = lambda: False
        g.run_blocking = asyncio.to_thread
        async def pace():
            pass
        g.pace = pace
        return g

    async def test_throttle_race_refreshes_checks_without_new_bubble_or_post(self):
        bubbles, histories = [], []
        def begin(**kw):
            bubbles.append(kw["bubble_no"])
            if len(bubbles) < 3:
                raise Refused("allowance-throttled")
            return "admitted"
        g = self.prepare_gateway(begin)
        async def history(*args, **kw):
            histories.append(True)
            return []
        g.reader = SimpleNamespace(preflight_history=history)
        with patch("native_pilot.runner.check_provider_history"), patch("native_pilot.runner.check_local"):
            self.assertEqual(await g._prepare_attempt(None), ("admitted", None))
        self.assertEqual(bubbles, [1, 1, 1])
        self.assertEqual(len(histories), 3)
        self.assertEqual(g.bubble, 1)

    async def test_pause_during_admission_retry_prevents_next_attempt(self):
        calls, histories = [], []
        def begin(**kw):
            calls.append(True)
            raise Refused("allowance-throttled")
        g = self.prepare_gateway(begin)
        async def history(*args, **kw):
            histories.append(True)
            if len(histories) == 2:
                g.hold_check = lambda: "fan-paused"
            return []
        g.reader = SimpleNamespace(preflight_history=history)
        with patch("native_pilot.runner.check_provider_history"), patch("native_pilot.runner.check_local"):
            with self.assertRaises(Refused) as caught:
                await g._prepare_attempt(None)
        self.assertEqual(caught.exception.reason, "fan-paused")
        self.assertEqual(len(calls), 1)
        self.assertEqual(g.bubble, 0)

    async def test_account_policy_and_cooldown_rechecked_at_wire(self):
        g = ServiceGateway.__new__(ServiceGateway)
        g.service = SimpleNamespace(stop=asyncio.Event())
        g.pilot = SimpleNamespace(account_id="of-100")
        now = datetime(2026, 10, 25)
        policy = {"enabled": True, "used": 10, "limit": 10, "busy": False,
                  "next_dispatch_at": (now + timedelta(seconds=.25)).isoformat(), "throttle_seconds":.25}
        g.ledger = SimpleNamespace(allowance_status=lambda account: dict(policy))
        with patch("native_pilot.runner.now", return_value=now):
            self.assertIsNone(g._parallel_wire_check())  # last admitted unit remains valid
            policy["next_dispatch_at"] = (now + timedelta(seconds=60)).isoformat()
            self.assertEqual(g._parallel_wire_check(), "account-cooldown-before-post")
            policy["enabled"] = False
            self.assertEqual(g._parallel_wire_check(), "allowance-disabled")
            g.service.stop.set()
            self.assertEqual(g._parallel_wire_check(), "service-stopping")

if __name__ == "__main__":
    unittest.main()
