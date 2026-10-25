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


if __name__ == "__main__":
    unittest.main()
