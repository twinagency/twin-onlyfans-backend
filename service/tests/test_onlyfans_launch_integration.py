"""Launch wiring defaults: every launch feature is off unless its worker flag is set; routes are mounted."""
import os
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import onlyfans_worker as entry


def run_once(**options):
    stop, seen = threading.Event(), {}
    class Worker:
        def __init__(self, store, tenant, accounts, **kw):
            seen.update(kw)
        def run(self, *args, **kw):
            stop.set()
    account = type("A", (), {"account_id": "acc-a"})()
    class Store:
        def request_poll(self, *a, **k): pass
    entry.run_loop(Store(), "tenant-a", stop, once=True, load_cohort=lambda *_, **__: ([account], None, 1),
                   worker_type=Worker, observe=lambda _: None, **options)
    return seen


def test_run_loop_passes_no_launch_features_by_default():
    assert run_once() == {}


def test_run_loop_forwards_explicit_launch_features():
    assert run_once(worker_options={"engagement": True}) == {"engagement": True}


def test_main_derives_options_only_from_exact_flags(monkeypatch):
    for name in ("ONLYFANS_HISTORY_RECOVERY_ENABLED", "ONLYFANS_HISTORY_MEMORY_ENABLED", "ONLYFANS_ENGAGEMENT_ENABLED"):
        monkeypatch.setenv(name, "true")  # anything but "1" stays off
    monkeypatch.setenv("ONLYFANS_ENGAGEMENT_ENABLED", "1")
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setenv("ONLYFANS_WORKER_TENANT", "tenant-a")
    monkeypatch.setenv("ONLYFANS_CONNECTION_KEY", "ab" * 32)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x")
    captured = {}
    import onlyfans_runtime.store
    monkeypatch.setattr(onlyfans_runtime.store, "Store", lambda url: type("S", (), {"close": lambda self: None})())
    monkeypatch.setattr(entry, "run_loop", lambda *a, **k: captured.update(k) or 0)
    assert entry.main(["--once"]) == 0
    assert captured["worker_options"] == {"engagement": True}


def test_native_worker_defaults_launch_features_off():
    import inspect
    from onlyfans_runtime.native_worker import NativeWorker
    params = inspect.signature(NativeWorker.__init__).parameters
    assert all(params[name].default is False for name in ("history_recovery", "history_memory", "engagement"))


def test_launch_routes_are_mounted_on_the_api_app(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/x")
    sys.modules.pop("onlyfans_app", None)
    import onlyfans_app
    paths = {(m, r.path) for r in onlyfans_app.app.routes for m in getattr(r, "methods", ())}
    base = "/onlyfans/configuration/v1/accounts/{account_id}"
    assert ("POST", base + "/personality/approve") in paths
    assert ("POST", base + "/engagement/settings") in paths
    assert ("POST", base + "/history-memory/fans/{fan_id}/corrections") in paths
    assert ("GET", base + "/history-memory/progress") in paths
