"""The target schedule must offer exactly the configured rate, with no end-boundary extra."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(sys.platform != "win32", "the load harness uses Linux resource measurements")
class TargetScheduleTests(unittest.TestCase):
    def test_full_target_arrivals_and_deadline_boundary(self):
        from native_pilot.tools.load_test import Cfg, World, make_schedule
        cfg = Cfg(name="target_100k", accounts=10, fans=500, sim_minutes=5, scale=1,
                  rate_per_min=100000 / 60 / 10, single_messages=True, deterministic_arrivals=True)
        world = World(cfg)
        events = make_schedule(world)
        self.assertEqual(len(events), 7500)
        self.assertEqual(min(t for t, _, _ in events), 30)
        self.assertLess(max(t for t, _, _ in events), 300)
        for account in world.accts:
            self.assertEqual(sum(a == account for _, a, _ in events), 750)

    def test_creator_offsets_partition_the_fleet(self):
        from native_pilot.tools.load_test import Cfg, World, make_schedule
        accounts, total = set(), 0
        for offset in range(10):
            cfg = Cfg(name="target_10k", accounts=1, account_offset=offset, fans=500,
                      sim_minutes=5, scale=1, rate_per_min=10000 / 60,
                      single_messages=True, deterministic_arrivals=True)
            world = World(cfg)
            events = make_schedule(world)
            self.assertEqual(len(events), 750)
            self.assertEqual({a for _, a, _ in events}, set(world.accts))
            self.assertFalse(accounts.intersection(world.accts))
            accounts.update(world.accts)
            total += len(events)
        self.assertEqual(total, 7500)
        self.assertEqual(len(accounts), 10)


if __name__ == "__main__":
    unittest.main()
