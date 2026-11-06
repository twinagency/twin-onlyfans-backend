"""Observed incident regressions; isolated PG and synthetic models, no live sends."""
import json
from datetime import timedelta
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database,event
from test_moxy_integrated_pg import setup,decision,model
from test_moxy_reply_recovery_pg import add,row,correction
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT
from db.models import Message
from db.onlyfans_moxy_models import MoxyEvent,MoxyAudit
from db.onlyfans_case_models import OfOperatorCase
from db.onlyfans_delivery_models import OfTextIntent
from onlyfans_runtime.store import utcnow
from moxy_replacement.conversation import history_problem,pending_messages,LEGACY_TECHNICAL_FALLBACK
from moxy_replacement.runtime import snapshot,correction_brief,wording_problem
from moxy_replacement.observations import observations


def h(direction,text,mid=1):return {'id':mid,'direction':direction,'text':text,'sender':'fan' if direction=='in' else 'ai'}

@pytest.mark.parametrize('text,reason',[
 ("haha a kiss was me being nice after you said you'd pass out on me",'unsupported_fan_attribution'),
 ("you said it babe, right before you told me you'd pass out on me",'unsupported_fan_attribution'),
 ("haha ages? babe we met like yesterday",'unsupported_relationship_history'),
 ("we haven't even made it through one full conversation yet babe",'unsupported_relationship_history'),
 (LEGACY_TECHNICAL_FALLBACK,'customer_facing_technical_fallback'),
 ("Technical difficulties, give me a moment.",'customer_facing_technical_fallback'),
 ("You didn't say that, I did. I shouldn't have said we met yesterday.",None),
 ("I don't know exactly when we started talking. That date was wrong.",None),
 ("I can't actually see when we started talking, so I can't give an exact date.",None)])
def test_speaker_and_relationship_claims_require_evidence(text,reason):
 snap={'history':[h('out',"you'd be lucky to get a goodnight kiss before i pass out cold"),h('in','when did i say that?',2)]}
 assert history_problem(snap,[text])==reason


def test_only_fan_message_can_ground_a_fan_attribution():
 text="You said you'd pass out."
 assert history_problem({'history':[h('in',"I'd pass out") ]},[text]) is None
 assert history_problem({'history':[h('out',"I'd pass out")]},[text])=='unsupported_fan_attribution'


def test_factual_error_outranks_repetition_and_brief_carries_speakers():
 wrong="you said you'd pass out on me"
 snap={'history':[h('out',wrong),h('in','when did i say that?',2)],'event':{'payload':{}}}
 assert wording_problem(snap,decision(text=wrong))=='unsupported_fan_attribution'
 brief=correction_brief('unsupported_fan_attribution',decision(text=wrong),snap)
 assert brief['kind']=='factual' and brief['evidence'][0]['direction']=='out'


def test_fallback_does_not_answer_or_close_the_input_batch():
 snap={'history':[h('out','Earlier reply'),h('in','When did I say that?',2),h('out',LEGACY_TECHNICAL_FALLBACK,3),h('in','WHAT',4)]}
 assert [m['id'] for m in pending_messages(snap)]==[2,4]


def test_consecutive_correction_and_question_reach_the_newest_turn(database,monkeypatch):
 engine,store,service,_,_,_=setup(database,monkeypatch)
 store.record(TENANT,ACCOUNT,event(mid=2,body='No, how long have we actually been talking?'));store.drain(TENANT,ACCOUNT)
 older=add(service,key='older',mid=2)
 store.record(TENANT,ACCOUNT,event(mid=3,body='And is that it, a kiss?'));store.drain(TENANT,ACCOUNT)
 service.prepare(TENANT,ACCOUNT,older,model(decision()))
 assert row(engine,older).reason=='moxy_input_superseded'
 newest=add(service,key='newest',mid=3)
 generated=model(decision(text="I shouldn't have guessed a date. I can't give an exact start from this history. A kiss was only a playful answer."))
 service.prepare(TENANT,ACCOUNT,newest,generated)
 assert row(engine,newest).status=='ready'
 prompt=json.loads(generated.calls[0][1]['content'])
 assert [m['id'] for m in prompt['pending_fan_messages']][-2:]==[2,3]
 assert 'Latest input never erases' in generated.calls[0][0]['content']
 assert prompt['history_scope']['relationship_start_known'] is False
 store.close()


def test_legacy_fallback_plus_two_new_failures_never_creates_another_reply(database,monkeypatch):
 engine,store,service,_,_,_=setup(database,monkeypatch)
 with Session(engine) as s,s.begin():
  s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=10,direction='out',body=LEGACY_TECHNICAL_FALLBACK,created_at=utcnow()))
 store.record(TENANT,ACCOUNT,event(mid=2,body='When did I say that?'));store.drain(TENANT,ACCOUNT)
 first=add(service,mid=2);wrong=decision(text="you told me you'd pass out on me")
 service.prepare(TENANT,ACCOUNT,first,model(wrong));retry=correction(engine)
 service.prepare(TENANT,ACCOUNT,retry,model(wrong))
 assert row(engine,retry).status=='held' and row(engine,retry).reason=='unsupported_fan_attribution'
 with Session(engine) as s:
  assert not s.scalar(select(OfTextIntent.intent_id))
  exhausted=[json.loads(x.after_json) for x in s.scalars(select(MoxyAudit)) if json.loads(x.after_json).get('phase')=='correction_exhausted']
  assert exhausted[0]['original_rejection']=='unsupported_fan_attribution'
  assert exhausted[0]['correction_rejection']=='unsupported_fan_attribution'
  assert exhausted[0]['recovery_rejection'] is None and not exhausted[0]['recovery_attempted']
  assert s.scalar(select(OfOperatorCase.case_id))
 store.close()


def test_pending_batch_extends_beyond_rolling_history_and_fallback(database,monkeypatch):
 engine,store,service,_,_,_=setup(database,monkeypatch)
 now=utcnow()
 with Session(engine) as s,s.begin():
  s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=300,direction='out',body=LEGACY_TECHNICAL_FALLBACK,created_at=now+timedelta(seconds=200)))
 # Unanswered questions arrive as live ingress: imported rows are context only (by design).
 for i in range(105):store.record(TENANT,ACCOUNT,event(mid=100+i,body='Unanswered question '+str(i)))
 while store.drain(TENANT,ACCOUNT):pass
 identity=add(service,mid=204)
 with Session(engine) as s,s.begin():
  snap=snapshot(s,ACCOUNT,101,s.get(MoxyEvent,(ACCOUNT,identity)))
  assert len(snap['history'])==100
  assert any(m['id']==100 for m in snap['pending_fan_messages'])
  assert any(m['id']==204 for m in snap['pending_fan_messages'])
  from moxy_replacement.runtime import validate_decision,observe_input
  from moxy_replacement.configuration import canonical
  from db.onlyfans_moxy_models import MoxyFan,MoxyAccount
  d=decision(text='I will answer that question.',question='Unanswered question 0')
  validate_decision(json.loads(s.get(MoxyAccount,ACCOUNT).settings_json),snap,d,planning=True)
  assert d['question']=='Unanswered question 0'
  ev=s.get(MoxyEvent,(ACCOUNT,identity));ev.snapshot_json=canonical(snap);observe_input(s,ev,d)
  assert any(o['question']=='Unanswered question 0' for o in json.loads(s.get(MoxyFan,(ACCOUNT,101)).state_json)['obligations'])
 store.close()


def test_pending_request_and_restriction_are_not_erased_by_sure(database,monkeypatch):
 engine,store,service,_,_,settings=setup(database,monkeypatch)
 store.record(TENANT,ACCOUNT,event(mid=2,body='No offers, please just talk to me.'));store.drain(TENANT,ACCOUNT)
 store.record(TENANT,ACCOUNT,event(mid=3,body='sure'));store.drain(TENANT,ACCOUNT)
 identity=add(service,mid=3)
 with Session(engine) as s,s.begin():snap=snapshot(s,ACCOUNT,101,s.get(MoxyEvent,(ACCOUNT,identity)))
 d=decision(text='Of course, what would you like to talk about?',observations=[{'kind':'ordinary_chat_only','active':True,'message_id':2,'quote':'No offers','product_id':None}])
 assert len(observations(d,snap))==1
 store.close()


def test_disputed_outgoing_claims_show_the_real_creator_source():
 from moxy_replacement.conversation import unverified_history_claims
 snap={'history':[h('out',"a kiss before I pass out cold",1),h('in','is that it a kiss?',2),h('out',"after you said you'd pass out on me",3),h('in','when did I say that?',4)]}
 evidence=unverified_history_claims(snap)
 assert evidence[0]['message_id']==3
 assert evidence[0]['related_creator_statements']==[{'message_id':1,'speaker':'creator','text':'a kiss before I pass out cold'}]


def test_direct_request_in_earlier_pending_message_remains_usable(database,monkeypatch):
 engine,store,service,_,_,_=setup(database,monkeypatch)
 store.record(TENANT,ACCOUNT,event(mid=2,body='send the sports outfit photo'));store.drain(TENANT,ACCOUNT)
 store.record(TENANT,ACCOUNT,event(mid=3,body='sure'));store.drain(TENANT,ACCOUNT)
 identity=add(service,mid=3)
 service.prepare(TENANT,ACCOUNT,identity,model(decision('offer','Here is the outfit photo.','first',sequence='fixture_level',request_kind='direct',request_quote='send the sports outfit photo')))
 assert row(engine,identity).status=='ready'
 store.close()


def test_wording_retry_rechecks_changed_catalogue_instead_of_sending_old_terms(database,monkeypatch):
 from db.models import CatalogItem
 engine,store,service,_,_,settings=setup(database,monkeypatch)
 text='That long journey sounded exhausting, tell me about the route.'
 with Session(engine) as s,s.begin():s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=10,direction='out',body=text,created_at=utcnow()-timedelta(seconds=5)))
 identity=add(service)
 service.prepare(TENANT,ACCOUNT,identity,model(decision('offer',text,'first',sequence='fixture_level')))
 retry=correction(engine)
 with Session(engine) as s,s.begin():s.get(CatalogItem,settings['catalogue_bindings']['first']).price_cents=7000
 service.prepare(TENANT,ACCOUNT,retry,model(decision('offer','Here is the requested outfit photo.','first',sequence='fixture_level')))
 assert row(engine,retry).status=='held' and row(engine,retry).reason=='real_inventory_missing_or_stale'
 with Session(engine) as s:assert not s.scalar(select(OfTextIntent.intent_id))
 store.close()
