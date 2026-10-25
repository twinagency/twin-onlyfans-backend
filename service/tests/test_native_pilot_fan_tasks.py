"""Concurrency isolation and a slow fan must not stall other conversations."""
import asyncio
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from native_pilot.fan_tasks import FanTasks

class FanTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_fan_does_not_block_and_duplicate_is_not_started(self):
        tasks=FanTasks(2);slow=asyncio.Event();fast=asyncio.Event();seen=[]
        async def turn(fan,event):
            await event.wait();seen.append(fan)
        self.assertTrue(tasks.start(1,lambda:turn(1,slow)))
        self.assertFalse(tasks.start(1,lambda:turn(1,slow)))
        self.assertTrue(tasks.start(2,lambda:turn(2,fast)))
        self.assertFalse(tasks.start(3,lambda:turn(3,fast)))
        fast.set();await asyncio.sleep(0);tasks.reap()
        self.assertEqual(seen,[2]);self.assertIn(1,tasks)
        self.assertTrue(tasks.start(3,lambda:turn(3,fast)))
        slow.set();await tasks.drain()
        self.assertEqual(sorted(seen),[1,2,3]);self.assertEqual(len(tasks),0)

    async def test_account_wide_error_reaches_supervisor_loop(self):
        tasks=FanTasks(2)
        async def fail():raise RuntimeError("account-held")
        tasks.start(1,fail);await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError,"account-held"):tasks.reap()
        self.assertEqual(len(tasks),0)

    async def test_drain_preserves_started_work(self):
        tasks=FanTasks(2);finished=[]
        async def finish():
            await asyncio.sleep(0.01);finished.append(True)
        tasks.start(1,finish);await tasks.drain()
        self.assertEqual(finished,[True])

if __name__=="__main__":unittest.main()
