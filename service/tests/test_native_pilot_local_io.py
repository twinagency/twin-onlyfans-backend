"""Bookkeeping cannot stall fan progress or outlive cancellation cleanup."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_pilot.local_io import run_blocking


class LocalIoTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_wait_does_not_block_event_loop(self):
        entered, release = threading.Event(), threading.Event()
        def database():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test callback was not released")
            return "committed"
        task = asyncio.create_task(run_blocking(database))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0)
            self.assertFalse(task.done())  # loop progressed while database is blocked
            release.set()
            self.assertEqual(await task, "committed")
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_default_native_callback_pool_cannot_starve_database_pool(self):
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        async def finish():
            return await run_blocking(lambda: "receipt")
        def native_sender():
            return asyncio.run_coroutine_threadsafe(finish(), loop).result(timeout=3)
        self.assertEqual(await asyncio.wait_for(asyncio.to_thread(native_sender), 4), "receipt")

    async def test_cancellation_drains_started_database_write(self):
        entered, release, committed = threading.Event(), threading.Event(), threading.Event()
        def write():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test callback was not released")
            committed.set()
        task = asyncio.create_task(run_blocking(write))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # repeated cancellation must not release ownership early
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertFalse(committed.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(committed.is_set())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_account_context_and_failure_are_preserved(self):
        account = ContextVar("local_io_account")
        async def scoped(value):
            token = account.set(value)
            try:
                return await run_blocking(account.get)
            finally:
                account.reset(token)
        self.assertEqual(await asyncio.gather(scoped("creator-a"), scoped("creator-b")),
                         ["creator-a", "creator-b"])
        def refused():
            raise ValueError("ownership-refused")
        with self.assertRaisesRegex(ValueError, "ownership-refused"):
            await run_blocking(refused)

    async def test_pool_has_a_fixed_bound(self):
        release = threading.Event()
        lock = threading.Lock()
        entered = []
        def database(index):
            with lock:
                entered.append(index)
            if not release.wait(5):
                raise TimeoutError("test callback was not released")
            return index
        tasks = [asyncio.create_task(run_blocking(database, index)) for index in range(5)]
        try:
            async with asyncio.timeout(2):
                while len(entered) < 4:
                    await asyncio.sleep(0)
            await asyncio.sleep(0.02)
            self.assertEqual(len(entered), 4)
            release.set()
            self.assertEqual(await asyncio.gather(*tasks), list(range(5)))
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
