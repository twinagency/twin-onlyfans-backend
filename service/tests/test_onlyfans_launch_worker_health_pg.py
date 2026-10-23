"""Worker observations cannot bless a replaced session or creator binding."""
import json

import pytest
from sqlalchemy.orm import Session

from test_onlyfans_launch_engagement_pg import schema_template, database  # noqa: F401
from test_onlyfans_connections_pg import verified, change, LIMITS, TENANT
from db.onlyfans_connection_models import OfProviderConnection
from onlyfans_runtime.connections import load_worker_cohort
from onlyfans_runtime.launch_operations import launch_readiness
from onlyfans_runtime.launch_operations_models import OfLaunchWorkerHealth
from onlyfans_runtime.native_worker import NativeWorker
from onlyfans_runtime.store import Refused


def native_worker(database, monkeypatch, *, outgoing=False):
    store, account, _ = verified(database, monkeypatch)
    if outgoing:
        change(store, account, "activate", 1, "c" * 64, LIMITS)
    accounts, _, _ = load_worker_cohort(store, TENANT, include_read_only=True)
    assert len(accounts) == 1
    return store, account, accounts[0], NativeWorker(store, TENANT, tuple(accounts),
        history_recovery=True, history_memory=True)


@pytest.mark.parametrize("outgoing", [False, True])
def test_stale_readonly_or_active_worker_cannot_refresh_current_session_health(database, monkeypatch, outgoing):
    store, account, loaded, worker = native_worker(database, monkeypatch, outgoing=outgoing)
    try:
        worker._heartbeat(loaded)
        with Session(store.engine) as s, s.begin():
            row=s.get(OfLaunchWorkerHealth, account)
            observed, retained=row.observed_at,row.capabilities_json
            connection=s.get(OfProviderConnection, account)
            connection.revision+=1
        report=worker.step()
        assert report.held and report.errors == 0
        with Session(store.engine) as s:
            row=s.get(OfLaunchWorkerHealth, account)
            assert (row.observed_at,row.capabilities_json) == (observed,retained)
        with pytest.raises(Refused, match="connection"):
            worker._heartbeat(loaded)
    finally:
        store.close()


def test_same_binding_session_rotation_needs_matching_worker_observation(database, monkeypatch):
    from onlyfans_runtime.connections import _open, _seal
    store, account, loaded, worker = native_worker(database, monkeypatch)
    try:
        worker._heartbeat(loaded)
        before=launch_readiness(store,TENANT,account)
        assert next(check for check in before["checks"] if check["key"] == "worker")["status"] == "ready"
        assert set(before["autonomy"]["workerCapabilities"]) == {"history_recovery","history_memory","engagement"}
        with Session(store.engine) as s,s.begin():
            connection=s.get(OfProviderConnection,account)
            clear=_open(connection,TENANT)
            connection.revision+=1
            connection.sealed_config=_seal(clear,TENANT,account,connection.binding_id,connection.revision)
        stale=launch_readiness(store,TENANT,account)
        assert next(check for check in stale["checks"] if check["key"] == "worker")["status"] == "unknown"
        accounts,_,_=load_worker_cohort(store,TENANT,include_read_only=True)
        current=NativeWorker(store,TENANT,tuple(accounts),history_recovery=True,history_memory=True)
        current._heartbeat(accounts[0])
        after=launch_readiness(store,TENANT,account)
        assert next(check for check in after["checks"] if check["key"] == "worker")["status"] == "ready"
        assert set(after["autonomy"]["workerCapabilities"]) == {"history_recovery","history_memory","engagement"}
        with Session(store.engine) as s:
            assert json.loads(s.get(OfLaunchWorkerHealth,account).capabilities_json)["connection_revision"] == 2
    finally:
        store.close()
