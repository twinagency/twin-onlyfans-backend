"""The FASTT capacity harness (native_pilot/tools/load_test.py) runs, tiny, on REAL PostgreSQL.

Not a capacity measurement: it proves the harness itself works and that the runner invariants it
reports hold under a short burst + steady load + a mid-burst pause/takeover:

* the harness drives the real all-fans ``Service`` for two accounts and sends replies;
* the 20 s style throttle (here 2 s) is respected between sends of one account;
* a fan paused / taken over while its turn is in flight is refused before the wire (no POST after);
* nothing crashed, the metrics block is complete and JSON-serializable.

ONLYFANS_TEST_PG_URL must be an admin URL of a disposable LOCAL server on which this script may create
and drop its own database. Run as a plain script. Takes the PostgreSQL bootstrap time (about a minute on a
cold server) plus a few seconds.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

# Engine v2 enforces a 20 s account send interval floor. These older suites drive a virtual clock with
# 1 s throttles; the floor is a code constant, so they lower it here (production cannot).
import native_pilot.tuning as _tuning  # noqa: E402
_tuning.MIN_SEND_INTERVAL_FLOOR_S = 0.0
sys.path.insert(0, str(SERVICE / "native_pilot" / "tools"))

CHECKS = 0


def check(value, label=""):
    global CHECKS
    assert value, label
    CHECKS += 1


def main():
    if not os.environ.get("ONLYFANS_TEST_PG_URL"):
        sys.exit("FAILED: ONLYFANS_TEST_PG_URL (admin URL of a disposable PostgreSQL server) is required; nothing was verified")
    import load_test
    args = argparse.Namespace(scenario="tiny", pg_url=None, pg_container=None, accounts=2, fans=250, scale=30.0,
                              throttle=20, sim_minutes=60.0, seed=7, out=None)
    report = load_test.run_all(args)
    json.dumps(report, default=str)                      # serializable
    check(len(report["results"]) == 1)
    r = report["results"][0]
    check(r["scenario"] == "tiny" and not r["service_crashed"] and not r["helper_errors"], (r["service_crashed"], r["helper_errors"]))
    check(not any(k.startswith("held") and "service-error" in k for k in r["event_reasons"]), r["event_reasons"])
    check(len(r["accounts"]) == 2)
    check(r["totals"]["replies"] >= 2, {"expected": "the runner answered fans", "events": r["event_reasons"], "accounts": r["accounts"], "control": r["control"]})
    for acct, a in r["accounts"].items():
        check(a["throttle_respected"], (acct, a.get("min_admission_gap_s")))
        check(a["inbound_messages"] > 0 and a["reply_latency_s"]["n"] >= 0)
    c = r["control"]
    check(c["paused_or_taken_over"] >= 2, c)
    check(c["in_flight_fans"] >= 1 and c["in_flight_refused_before_wire"] == c["in_flight_fans"], c)
    for key in ("cpu_seconds", "rss_mb_peak", "threads_peak", "event_loop_lag_real_ms"):
        check(key in r["process"], key)
    check(r["database"]["connections_to_app_db"]["max"] >= 1)
    check("service.pending_fans" in r["timers"] and "ledger.begin_attempt" in r["timers"])
    print(f"native pilot load harness (tiny) on PostgreSQL: all cases passed ({CHECKS} checks)")


if __name__ == "__main__":
    main()
