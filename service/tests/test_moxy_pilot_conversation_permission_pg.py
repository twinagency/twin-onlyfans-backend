"""Conversational permissions retain normal validation, never specialist routing."""
from copy import deepcopy
import json
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_moxy_integrated_pg import run,decision
from test_moxy_pilot_pg import pilot
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT
from db.onlyfans_moxy_models import MoxyEvent,MoxyFan
from db.onlyfans_case_models import OfOperatorCase
from db.models import Transaction
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from moxy_replacement.pilot import route_specialist,decision as pilot_decision
from moxy_replacement.configuration import load


def settings():
 return {'products':[{'id':'p'}],'pilot':{'fan_ids':[101],'product_ids':['p'],'supervisor':'Jesse'},
         'operational':{'assigned_operator':'Jesse'}}


def test_reply_with_sexting_permission_retains_every_bubble_and_passes_pilot_gate():
 cfg=settings();d=decision(text='Come closer, babe.',permission='sexting',
   messages=[{'text':'Come closer, babe.','media':False},{'text':'Tell me what you want.','media':False}])
 original=deepcopy(d)
 assert route_specialist(cfg,d)==original
 pilot_decision(cfg,{'fan_id':101,'event':{'kind':'turn'}},d)
 assert d==original


@pytest.mark.parametrize('permission,action,flags',[
 ('customs','reply',[]),('ratings','reply',[]),('unknown','reply',[]),
 ('sexting','offer',[]),(None,'correct',[]),(None,'fulfil',[])])
def test_specialist_services_and_complaints_still_require_human(permission,action,flags):
 d=decision(action=action,permission=permission,flags=flags,
  messages=[{'text':'This must not be sent.','media':False}])
 routed=route_specialist(settings(),d)
 assert routed['action']=='handover' and routed['permission'] is None
 assert routed['text']=='' and routed['messages']==[]


@pytest.mark.parametrize('mode',['on','preview'])
@pytest.mark.parametrize('permission_state',['qualified','operator_amended','refused','unknown'])
def test_sexting_reply_uses_real_permission_validation_without_purchase_changes(database,monkeypatch,mode,permission_state):
 engine,store,svc,authority,_,cfg=pilot(database,monkeypatch,mode=mode)
 if permission_state=='operator_amended':
  cfg['creator_document']=deepcopy(load('amelia').creator)
  rule=next(r for r in cfg['creator_document']['rules'] if r['key']=='sexting')
  rule['requires']={};rule['value']=True
  svc.configure(TENANT,ACCOUNT,expected_revision=2,mode=mode,settings=cfg,actor='fixture-operator')
 if permission_state in {'operator_amended','refused','unknown'}:
  svc.fan_facts(TENANT,ACCOUNT,101,{'vip':False,'spender':False},actor='fixture-operator',evidence_ref='fixture:not-spending')
 if permission_state=='unknown':
  with Session(engine) as s,s.begin():
   f=s.get(MoxyFan,(ACCOUNT,101));facts=json.loads(f.facts_json);facts.pop('vip');facts.pop('spender');f.facts_json=json.dumps(facts)
 with Session(engine) as s:before=s.get(MoxyFan,(ACCOUNT,101)).facts_json
 identity,_=run(svc,engine,decision(text='Come closer, babe.',permission='sexting',
  messages=[{'text':'Come closer, babe.','media':False}]))
 with Session(engine) as s:
  e=s.get(MoxyEvent,(ACCOUNT,identity))
  if permission_state in {'refused','unknown'}:
   assert e.status=='held' and e.reason.startswith('condition_'),(e.status,e.reason)
  else:
   assert e.status==('ready' if mode=='on' else 'preview'),(e.status,e.reason)
   assert json.loads(e.decision_json)['permission']=='sexting'
  assert s.scalar(select(func.count()).select_from(OfOperatorCase))==0
  assert s.get(MoxyFan,(ACCOUNT,101)).facts_json==before
 if mode=='on' and permission_state in {'qualified','operator_amended'}:
  intent=svc.queue(TENANT,ACCOUNT,identity);wire=SyntheticTextTransport(ACCOUNT)
  assert authority.dispatch(TENANT,ACCOUNT,intent,wire)=='accepted'
  assert len(wire.calls)==1 and wire.calls[0].body=='Come closer, babe.'
 with Session(engine) as s:assert s.scalar(select(func.count()).select_from(Transaction))==0
 store.close()
