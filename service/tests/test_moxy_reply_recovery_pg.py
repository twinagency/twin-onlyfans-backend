"""Repair acceptance on disposable PostgreSQL; no provider credential or live transport."""
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from db.models import Fan,Message,GrokCall,GrokDailyCost,OfConversationState
from db.onlyfans_case_models import OfOperatorCase
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_moxy_models import MoxyEvent,MoxyAudit
from test_onlyfans_foundation_pg import database,event
from test_moxy_integrated_pg import setup,decision,model
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT,unthrottle
from onlyfans_runtime.store import utcnow
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from moxy_replacement.runtime import MoxyService,snapshot,content_status_violation
from moxy_replacement.configuration import digest,canonical
from moxy_replacement.diagnostics import record,completion


@pytest.fixture(autouse=True)
def event_trace(database,request):
    yield
    out=os.environ.get('MOXY_RECOVERY_TRACE_OUT')
    if not out:return
    _,engine=database
    with Session(engine) as s:
        events=[{key:getattr(e,key) for key in ('event_id','fan_id','kind','status','reason','model_call_id',
            'reserved_millicents','charged_millicents','intent_id','payload_json','decision_json')}
            for e in s.scalars(select(MoxyEvent))]
        audits=[json.loads(a.after_json) for a in s.scalars(select(MoxyAudit)) if a.actor=='moxy-reply-diagnostic']
        intents=[{key:getattr(i,key) for key in ('intent_id','fan_id','status','reason','body','receipt_json')}
            for i in s.scalars(select(OfTextIntent))]
    directory=Path(out);directory.mkdir(parents=True,exist_ok=True)
    (directory/(request.node.name+'.json')).write_text(json.dumps({'label':'SYNTHETIC, NO LIVE MODEL OR SEND',
        'events':events,'audits':audits,'intents':intents},indent=2),encoding='utf-8')


def row(engine,identity):
    with Session(engine) as s:return s.get(MoxyEvent,(ACCOUNT,identity))


def add(service,kind='turn',key='repair',mid=1,fan=101):
    return service.add_event(TENANT,ACCOUNT,fan,kind,key,{'source_message_id':mid})


def correction(engine):
    with Session(engine) as s:
        return next(e.event_id for e in s.scalars(select(MoxyEvent)) if json.loads(e.payload_json).get('correction_of'))


def send(service,authority,engine,identity,mid=9001,outcome='accepted'):
    unthrottle(engine)
    intent=service.queue(TENANT,ACCOUNT,identity)
    wire=SyntheticTextTransport(ACCOUNT,message_id=mid,outcome=outcome)
    return authority.dispatch(TENANT,ACCOUNT,intent,wire),wire,intent


@pytest.mark.parametrize('kind',['turn','welcome'])
def test_malformed_then_failed_correction_is_internal_attention_only(database,monkeypatch,kind):
    engine,store,service,authority,_,_=setup(database,monkeypatch)
    identity=add(service,kind)
    service.prepare(TENANT,ACCOUNT,identity,SyntheticNativeModel(raw_output='{broken'))
    retry=correction(engine)
    service.prepare(TENANT,ACCOUNT,retry,SyntheticNativeModel(raw_output='also broken'))
    assert row(engine,retry).status=='held' and row(engine,retry).reason=='invalid_model_json'
    assert row(engine,retry).charged_millicents is not None
    service.prepare(TENANT,ACCOUNT,retry,model(decision()))
    with Session(engine) as s:
        assert not s.scalar(select(OfTextIntent.intent_id))
        case=s.scalar(select(OfOperatorCase).where(OfOperatorCase.category=='moxy_technical'))
        assert case is not None and case.owner=='fixture-operator'
        ev=json.loads(case.evidence_json)
        assert ev['original_rejection']=='invalid_model_json' and ev['correction_rejection']=='invalid_model_json'
        audits=[json.loads(a.after_json) for a in s.scalars(select(MoxyAudit)) if a.actor=='moxy-reply-diagnostic']
        assert any(a.get('raw')=='{broken' for a in audits)
        assert any(a.get('phase')=='correction_exhausted' and a['recovery_attempted'] is False for a in audits)
        assert not any(a.get('phase')=='validated_fallback' for a in audits)
    # Scoped internal attention does not silently take over ordinary chat.
    store.record(TENANT,ACCOUNT,event(mid=2,body='How was your morning?'));store.drain(TENANT,ACCOUNT)
    next_id=add(service,key='next',mid=2)
    service.prepare(TENANT,ACCOUNT,next_id,model(decision(text='What made your morning interesting?')))
    assert row(engine,next_id).status=='ready'
    store.close()


@pytest.mark.parametrize('draft,expected',[
    ('You already opened it',True),('You already watched it',True),
    ('Did you enjoy the earlier video?',False),('The card is still waiting for you',False)])
def test_purchase_references_and_unrelated_fan_words(draft,expected):
    history=[{'id':1,'direction':'out','text':'earlier video','price_cents':1000,'paid':True,'product_id':'earlier_video'},
        {'id':2,'direction':'out','text':'second outfit','price_cents':2000,'paid':False,'product_id':'second_outfit'}]
    history += [{'id':i+3,'direction':'in','text':'I have not opened it. I watched football yesterday.'} for i in range(15)]
    assert content_status_violation({'history':history},[draft]) is expected


@pytest.mark.parametrize('paid',[False,None])
def test_unknown_access_is_not_ownership(paid):
    snap={'history':[], 'card_state':[{'id':1,'price_cents':1000,'paid':paid,'text':'old card'}]}
    assert content_status_violation(snap,['You already opened it'])
    assert not content_status_violation({'history':[{'id':2,'direction':'out','price_cents':0,'media_count':1}]},
        ['Did you like the preview?'])


def test_free_preview_with_unpaid_card_is_separate_access():
    snap={'history':[{'id':1,'direction':'out','price_cents':0,'media_count':1},
        {'id':2,'direction':'out','price_cents':1000,'paid':False,'media_count':1}]}
    assert not content_status_violation(snap,['Did you like the preview?'])
    assert content_status_violation(snap,['You already watched the full video'])


def test_fan_report_allows_acknowledgement_without_becoming_verified_ownership():
    snap={'history':[{'id':1,'direction':'out','price_cents':1000,'paid':False},
        {'id':2,'direction':'in','text':'I just opened it'}]}
    assert not content_status_violation(snap,['so glad you liked it, did you enjoy it?'])
    assert content_status_violation(snap,['You already own it'])
    assert snap['history'][0]['paid'] is False


def test_card_evidence_survives_beyond_history_window(database,monkeypatch):
    engine,store,service,_,_,_=setup(database,monkeypatch)
    with Session(engine) as s,s.begin():
        s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=10,direction='out',body='old paid card',
            price_cents=1500,is_paid=False,created_at=utcnow()-timedelta(days=3)))
        for i in range(110):
            s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=100+i,direction='in',body='ordinary chat',
                created_at=utcnow()-timedelta(days=2,seconds=-i)))
    identity=add(service,mid=209)
    with Session(engine) as s,s.begin():
        snap=snapshot(s,ACCOUNT,101,s.get(MoxyEvent,(ACCOUNT,identity)))
        assert not any(m['id']==10 for m in snap['history'])
        assert any(m['id']==10 for m in snap['card_state'])
        assert content_status_violation(snap,['You already opened it'])
    store.close()


def test_six_minute_history_wait_survives_restart_and_supersedes(database,monkeypatch):
    engine,store,service,_,_,_=setup(database,monkeypatch)
    identity=add(service)
    with Session(engine) as s,s.begin():s.get(MoxyEvent,(ACCOUNT,identity)).created_at=utcnow()-timedelta(minutes=7)
    original=service.authority._eligibility
    monkeypatch.setattr(service.authority,'_eligibility',lambda *a,**k:'history_not_current')
    unused=model(decision())
    service.prepare(TENANT,ACCOUNT,identity,unused)
    waiting=row(engine,identity)
    assert waiting.status=='pending' and waiting.reason=='history_not_current' and unused.calls==[]
    assert 55<(waiting.not_before-utcnow()).total_seconds()<=60
    # A newer message cancels the older wait, even while history is still stale.
    store.record(TENANT,ACCOUNT,event(mid=2,body='Actually I meant your morning.'));store.drain(TENANT,ACCOUNT)
    with Session(engine) as s,s.begin():s.get(MoxyEvent,(ACCOUNT,identity)).not_before=utcnow()-timedelta(seconds=1)
    service.prepare(TENANT,ACCOUNT,identity,unused)
    assert row(engine,identity).reason=='moxy_input_superseded'
    latest=add(service,key='latest',mid=2)
    with Session(engine) as s,s.begin():
        e=s.get(MoxyEvent,(ACCOUNT,latest));e.created_at=utcnow()-timedelta(minutes=7)
        # Existing durable rows stranded by the released implementation are recovered too.
        e.status='held';e.reason='history_not_current'
    monkeypatch.setattr(service.authority,'_eligibility',original)
    restarted=MoxyService(store)
    generated=model(decision(text='What did you want to know about my morning?'))
    assert restarted.step(TENANT,ACCOUNT,generated)==1
    assert row(engine,latest).status=='queued'
    assert restarted.step(TENANT,ACCOUNT,generated)==0
    store.close()


@pytest.mark.parametrize('fault',['timeout','missing_usage','expired_lease'])
def test_unknown_generation_has_operator_outcome_and_retains_accounting(database,monkeypatch,fault):
    engine,store,service,_,_,_=setup(database,monkeypatch)
    identity=add(service)
    broken=model(decision(),fail=fault in {'timeout','expired_lease'},
        reported_cost_millicents=None if fault=='missing_usage' else 1)
    service.prepare(TENANT,ACCOUNT,identity,broken)
    before=row(engine,identity)
    with Session(engine) as s,s.begin():
        call=s.get(GrokCall,before.model_call_id);reserved=call.cost_cents
        ledger=s.scalar(select(GrokDailyCost));total=ledger.cost_cents
        if fault=='expired_lease':
            e=s.get(MoxyEvent,(ACCOUNT,identity));e.status='generating';e.lease_until=utcnow()-timedelta(seconds=1)
    restarted=MoxyService(store)
    unused=model(decision())
    restarted.step(TENANT,ACCOUNT,unused)
    assert row(engine,identity).reason=='moxy_generation_reconciliation_required'
    with Session(engine) as s:
        assert s.get(GrokCall,before.model_call_id).cost_cents==reserved
        assert s.scalar(select(GrokDailyCost)).cost_cents==total
        assert not s.scalar(select(OfTextIntent.intent_id))
        assert not s.scalar(select(OfOperatorCase.case_id).where(OfOperatorCase.scope=='all'))
    store.record(TENANT,ACCOUNT,event(mid=2,body='Can we talk about hiking?'));store.drain(TENANT,ACCOUNT)
    next_id=add(service,key='later',mid=2)
    restarted.prepare(TENANT,ACCOUNT,next_id,model(decision(text='What hiking trails do you enjoy?')))
    assert row(engine,next_id).status=='ready'
    store.close()


def test_persisted_completion_reconciles_once_under_concurrent_restart(database,monkeypatch):
    engine,store,service,authority,_,_=setup(database,monkeypatch)
    identity=add(service)
    service.prepare(TENANT,ACCOUNT,identity,model(decision(text='What is your favourite walking trail?')))
    with Session(engine) as s,s.begin():
        e=s.get(MoxyEvent,(ACCOUNT,identity));call=s.get(GrokCall,e.model_call_id)
        ledger=s.scalar(select(GrokDailyCost));ledger.cost_cents+=e.reserved_millicents-call.cost_cents
        call.status='pending';call.cost_cents=e.reserved_millicents
        e.charged_millicents=None;e.decision_json=None;e.status='generating';e.lease_until=utcnow()-timedelta(seconds=1)
    def recover(_):return MoxyService(store).step(TENANT,ACCOUNT,model(decision()))
    with ThreadPoolExecutor(max_workers=2) as pool:counts=list(pool.map(recover,range(2)))
    assert 1<=sum(counts)<=2 and row(engine,identity).status=='queued'
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent))==1
        assert s.scalar(select(func.count()).select_from(GrokCall))==1
        assert s.scalar(select(GrokDailyCost)).cost_cents==1
        intent=s.get(MoxyEvent,(ACCOUNT,identity)).intent_id
    assert authority.dispatch(TENANT,ACCOUNT,intent,SyntheticTextTransport(ACCOUNT))=='accepted'
    assert row(engine,identity).status=='done'
    store.close()


def test_late_completion_after_recovery_is_accounted_but_never_sent(database,monkeypatch):
    engine,store,service,_,_,_=setup(database,monkeypatch)
    identity=add(service)
    def interrupt():
        with Session(engine) as s,s.begin():
            s.get(MoxyEvent,(ACCOUNT,identity)).lease_until=utcnow()-timedelta(seconds=1)
        MoxyService(store).step(TENANT,ACCOUNT,model(decision()))
    service.prepare(TENANT,ACCOUNT,identity,model(decision(text='This result arrived after its authority expired.'),
        on_generate=interrupt))
    assert row(engine,identity).status=='held'
    MoxyService(store).step(TENANT,ACCOUNT,model(decision()))
    assert row(engine,identity).reason=='moxy_late_generation_fenced'
    with Session(engine) as s:
        assert s.get(GrokCall,row(engine,identity).model_call_id).status=='done'
        assert s.scalar(select(GrokDailyCost)).cost_cents==1
        assert not s.scalar(select(OfTextIntent.intent_id))
    store.close()


def test_waiting_fan_does_not_prevent_another_fan(database,monkeypatch):
    engine,store,service,_,_,_=setup(database,monkeypatch)
    waiting=add(service)
    original=service.authority._eligibility
    def gate(*args,**kwargs):
        return 'history_not_current' if args[4].fan_id==101 else original(*args,**kwargs)
    monkeypatch.setattr(service.authority,'_eligibility',gate)
    service.prepare(TENANT,ACCOUNT,waiting,model(decision()))
    with Session(engine) as s,s.begin():
        s.add(Fan(account_id=ACCOUNT,fan_id=102))
        s.flush()
        s.add(OfConversationState(account_id=ACCOUNT,fan_id=102,mode='automated',revision=0,
            takeover_state='none',requires_attention=False))
    store.record(TENANT,ACCOUNT,event(mid=2,fan=102,body='How was your afternoon?'));store.drain(TENANT,ACCOUNT)
    eligible=add(service,key='other-fan',mid=2,fan=102)
    assert service.step(TENANT,ACCOUNT,model(decision(text='What have you been up to today?')))==1
    assert row(engine,eligible).status=='queued' and row(engine,waiting).status=='pending'
    store.close()


def test_wording_repair_preserves_offer_and_media_through_three_bubble_restart(database,monkeypatch):
    engine,store,service,authority,_,_=setup(database,monkeypatch)
    opening='What a difficult commute you had this morning.'
    with Session(engine) as s,s.begin():
        s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=30,direction='out',body=opening,
            created_at=utcnow()-timedelta(minutes=5)))
    identity=add(service)
    def offer(text,media_at=1):
        return decision('offer',text,'first',sequence='fixture_level',messages=[
            {'text':text,'media':media_at==0},
            {'text':'Here is the outfit photograph.','media':media_at==1},
            {'text':'How did the rest of your day go?','media':False}])
    service.prepare(TENANT,ACCOUNT,identity,model(offer(opening)))
    retry=correction(engine)
    brief=json.loads(row(engine,retry).payload_json)['rejected']
    assert brief['keep_terms']['media_ids']==[7101] and brief['keep_terms']['price_cents']==7500
    assert 'media_positions' not in brief and 'message_count' not in brief
    service.prepare(TENANT,ACCOUNT,retry,model(offer('That journey sounded exhausting.')))
    assert row(engine,retry).status=='ready'
    wires=[]
    for i in range(3):
        outcome,wire,_=send(MoxyService(store),authority,engine,retry,mid=9001+i)
        assert outcome=='accepted'
        wires.append(wire.calls[0])
    assert [w.media_ids for w in wires]==[(),(7101,),()]
    assert [w.price_cents for w in wires]==[0,7500,0]
    assert row(engine,retry).status=='done'
    store.close()


def test_worker_due_failure_has_scoped_evidence_without_exception_body(database,monkeypatch):
    from types import SimpleNamespace
    from onlyfans_runtime.native_worker import NativeWorker
    engine,store,service,_,_,_=setup(database,monkeypatch)
    identity=add(service)
    worker=object.__new__(NativeWorker);worker.store=store
    worker.accounts=[SimpleNamespace(account_id=ACCOUNT)]
    def broken():raise RuntimeError('private password should never appear')
    monkeypatch.setattr(worker,'_moxy_due',broken)
    assert worker.moxy_work_due() is False
    with Session(engine) as s:
        raw=[a.after_json for a in s.scalars(select(MoxyAudit)) if a.actor=='moxy-reply-diagnostic']
        assert len(raw)==1 and 'private password' not in raw[0]
        evidence=json.loads(raw[0]);assert evidence['boundary']=='due_query' and evidence['event_id']==identity
        assert evidence['error_type']=='RuntimeError'
    store.close()


@pytest.mark.parametrize('wrong',["I still don't have your name.","my side doesn't show me names, so just tell me"])
def test_known_name_repeated_failure_requires_operator_attention(database,monkeypatch,wrong):
    engine,store,service,authority,_,_=setup(database,monkeypatch)
    with Session(engine) as s,s.begin():
        fan=s.get(Fan,(ACCOUNT,101));fan.real_name='Gervonta';fan.is_name_real=True
    store.record(TENANT,ACCOUNT,event(mid=2,body='Do you remember my name?'));store.drain(TENANT,ACCOUNT)
    identity=add(service,mid=2)
    service.prepare(TENANT,ACCOUNT,identity,model(decision(text=wrong)))
    assert row(engine,identity).reason=='known_name_not_used'
    retry=correction(engine)
    service.prepare(TENANT,ACCOUNT,retry,model(decision(text=wrong)))
    assert row(engine,retry).status=='held'
    with Session(engine) as s:
        assert not s.scalar(select(OfTextIntent.intent_id))
        assert s.scalar(select(OfOperatorCase.case_id).where(OfOperatorCase.category=='moxy_technical'))
    store.close()


def test_unknown_name_does_not_authorise_a_profile_visibility_claim(database,monkeypatch):
    engine,store,service,authority,_,_=setup(database,monkeypatch)
    with Session(engine) as s,s.begin():
        fan=s.get(Fan,(ACCOUNT,101));fan.real_name=None;fan.of_display_name=None
    store.record(TENANT,ACCOUNT,event(mid=2,body='What is my name?'));store.drain(TENANT,ACCOUNT)
    identity=add(service,mid=2)
    service.prepare(TENANT,ACCOUNT,identity,model(decision(text="I can't actually see it from here.")))
    assert row(engine,identity).reason=='unsupported_profile_visibility_claim'
    retry=correction(engine)
    service.prepare(TENANT,ACCOUNT,retry,model(decision(text="I don't have a name to use for you here yet.")))
    assert row(engine,retry).status=='ready'
    assert send(service,authority,engine,retry)[0]=='accepted'
    store.close()


def test_diagnostics_are_private_bounded_and_do_not_change_snapshot(database,monkeypatch):
    engine,store,service,_,_,_=setup(database,monkeypatch)
    identity=add(service)
    with Session(engine) as s,s.begin():
        e=s.get(MoxyEvent,(ACCOUNT,identity));before=digest(snapshot(s,ACCOUNT,101,e))
        record(s,ACCOUNT,'test',event=e,raw='Bearer abcdefghijklmnop '+('x'*80000))
        s.flush()
        assert digest(snapshot(s,ACCOUNT,101,e))==before
        data=json.loads(s.scalar(select(MoxyAudit).where(MoxyAudit.actor=='moxy-reply-diagnostic')).after_json)
        assert 'abcdefghijklmnop' not in data['raw'] and len(data['raw'])<=20000
        assert data['raw_complete'] is False
    store.close()


def test_context_budget_failure_is_named_and_durable(database,monkeypatch):
    from moxy_replacement import writer
    engine,store,service,_,_,_=setup(database,monkeypatch)
    identity=add(service)
    monkeypatch.setattr(writer,'messages',lambda snap:[{'role':'user','content':'x'*140000}])
    unused=model(decision())
    service.prepare(TENANT,ACCOUNT,identity,unused)
    assert row(engine,identity).reason=='moxy_context_budget_exceeded' and unused.calls==[]
    with Session(engine) as s:
        assert any(json.loads(a.after_json).get('phase')=='context_failure'
            for a in s.scalars(select(MoxyAudit)) if a.actor=='moxy-reply-diagnostic')
    store.close()


def test_interrupted_multipart_reconciles_existing_intent_and_never_repeats_first(database,monkeypatch):
    engine,store,service,authority,_,_=setup(database,monkeypatch)
    identity=add(service)
    d=decision(text='That sounds like a long day.',messages=[
        {'text':'That sounds like a long day.','media':False},{'text':'What happened after work?','media':False}])
    service.prepare(TENANT,ACCOUNT,identity,model(d))
    accepted,first,_=send(service,authority,engine,identity)
    assert accepted=='accepted'
    unknown,second,intent=send(service,authority,engine,identity,mid=9002,outcome='accepted_lost_response')
    assert unknown=='unknown'
    unused=model(decision())
    MoxyService(store).step(TENANT,ACCOUNT,unused)
    assert unused.calls==[]
    assert authority.dispatch(TENANT,ACCOUNT,intent,second)=='unknown'
    assert len(first.calls)==len(second.calls)==1
    with Session(engine) as s:assert s.scalar(select(func.count()).select_from(OfTextIntent))==2
    store.close()
