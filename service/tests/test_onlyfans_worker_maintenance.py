"""Maintenance contention must not starve independent intake."""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy.exc import OperationalError
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import onlyfans_worker as entry
from onlyfans_runtime.store import Refused

def database_error(code, invalidated=False):
    original = Exception("private database detail")
    original.pgcode = code
    return OperationalError("private SQL", {}, original, connection_invalidated=invalidated)

@pytest.mark.parametrize("failure", [Refused("private scope"), database_error("55P03"), database_error("57014")])
def test_local_maintenance_failure_keeps_healthy_intake_and_reports_once_failure(failure):
    seen, reports = [], []
    class Store:
        def request_poll(self, tenant, account): seen.append("poll")
    class Worker:
        def __init__(self, *args): pass
        def run(self, *args, **kwargs): seen.append("worker")
    def broken(*args, **kwargs): raise failure
    def healthy(*args, **kwargs): seen.append("healthy-maintenance")
    def load(*args, **kwargs): seen.append("load"); return [SimpleNamespace(account_id="healthy")], None, 1
    assert entry.run_loop(Store(), "tenant-a", threading.Event(), once=True, vault_jobs=broken,
        ledger_jobs=healthy, load_cohort=load, worker_type=Worker, observe=reports.append) == 1
    assert seen == ["healthy-maintenance", "load", "poll", "worker"]
    assert reports[0]["state"] == "maintenance_deferred"
    assert "private" not in str(reports)

@pytest.mark.parametrize("failure", [database_error("08006", True), database_error("42P01"), RuntimeError("private schema failure")])
def test_global_or_unclassified_failure_does_not_continue_intake(failure):
    def broken(*args, **kwargs): raise failure
    def forbidden(*args, **kwargs): pytest.fail("intake continued after global failure")
    reports=[]
    assert entry.run_loop(object(), "tenant-a", threading.Event(), once=True, vault_jobs=broken,
        load_cohort=forbidden, observe=reports.append) == 1
    assert reports == [{"schemaVersion":"of-worker.v1", "state":"unavailable"}]

def test_busy_maintenance_backs_off_while_empty_list_intake_continues(monkeypatch):
    clock=[0.0]; calls=[]; loads=[]; stop=threading.Event()
    monkeypatch.setattr(entry.time, "monotonic", lambda: clock[0])
    def broken(*args, **kwargs): calls.append(clock[0]); raise Refused("busy")
    def load(*args, **kwargs):
        loads.append(clock[0])
        if len(loads)==6: stop.set()
        return [], None, 0
    def wait(seconds): clock[0]+=1
    stop.wait=wait
    assert entry.run_loop(object(), "tenant-a", stop, vault_jobs=broken,
        load_cohort=load, observe=lambda _:None) == 0
    assert len(loads)==6 and len(calls)<len(loads)
    assert calls[0]==0 and all(b-a>=1 for a,b in zip(calls,calls[1:]))
