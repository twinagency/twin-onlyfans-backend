"""Capacity success requires real offered load and complete deadline evidence."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_pilot.tools.capacity_checks import evaluate_capacity


def complete_result():
    account = {"inbound_messages": 750, "reply_latency_s": {"n": 750, "max": 29.9},
               "unanswered_at_end": 0, "unanswered_over_30s": 0,
               "delivered_over_30s": 0, "throttle_respected": True}
    return {"scenario": "target_100k", "config": {"scale": 1, "sim_minutes": 5, "accounts": 10},
            "offered_load": {"scheduled_messages": 7500, "generated_messages": 7500,
                             "persisted_messages": 7500, "scheduled_messages_per_hour": 100000,
                             "max_ingress_delay_s": 0.1},
            "accounts": {f"of-{i}": copy.deepcopy(account) for i in range(10)},
            "service_crashed": [], "helper_errors": []}


class CapacityAcceptanceTests(unittest.TestCase):
    def test_complete_synthetic_evidence_passes(self):
        self.assertTrue(evaluate_capacity(complete_result())["passed"])

    def test_successful_process_with_underfed_traffic_fails(self):
        result = complete_result()
        result["offered_load"]["generated_messages"] = 150
        self.assertFalse(evaluate_capacity(result)["passed"])

    def test_persistence_or_generator_delay_invalidates_result(self):
        for key, value in [("persisted_messages", 7499), ("max_ingress_delay_s", 1.001),
                           ("scheduled_messages_per_hour", 99900)]:
            with self.subTest(key=key):
                result = complete_result()
                result["offered_load"][key] = value
                self.assertFalse(evaluate_capacity(result)["passed"])

    def test_one_late_or_missing_reply_fails_even_with_good_average(self):
        for mutation in [{"delivered_over_30s": 1}, {"unanswered_at_end": 1},
                         {"reply_latency_s": {"n": 749, "max": 15}},
                         {"reply_latency_s": {"n": 750, "max": 30.001}},
                         {"throttle_respected": False}]:
            with self.subTest(mutation=mutation):
                result = complete_result()
                result["accounts"]["of-0"].update(mutation)
                self.assertFalse(evaluate_capacity(result)["passed"])

    def test_scaled_clock_and_incomplete_evidence_fail(self):
        result = complete_result()
        result["config"]["scale"] = 2
        self.assertFalse(evaluate_capacity(result)["passed"])
        for key in ["offered_load", "service_crashed", "helper_errors"]:
            result = complete_result()
            del result[key]
            self.assertFalse(evaluate_capacity(result)["passed"])

    def test_crashes_and_helper_failures_are_not_capacity_success(self):
        for key in ["service_crashed", "helper_errors"]:
            result = complete_result()
            result[key] = ["synthetic failure"]
            self.assertFalse(evaluate_capacity(result)["passed"])


if __name__ == "__main__":
    unittest.main()
