"""Exact queue-group review, with no sending or authority created by a review."""
from datetime import timedelta,timezone
from hashlib import sha256
import json
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from conversation_engine import operator_v2,pilot_scope
from db.onlyfans_engine_models import OfEnginePilotWebhookDigest
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfDeliveryPolicy
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import utcnow,Refused
from test_plan_c_events_stage3_pg import (rig,stopped_text,configured_draft,world,database,
    queue_event,sent_event,ev,K,EVIDENCE,ACTOR,ACCOUNT,TENANT,FAN,FAN2,PROVIDER)
from test_plan_c_scope_pg import OTHER
from fans_api_adapter.queue_review import reviewed_queue_delivery
from test_engine_approval_api_pg import api,HEADERS,url


def stopped(rig):
    d=pilot_scope.describe(rig.w.store,TENANT,ACCOUNT)
    return pilot_scope.set_status(rig.w.store,TENANT,ACCOUNT,'stopped',expected_revision=d['revision'],
        command_key=K(96000),actor=ACTOR,reason='exact queue evidence review')


def proof(raws):
    now=utcnow().replace(tzinfo=timezone.utc)
    return {'rawBodies':[r.decode() for r in raws],'observedAt':now.isoformat(),
        'queueListingSha256':'ab'*32,'pendingQueues':0,'externalWritersExcluded':True,
        'fanHistory':[{'fanId':fan,'sha256':'cd'*32,'coveredFrom':(now-timedelta(days=1)).isoformat(),
                       'coveredThrough':now.isoformat(),'activityReviewed':True} for fan in (FAN,FAN2)]}


def held(rig):
    a=queue_event();b=queue_event();b['payload']['isCanceled']=True
    raws=[rig.post(a,key='reviewed-queue'),rig.post(b,key='reviewed-queue')]
    stopped(rig)
    return raws,next(i for i in rig.items('provider_event') if i['eventKey']=='reviewed-queue')


def submit(rig,item,raws,n=96001,**patch):
    return rig.reconcile(item,n,raw=next(r for r in raws if sha256(r).hexdigest()==item['payloadSha256']),
                         queueReview=proof(raws),**patch)


def test_review_resolves_exact_conflict_group_but_grants_no_send_and_new_variant_reholds(rig):
    raws,item=held(rig)
    # Existing route continues to reject arbitrary queue clearing.
    with pytest.raises(operator_v2.Conflict):rig.reconcile(item,96002,raw=raws[0])
    result=submit(rig,item,raws)
    assert result['resolved'] and result['sendAuthorized'] is False and result['activationAuthorized'] is False
    assert rig.block(FAN) is None and rig.block(FAN2) is None
    with Session(rig.w.database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfEnginePilotWebhookDigest))==2
        assert all(x.safety_hold for x in s.scalars(select(OfEnginePilotWebhookDigest)))
        assert not s.get(OfRuntime,ACCOUNT).enabled and not s.get(OfDeliveryPolicy,ACCOUNT).enabled
        audit=s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.kind=='plan_c_queue_review'))
        before=json.loads(audit.before_json)
        local={'digests':[{'key':'reviewed-queue','hash':sha256(r).hexdigest()} for r in raws],
               'queue_reviews':[{'before':before,'result':json.loads(audit.result_json)}]}
    assert reviewed_queue_delivery(local,'reviewed-queue',json.loads(raws[0]),PROVIDER)
    changed=queue_event();changed['payload']['canUnsend']=True
    extra=rig.post(changed,key='reviewed-queue')
    assert rig.block(FAN)==rig.block(FAN2)=='pilot_evidence_pending'
    local['digests'].append({'key':'reviewed-queue','hash':sha256(extra).hexdigest()})
    assert not reviewed_queue_delivery(local,'reviewed-queue',json.loads(raws[0]),PROVIDER)
    assert rig.w.fans.posts==[] and rig.seen==[]


@pytest.mark.parametrize('bad',['missing_variant','wrong_bytes','wrong_fans','active_queue','no_exclusion',
    'stale','future','future_history','short_history','unreviewed_activity','other_actor','wrong_revision','wrong_provider'])
def test_incomplete_or_unauthorized_review_preserves_all_holds(rig,bad):
    raws,item=held(rig);p=proof(raws);patch={}
    if bad=='missing_variant':p['rawBodies']=p['rawBodies'][:1]
    if bad=='wrong_bytes':p['rawBodies'][0]+=' '
    if bad=='wrong_fans':p['fanHistory']=p['fanHistory'][:1]
    if bad=='active_queue':p['pendingQueues']=1
    if bad=='no_exclusion':p['externalWritersExcluded']=False
    if bad=='stale':p['observedAt']=(utcnow().replace(tzinfo=timezone.utc)-timedelta(hours=1)).isoformat()
    if bad=='future':p['observedAt']=(utcnow().replace(tzinfo=timezone.utc)+timedelta(hours=1)).isoformat()
    if bad=='future_history':p['fanHistory'][0]['coveredThrough']=(utcnow().replace(tzinfo=timezone.utc)+timedelta(hours=1)).isoformat()
    if bad=='short_history':p['fanHistory'][0]['coveredFrom']=(utcnow().replace(tzinfo=timezone.utc)+timedelta(hours=1)).isoformat()
    if bad=='unreviewed_activity':p['fanHistory'][0]['activityReviewed']=False
    if bad=='other_actor':patch['actor']=OTHER
    if bad=='wrong_revision':patch['scopeRevision']=item['scopeRevision']-1
    if bad=='wrong_provider':p['rawBodies']=[r.decode().replace(PROVIDER,'acct_wrong') for r in raws]
    expected=Refused if bad=='other_actor' else (operator_v2.BadRequest,operator_v2.Conflict)
    with pytest.raises(expected) as error:
        rig.reconcile(item,96010,raw=next(r for r in raws if sha256(r).hexdigest()==item['payloadSha256']),queueReview=p,**patch)

    assert rig.block(FAN)==rig.block(FAN2)=='pilot_evidence_pending'
    assert rig.w.fans.posts==[] and rig.seen==[]


@pytest.mark.parametrize('other_unknown',[False,True])
def test_queue_review_does_not_dismiss_queued_send_or_other_event(rig,other_unknown):
    rig.post(sent_event(99001,FAN,True),key='independent-send')
    rig.ticks()  # Project the independent send before stopping. No inbound/model call.
    assert rig.items('held_send_evidence')
    raws,item=held(rig)
    if other_unknown:rig.post(ev('posts.new',{'id':123}),key='independent-unknown')
    submit(rig,item,raws)
    from db.onlyfans_fans_intake_models import OfFansEvidence
    with Session(rig.w.database[1]) as session:
        row=session.scalar(select(OfFansEvidence).where(OfFansEvidence.kind=='human_sent'))
        assert row.status=='held' and row.reason=='queued_send_to_engine_owned_fan'
    assert rig.block(FAN)=='pilot_evidence_pending'
    assert rig.block(FAN2)==('pilot_evidence_pending' if other_unknown else None)
    assert rig.w.fans.posts==[]


def test_review_then_separate_authorized_restart_answers_both_fans_once(rig):
    from test_plan_c_text_activation_pg import start
    from model_transport.pilot_supervised import build_supervised_pilot
    raws,item=held(rig)
    submit(rig,item,raws)
    assert rig.w.fans.posts==[]
    revision=pilot_scope.describe(rig.w.store,TENANT,ACCOUNT)['revision']
    start(rig.w,revision,command=96040)
    pilot=build_supervised_pilot(rig.w.store,rig.env,model_transport=rig.model,fans_transport=rig.w.fans.transport)
    rig.fans_in(base=96050)
    for _ in range(4):pilot.tick()
    assert len(rig.w.fans.posts)==2 and len(rig.seen)==2
    for _ in range(2):pilot.tick()
    assert len(rig.w.fans.posts)==2


def test_active_scope_cannot_use_review_to_clear_holds(rig):
    raw=rig.post(queue_event(),key='active-queue')
    [item]=rig.items('provider_event')
    with pytest.raises(operator_v2.Conflict,match='stopped'):
        submit(rig,item,[raw])
    assert rig.block(FAN)==rig.block(FAN2)=='pilot_evidence_pending'


def test_existing_http_authorization_applies_to_queue_review(rig,api):
    raws,item=held(rig)
    raw=next(r for r in raws if sha256(r).hexdigest()==item['payloadSha256'])
    body={'commandKey':K(96060),'actor':ACTOR,'evidenceRef':EVIDENCE,'scopeRevision':item['scopeRevision'],
          'eventEpoch':item['eventEpoch'],'eventKey':item['eventKey'],'payloadSha256':item['payloadSha256'],
          'rawBody':raw.decode(),'queueReview':proof(raws)}
    endpoint=url('unresolved/'+item['id']+'/reconcile')
    assert api.client.post(endpoint,json=body).status_code==401
    assert rig.block(FAN)=='pilot_evidence_pending'
    response=api.client.post(endpoint,json=body,headers=HEADERS)
    assert response.status_code==200,response.text
    assert response.json()['sendAuthorized'] is False
    assert api.client.post(endpoint,json=body,headers=HEADERS).json()['replayed']


def test_sibling_digest_command_key_is_rejected_before_any_disposition(rig):
    raws,item=held(rig)
    from conversation_engine.pilot_scope import digest_resolution_key
    with Session(rig.w.database[1]) as s:
        runtime=rig.w.store._runtime(s,TENANT,ACCOUNT,lock=False)
        sibling=next(sha256(r).hexdigest() for r in raws if sha256(r).hexdigest()!=item['payloadSha256'])
        key=digest_resolution_key(runtime.binding_id,item['eventEpoch'],item['eventKey'],sibling)
    with pytest.raises(operator_v2.BadRequest):submit(rig,item,raws,n=int(key,16))
    assert rig.block(FAN)==rig.block(FAN2)=='pilot_evidence_pending'
