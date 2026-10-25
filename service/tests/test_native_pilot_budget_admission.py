"""Queue account budget checks without holding model execution or other accounts."""
import asyncio
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_pilot.budget import DailyBudget

class BudgetAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def budget(self, lock, reserve):
        budget = DailyBudget.__new__(DailyBudget)
        budget._admission_lock = lock
        budget._reserve = reserve
        return budget

    async def test_shared_account_queue_and_independent_account(self):
        entered, release, other_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        async def reserve(*, fan_id, **kw):
            calls.append(fan_id)
            if fan_id == 1:
                entered.set()
                await release.wait()
            return fan_id
        async def other_reserve(**kw):
            other_done.set()
            return 3
        lock = asyncio.Lock()
        first, second = [self.budget(lock, reserve) for _ in range(2)]
        other = self.budget(asyncio.Lock(), other_reserve)
        kwargs = {"request_bytes": 100, "max_output_tokens": 100}
        a = asyncio.create_task(first.reserve(fan_id=1, **kwargs))
        await asyncio.wait_for(entered.wait(), 1)
        b = asyncio.create_task(second.reserve(fan_id=2, **kwargs))
        c = asyncio.create_task(other.reserve(fan_id=3, **kwargs))
        await asyncio.wait_for(other_done.wait(), 1)
        self.assertEqual(calls, [1])
        release.set()
        self.assertEqual(await asyncio.wait_for(asyncio.gather(a, b, c), 1), [1, 2, 3])
        self.assertFalse(lock.locked())

    async def test_cancelled_queued_request_never_enters_reservation(self):
        lock, called = asyncio.Lock(), []
        async def reserve(**kw):
            called.append(True)
        budget = self.budget(lock, reserve)
        await lock.acquire()
        pending = asyncio.create_task(budget.reserve(fan_id=1, request_bytes=100, max_output_tokens=100))
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        lock.release()
        self.assertEqual(called, [])
        await budget.reserve(fan_id=1, request_bytes=100, max_output_tokens=100)
        self.assertEqual(called, [True])

    async def test_default_path_does_not_add_a_lock(self):
        called = []
        async def reserve(**kw):
            called.append(kw["fan_id"])
            return 7
        budget = self.budget(None, reserve)
        self.assertEqual(await budget.reserve(fan_id=7, request_bytes=100, max_output_tokens=100), 7)
        self.assertEqual(called, [7])

if __name__ == "__main__":
    unittest.main()
