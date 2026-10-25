"""Native synchronous send callbacks must not exhaust the workers they await."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_pilot.runner import Gateway
from native_pilot.send_io import run_blocking
from twin_bridge.platform import OnlyFansTextAdapter


class SendWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_saturated_callback_pool_can_read_history_and_finish_sends(self):
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
        client = SimpleNamespace(account_id="100", platform="onlyfans",
                                 get_messages=lambda *a, **k: {"list": [], "hasMore": False})
        reader = OnlyFansTextAdapter("100", client=client, blocking_call=run_blocking)
        gateway = Gateway.__new__(Gateway)
        gateway.loop = asyncio.get_running_loop()
        gateway.pilot = SimpleNamespace(fan_id=1)
        gateway.content = None
        gate = asyncio.Event()
        entered = []
        async def send(text, terms):
            entered.append(asyncio.current_task())
            if len(entered) == 2:
                gate.set()
            await gate.wait()  # both default workers now wait in the real Gateway.send callback
            self.assertEqual(await reader.preflight_history(1), [])
            return await run_blocking(lambda: text)
        gateway._send = send
        sends = [asyncio.create_task(asyncio.to_thread(gateway.send, 1, str(i), {})) for i in range(2)]
        try:
            results = await asyncio.wait_for(asyncio.gather(*sends), timeout=5)
            self.assertEqual(results, ["0", "1"])
        finally:
            for task in entered:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*entered, return_exceptions=True)
            await asyncio.gather(*sends, return_exceptions=True)

    async def test_account_send_admission_is_serialized_but_accounts_are_independent(self):
        from native_pilot.service import ServiceGateway
        active, peak, sent = {}, {}, []
        fleet_peak = 0
        async def wire(gateway, text, terms=None):
            nonlocal fleet_peak
            account = gateway.service.account
            active[account] = active.get(account, 0) + 1
            peak[account] = max(peak.get(account, 0), active[account])
            fleet_peak = max(fleet_peak, sum(active.values()))
            await asyncio.sleep(0.01)
            sent.append(text)
            active[account] -= 1
            return text
        services = [SimpleNamespace(account=a, fan_tasks=SimpleNamespace(limit=2), dispatch_lock=asyncio.Lock())
                    for a in ["creator-a", "creator-b"]]
        gateways = []
        for service in services:
            for _ in range(2):
                gateway = ServiceGateway.__new__(ServiceGateway)
                gateway.service = service
                gateways.append(gateway)
        with patch.object(Gateway, "_send", wire):
            result = await asyncio.gather(*(g._send(str(i)) for i, g in enumerate(gateways)))
        self.assertEqual(result, ["0", "1", "2", "3"])
        self.assertEqual(peak, {"creator-a": 1, "creator-b": 1})
        self.assertEqual(fleet_peak, 2)
        self.assertEqual(sorted(sent), ["0", "1", "2", "3"])

    async def test_serial_fallback_keeps_original_worker_path(self):
        pilot = SimpleNamespace(account_id="of-100", fan_id=1, deadline="2099-01-01T00:00:00")
        for value, expected in [("1", asyncio.to_thread), ("2", run_blocking)]:
            with self.subTest(concurrency=value), patch.dict(os.environ, {"FASTT_MAX_CONCURRENT_FANS": value}):
                gateway = Gateway(None, pilot, object(), asyncio.get_running_loop())
                self.assertIs(gateway.run_blocking, expected)
                self.assertIs(gateway.reader._run_blocking, expected)

    async def test_manual_pause_still_refuses_before_send(self):
        from native_pilot.service import ServiceGateway
        from native_pilot.ledger import Refused
        gateway = ServiceGateway.__new__(ServiceGateway)
        gateway.run_blocking = run_blocking
        gateway.stopped = lambda: False
        gateway.hold_check = lambda: "fan-paused"
        with self.assertRaises(Refused) as caught:
            await gateway.pace()
        self.assertEqual(caught.exception.reason, "fan-paused")

    async def test_io_workers_preserve_task_scope(self):
        account = ContextVar("test_account")
        async def scoped(value):
            token = account.set(value)
            try:
                return await run_blocking(account.get)
            finally:
                account.reset(token)
        self.assertEqual(await asyncio.gather(scoped("creator-a"), scoped("creator-b")),
                         ["creator-a", "creator-b"])

    async def test_io_failure_propagates_without_retry(self):
        attempts = []
        def fail():
            attempts.append(1)
            raise RuntimeError("synthetic send failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic send failure"):
            await run_blocking(fail)
        self.assertEqual(attempts, [1])


if __name__ == "__main__":
    unittest.main()
