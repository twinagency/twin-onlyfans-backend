"""API admission never opens a provider client; worker evidence settles once."""
import asyncio,json
from datetime import timedelta
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_onlyfans_send_recovery_pg import database,template,configured,observed,ACCOUNT,FAN,TENANT,ACTOR
from db.models import OfConversationState,Message,OfMessageDelivery
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfTextIntent
from onlyfans_runtime import delivery_resolution,delivery_resolution_jobs,send_recovery
from onlyfans_runtime.launch_operations_models import OfLaunchCommand,OfLaunchWorkerHealth
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from onlyfans_runtime.store import utcnow,Refused

@pytest.mark.parametrize('revision_race',[False,True])
def test_api_queues_worker_checks_exact_evidence_and_replay_never_sends(database,monkeypatch,revision_race):
    store,authority=configured(database)
    identity=authority.prepare(TENANT,ACCOUNT,FAN,command_key='job-proof',expected_revision=0,body='Queued evidence check',origin='manual',actor=ACTOR)
    wire=SyntheticTextTransport(ACCOUNT,outcome='accepted_lost_response')
    assert authority.dispatch(TENANT,ACCOUNT,identity,wire)=='unknown'
    with Session(store.engine) as s,s.begin():
        intent=s.get(OfTextIntent,(ACCOUNT,identity));attempt=intent.attempt_id
        s.add(OfLaunchWorkerHealth(account_id=ACCOUNT,binding_id=s.get(OfRuntime,ACCOUNT).binding_id,observed_at=utcnow(),capabilities_json='{"delivery_resolution":true}'))
    monkeypatch.setenv('ONLYFANS_DELIVERY_RESOLUTION_ENABLED','1');monkeypatch.setenv('ONLYFANS_CONNECTION_KEY','synthetic-only')
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','api')
    assert delivery_resolution_jobs.available_for(store,TENANT,ACCOUNT)
    calls=[]
    async def provider_evidence(ledger,args):
        assert __import__('os').environ['ONLYFANS_PROCESS_ROLE']=='worker'
        calls.append(args)
        return send_recovery.reconcile(authority,TENANT,ACCOUNT,FAN,identity,history=[observed('Queued evidence check',utcnow())],complete=True,provider_message_id=9001,command_key=args.command_key,actor=args.actor,reason=args.reason,expected_revision=args.expected_revision)
    from native_pilot import recovery
    monkeypatch.setattr(recovery,'command',provider_evidence)
    args=dict(attempt=attempt,provider_message_id=9001,expected_revision=0,command_key='c'*64,actor=ACTOR)
    first=asyncio.run(delivery_resolution.resolve(store,TENANT,ACCOUNT,FAN,**args))
    assert first['state']=='queued' and not calls
    assert asyncio.run(delivery_resolution.resolve(store,TENANT,ACCOUNT,FAN,**args))==first
    with pytest.raises(Refused):asyncio.run(delivery_resolution.perform(store,TENANT,ACCOUNT,FAN,**args))
    if revision_race:
        with Session(store.engine) as s,s.begin():s.get(OfConversationState,(ACCOUNT,FAN)).revision+=1
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    delivery_resolution_jobs.process_request(store,ACCOUNT,'c'*64)
    delivery_resolution_jobs.process_request(store,ACCOUNT,'c'*64)
    with Session(store.engine) as s:
        result=s.get(OfLaunchCommand,(ACCOUNT,delivery_resolution_jobs.digest([delivery_resolution_jobs.RESULT,'c'*64])))
        assert json.loads(result.result_json)['state']==('refused' if revision_race else 'resolved')
        assert s.scalar(select(func.count()).select_from(Message).where(Message.account_id==ACCOUNT,Message.fan_id==FAN,Message.message_id==9001))==(0 if revision_race else 1)
        assert s.scalar(select(func.count()).select_from(OfMessageDelivery).where(OfMessageDelivery.account_id==ACCOUNT,OfMessageDelivery.message_id==9001))==(0 if revision_race else 1)
        assert bool(s.get(OfConversationState,(ACCOUNT,FAN)).unresolved_attempt_id)==revision_race
    assert len(calls)==(0 if revision_race else 1) and len(wire.calls)==1
    store.close()
