"""Resolution diagnostics cannot masquerade as delivery evidence or leak errors."""
import asyncio,json
from datetime import timedelta
import pytest
from sqlalchemy.orm import Session
from test_onlyfans_send_recovery_pg import database,template,configured,ACCOUNT,FAN,TENANT,ACTOR
from db.models import OfConversationState
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfTextIntent
from onlyfans_runtime import delivery_resolution,delivery_resolution_jobs as jobs
from onlyfans_runtime.launch_operations_models import OfLaunchCommand,OfLaunchWorkerHealth
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from onlyfans_runtime.store import utcnow

@pytest.mark.parametrize('expired',[False,True])
def test_failed_evidence_job_records_reason_without_releasing_unknown_or_replaying(database,monkeypatch,expired):
    store,authority=configured(database)
    try:
        identity=authority.prepare(TENANT,ACCOUNT,FAN,command_key='job-proof',expected_revision=0,body='Evidence check',origin='manual',actor=ACTOR)
        wire=SyntheticTextTransport(ACCOUNT,outcome='accepted_lost_response')
        assert authority.dispatch(TENANT,ACCOUNT,identity,wire)=='unknown'
        with Session(store.engine) as s,s.begin():
            attempt=s.get(OfTextIntent,(ACCOUNT,identity)).attempt_id
            s.add(OfLaunchWorkerHealth(account_id=ACCOUNT,binding_id=s.get(OfRuntime,ACCOUNT).binding_id,observed_at=utcnow(),capabilities_json='{"delivery_resolution":true}'))
        monkeypatch.setenv('ONLYFANS_DELIVERY_RESOLUTION_ENABLED','1');monkeypatch.setenv('ONLYFANS_CONNECTION_KEY','synthetic-only');monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','api')
        asyncio.run(jobs.enqueue(store,TENANT,ACCOUNT,FAN,attempt=attempt,provider_message_id=9001,expected_revision=0,command_key='d'*64,actor=ACTOR))
        calls=[]
        async def unavailable(*args,**kwargs):
            calls.append(1);raise RuntimeError('synthetic secret sentinel must never be logged')
        monkeypatch.setattr(delivery_resolution,'perform',unavailable)
        if expired:
            later=utcnow()+timedelta(minutes=6)
            monkeypatch.setattr(jobs,'utcnow',lambda:later)
        monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
        jobs.process_request(store,ACCOUNT,'d'*64);jobs.process_request(store,ACCOUNT,'d'*64)
        with Session(store.engine) as s:
            result=json.loads(s.get(OfLaunchCommand,(ACCOUNT,jobs.digest([jobs.RESULT,'d'*64]))).result_json)
            assert result['state']=='refused'
            assert result.get('reason')==('request_expired' if expired else 'evidence_check_failed')
            assert 'secret sentinel' not in json.dumps(result)
            assert s.get(OfConversationState,(ACCOUNT,FAN)).unresolved_attempt_id==attempt
            assert s.get(OfTextIntent,(ACCOUNT,identity)).status=='unknown'
        assert len(wire.calls)==1 and len(calls)==(0 if expired else 1)
    finally:store.close()


def test_uncertain_transport_error_diagnostics_do_not_print_exception_body(database,monkeypatch,capsys):
    store,authority=configured(database)
    try:
        identity=authority.prepare(TENANT,ACCOUNT,FAN,command_key='diagnostic',expected_revision=0,body='Safe body',origin='manual',actor=ACTOR)
        wire=SyntheticTextTransport(ACCOUNT)
        def fail(grant,**kwargs):raise RuntimeError('synthetic secret sentinel must never be logged')
        monkeypatch.setattr(wire,'send',fail)
        assert authority.dispatch(TENANT,ACCOUNT,identity,wire)=='unknown'
        output=capsys.readouterr().err
        assert 'onlyfans_dispatch_failed' in output and 'secret sentinel' not in output
    finally:store.close()
