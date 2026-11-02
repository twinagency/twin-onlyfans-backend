from datetime import timedelta
import pytest
from sqlalchemy.orm import Session
from test_onlyfans_launch_worker_health_pg import native_worker, database, schema_template
from test_onlyfans_connections_pg import TENANT
from onlyfans_runtime import worker as intake
from onlyfans_runtime.provider_connector import NativeReadTransientError, NativeRateLimitError
from onlyfans_runtime.native_protocol import NativeProtocolError
from onlyfans_runtime.launch_operations import launch_readiness
from onlyfans_runtime.launch_operations_models import OfLaunchWorkerHealth
from onlyfans_runtime.store import utcnow


@pytest.mark.parametrize('archive_enabled',[False,True])
@pytest.mark.parametrize('outgoing',[False,True])
@pytest.mark.parametrize('failure',[NativeReadTransientError,NativeRateLimitError])
def test_paced_read_retry_does_not_claim_a_worker_crash(database,monkeypatch,outgoing,failure,archive_enabled):
    store,account,loaded,worker=native_worker(database,monkeypatch,outgoing=outgoing)
    worker.history_recovery=archive_enabled
    try:
        def unavailable(*a,**kw):raise failure()
        monkeypatch.setattr(intake,'read_step',unavailable)
        monkeypatch.setattr(worker,'_deliver',lambda *a:pytest.fail('retry must not send'))
        report=worker.step()
        assert report.held==1+int(archive_enabled) and report.errors==0 and report.claims==0 and report.queued==0
        with Session(store.engine) as s:
            assert s.get(OfLaunchWorkerHealth,account).last_error is None
    finally:store.close()


def test_protocol_failure_is_still_reported(database,monkeypatch):
    store,account,loaded,worker=native_worker(database,monkeypatch)
    try:
        def malformed(*a,**kw):raise NativeProtocolError('synthetic unsupported page')
        monkeypatch.setattr(intake,'read_step',malformed)
        report=worker.step()
        assert report.errors==1
        with Session(store.engine) as s:
            assert s.get(OfLaunchWorkerHealth,account).last_error=='cycle_failed'
    finally:store.close()


def test_stale_observation_is_unknown_instead_of_disabled(database,monkeypatch):
    store,account,loaded,worker=native_worker(database,monkeypatch)
    try:
        worker._heartbeat(loaded)
        with Session(store.engine) as s,s.begin():
            s.get(OfLaunchWorkerHealth,account).observed_at=utcnow()-timedelta(minutes=3)
        report=launch_readiness(store,TENANT,account)
        archive=next(x for x in report['checks'] if x['key']=='archive')
        assert archive['status']=='unknown'
        assert archive['blocker']=='worker_observation_missing_or_stale'
    finally:store.close()
