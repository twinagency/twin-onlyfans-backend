"""Synthetic fleet benchmark: ten creator processes, one disposable shared PostgreSQL."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

SERVICE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SERVICE))
from native_pilot.tools.capacity_checks import evaluate_capacity


def combine_workers(rows, errors, *, minutes):
    """Missing, duplicate or differently shaped workers invalidate the fleet result."""
    accounts, starts, offered = {}, [], []
    failures = list(errors)
    for offset, row in rows:
        expected = f"of-{900000001 + offset}"
        config = row.get("config", {})
        if (row.get("scenario") != "target_10k" or config.get("accounts") != 1
                or config.get("account_offset") != offset or config.get("scale") != 1
                or config.get("sim_minutes") != minutes or set(row.get("accounts", {})) != {expected}
                or row.get("worker_database_role") != "worker"
                or row.get("async_pool") != {"size": 4, "max_overflow": 0}):
            failures.append(f"worker {offset}: invalid account or workload report")
        if expected in accounts:
            failures.append(f"worker {offset}: duplicate account report")
        accounts.update(row.get("accounts", {}))
        offered.append(row.get("offered_load", {}))
        if isinstance(row.get("start_epoch"), (int, float)):
            starts.append(row["start_epoch"])
        else:
            failures.append(f"worker {offset}: missing start evidence")
        failures.extend(row.get("service_crashed", ["missing service evidence"]))
        failures.extend(row.get("helper_errors", ["missing helper evidence"]))
    if len(rows) != 10:
        failures.append("ten worker reports are required")
    skew = max(starts) - min(starts) if len(starts) == 10 else None
    if skew is None or skew > 1:
        failures.append("worker scenario starts were not synchronized within one second")
    report = {
        "scenario": "target_100k",
        "topology": "ten isolated creator processes sharing one PostgreSQL database",
        "config": {"accounts": 10, "scale": 1, "sim_minutes": minutes},
        "accounts": accounts,
        "offered_load": {
            key: sum(value.get(key, 0) for value in offered)
            for key in ["scheduled_messages", "generated_messages", "persisted_messages", "scheduled_messages_per_hour"]
        },
        "service_crashed": failures,
        "helper_errors": [],
        "worker_start_skew_s": skew,
        "totals": {"replies": sum(row.get("totals", {}).get("replies", 0) for _, row in rows)},
    }
    report["offered_load"]["max_ingress_delay_s"] = max(
        (value.get("max_ingress_delay_s", float("inf")) for value in offered), default=float("inf"))
    report["capacity_verdict"] = evaluate_capacity(report)
    return report


def fleet_rss_mb(workers):
    total = 0
    for process, _ in workers:
        try:
            lines = Path(f"/proc/{process.pid}/status").read_text().splitlines()
            total += int(next(line for line in lines if line.startswith("VmRSS:")).split()[1]) / 1024
        except (OSError, StopIteration, ValueError):
            pass
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fans", type=int, choices=[50, 500, 1000], default=500,
                        help="fans per creator (500/5000/10000 in the fleet)")
    parser.add_argument("--sim-minutes", type=float, default=5)
    parser.add_argument("--concurrent-fans", type=int, choices=range(1, 65), default=32)
    parser.add_argument("--throttle", type=float, default=0.25)
    parser.add_argument("--out", required=True)
    parser.add_argument("--require-target-sla", action="store_true")
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        parser.error("the fleet harness requires Linux process/resource measurements")
    if args.sim_minutes < 1 or not 0.1 <= args.throttle <= 3600:
        parser.error("invalid workload duration or throttle")
    for name in ["LOAD_TEST_REUSE_DB", "LOAD_TEST_FLEET_TOKEN", "LOAD_TEST_FLEET_BARRIER"]:
        if os.environ.get(name):
            parser.error("the parent benchmark requires a new disposable database")
    admin = os.environ.get("ONLYFANS_TEST_PG_URL")
    if not admin:
        parser.error("ONLYFANS_TEST_PG_URL must name a disposable local PostgreSQL server")
    from native_pilot.tools.load_test import Cfg, Db, World
    db = Db(admin)  # refuses every non-loopback database host
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["DATABASE_URL"] = db.async_url
    os.environ["TWIN_BRIDGE_ENABLED"] = "0"
    os.environ["TWIN_BRIDGE_SEND_ENABLED"] = "0"
    os.environ["W7_WEBHOOK_DISPATCH_DISABLED"] = "1"
    workers, handles, rows, errors = [], [], [], []
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = output.parent / (output.stem + "-workers")
    evidence.mkdir(exist_ok=False)
    token = uuid.uuid4().hex
    peak_rss, pg_connection_limit = 0, None
    db.create()
    try:
        db.bootstrap()
        pg_connection_limit = int(db.run("SHOW max_connections")[0][0])
        db.seed(World(Cfg(name="fleet-seed", accounts=10, fans=args.fans,
                          scale=1, throttle_s=args.throttle)))
        db.run("CREATE TABLE public.load_harness_fleet(id integer PRIMARY KEY,token text NOT NULL)")
        db.run("INSERT INTO public.load_harness_fleet VALUES(1,:token)", {"token": token})
        with tempfile.TemporaryDirectory(prefix="synthetic-fleet-barrier-") as directory:
            barrier = Path(directory)
            for offset in range(10):
                child_out = evidence / f"creator-{offset}.json"
                log = (evidence / f"creator-{offset}.log").open("w")
                handles.append(log)
                env = dict(os.environ)
                env.update(LOAD_TEST_REUSE_DB=db.name, LOAD_TEST_FLEET_TOKEN=token,
                           LOAD_TEST_FLEET_BARRIER=str(barrier))
                command = [sys.executable, str(Path(__file__).with_name("load_test.py")),
                           "--scenario", "target_10k", "--accounts", "1", "--account-offset", str(offset),
                           "--fans", str(args.fans), "--sim-minutes", str(args.sim_minutes),
                           "--scale", "1", "--throttle", str(args.throttle),
                           "--concurrent-fans", str(args.concurrent_fans), "--out", str(child_out)]
                if args.profile:
                    command.append("--profile")
                process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL)
                workers.append((process, child_out))
            deadline = time.monotonic() + 90
            while not all((barrier / f"ready-{offset}.json").exists() for offset in range(10)):
                peak_rss = max(peak_rss, fleet_rss_mb(workers))
                if any(process.poll() is not None for process, _ in workers) or time.monotonic() > deadline:
                    raise RuntimeError("one or more creator processes did not become ready")
                time.sleep(0.1)
            start = barrier / "start.json"
            staging = barrier / "start.tmp"
            staging.write_text(json.dumps({"epoch": time.time() + 2}))
            staging.replace(start)
            print("ten synthetic creator processes ready; shared-clock workload starting", file=sys.stderr, flush=True)
            deadline = time.monotonic() + args.sim_minutes * 60 + 120
            while any(process.poll() is None for process, _ in workers):
                peak_rss = max(peak_rss, fleet_rss_mb(workers))
                if time.monotonic() > deadline:
                    raise RuntimeError("creator processes exceeded workload and shutdown deadline")
                time.sleep(0.25)
    except Exception as exc:
        # Fixed exception class only: no connection URLs, credentials or message text.
        errors.append(type(exc).__name__ + ": fleet startup or execution failed; inspect synthetic worker logs")
    finally:
        for process, _ in workers:
            if process.poll() is None:
                process.terminate()
        for process, _ in workers:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        for handle in handles:
            handle.close()
        for offset, (process, path) in enumerate(workers):
            if process.returncode != 0:
                errors.append(f"worker {offset}: exit status {process.returncode}")
            try:
                data = json.loads(path.read_text())
                if len(data["results"]) != 1:
                    raise ValueError("one result required")
                rows.append((offset, data["results"][0]))
            except (OSError, ValueError, KeyError):
                errors.append(f"worker {offset}: missing or invalid workload evidence")
        db.drop()  # drops only this parent's freshly named database, after every worker stopped
    report = combine_workers(rows, errors, minutes=args.sim_minutes)
    report["resources"] = {
        "creator_process_rss_peak_mb": round(peak_rss, 1),
        "creator_process_cpu_seconds": round(sum(row.get("process", {}).get("cpu_seconds", 0)
                                                 for _, row in rows), 2),
        "host_cpus": os.cpu_count(),
        "note": "creator workers only; excludes PostgreSQL, API, dashboard and supervisor overhead; no production CPU quota applied",
    }
    report["database_connection_budget"] = {
        "server_max_connections": pg_connection_limit,
        "worker_async_pool_limit_each": 4,
        "worker_bridge_pool_limit_each": 6,
        "creator_pool_theoretical_max": 100,
        "test_only_ingress_monitor_pools": "at least twenty connections across ten children plus parent setup",
        "note": "requires additional reserve for API, background jobs, supervisor, administration and deployment overlap",
    }
    report["source_commit"] = os.environ.get("GITHUB_SHA")
    output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    if errors or (args.require_target_sla and not report["capacity_verdict"]["passed"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
