"""Reject incomplete workload or worker failures in fleet aggregation."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_pilot.tools.fleet_load import combine_workers

def workers():
    rows = []
    for offset in range(10):
        account = f"of-{900000001 + offset}"
        rows.append((offset, {
            "scenario": "target_10k",
            "config": {"accounts": 1, "account_offset": offset, "scale": 1,  "sim_minutes": 5, "send_concurrency": 1, "provider_latency_s": 0.2, "history_latency_s": 0.0},
            "accounts": {account: {
                "inbound_messages": 750, "reply_latency_s": {"n": 750, "max": 20},
                "unanswered_at_end": 0, "unanswered_over_30s": 0,
                "delivered_over_30s": 0, "throttle_respected": True}},
            "offered_load": {"scheduled_messages": 750, "generated_messages": 750,
                             "persisted_messages": 750, "scheduled_messages_per_hour": 10000,
                             "max_ingress_delay_s": 0.1},
            "delivery_concurrency": 1, "runtime_errors": [],
            "worker_database_role": "worker", "async_pool": {"size": 4, "max_overflow": 0},
            "service_crashed": [], "helper_errors": [], "start_epoch": 1000 + offset * 0.001,
            "totals": {"replies": 750}}))
    return rows

class FleetEvidenceTests(unittest.TestCase):
    def test_complete_synchronized_fleet_passes(self):
        report = combine_workers(workers(), [], minutes=5)
        self.assertTrue(report["capacity_verdict"]["passed"], report["capacity_verdict"])
        self.assertEqual(report["totals"]["replies"], 7500)

    def test_missing_duplicate_or_miscoped_worker_fails(self):
        rows = workers()
        for mutated in [rows[:-1], rows[:-1] + [rows[0]]]:
            self.assertFalse(combine_workers(mutated, [], minutes=5)["capacity_verdict"]["passed"])
        rows[0][1]["config"]["account_offset"] = 1
        self.assertFalse(combine_workers(rows, [], minutes=5)["capacity_verdict"]["passed"])

    def test_crash_late_underfed_scaled_and_clock_skew_fail(self):
        for kind in ["crash", "late", "underfed", "scaled", "clock", "pool", "delivery", "latency", "runtime"]:
            with self.subTest(kind=kind):
                rows = deepcopy(workers())
                row = rows[0][1]
                if kind == "crash":
                    row["service_crashed"] = ["synthetic failure"]
                elif kind == "late":
                    next(iter(row["accounts"].values()))["reply_latency_s"]["max"] = 31
                elif kind == "underfed":
                    row["offered_load"]["persisted_messages"] = 749
                elif kind == "scaled":
                    row["config"]["scale"] = 2
                elif kind == "delivery":
                    row["delivery_concurrency"] = 2
                elif kind == "latency":
                    row["config"]["provider_latency_s"] = 1.6
                elif kind == "runtime":
                    row["runtime_errors"] = ["synthetic runtime error"]
                elif kind == "pool":
                    row["async_pool"]["max_overflow"] = 10
                else:
                    row["start_epoch"] += 2
                self.assertFalse(combine_workers(rows, [], minutes=5)["capacity_verdict"]["passed"])

    def test_explicit_parallel_and_network_settings_are_required_for_all_workers(self):
        rows = workers()
        for _, row in rows:
            row["delivery_concurrency"] = row["config"]["send_concurrency"] = 16
            row["config"]["provider_latency_s"] = row["config"]["history_latency_s"] = 1.6
        report = combine_workers(rows, [], minutes=5, concurrent_sends=16,
                                 provider_latency=1.6, history_latency=1.6)
        self.assertTrue(report["capacity_verdict"]["passed"], report["capacity_verdict"])
        self.assertFalse(combine_workers(rows, [], minutes=5)["capacity_verdict"]["passed"])

    def test_exit_failure_cannot_be_hidden_by_successful_reports(self):
        report = combine_workers(workers(), ["worker 0: exit status 1"], minutes=5)
        self.assertFalse(report["capacity_verdict"]["passed"])

if __name__ == "__main__":
    unittest.main()
