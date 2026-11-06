"""Existing-state context, ordinary pilot actions and correction progress."""
import json
from datetime import timedelta
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import Message
from db.onlyfans_moxy_models import MoxyEvent
from db.onlyfans_case_models import OfOperatorCase
from moxy_replacement.runtime import snapshot
from moxy_replacement.configuration import ActionBlocked
from moxy_replacement.writer import parse
from test_moxy_integrated_pg import setup,decision,model
from test_moxy_pilot_pg import pilot
from test_moxy_reply_recovery_pg import add,row,send
from test_onlyfans_foundation_pg import database,event
from test_onlyfans_text_delivery_pg import ACCOUNT,TENANT
from onlyfans_runtime.store import utcnow


def pending(engine,identity):
    with Session(engine) as s:
        e=s.get(MoxyEvent,(ACCOUNT,identity))
        return snapshot(s,ACCOUNT,101,e)['pending_fan_messages']


def test_initial_import_is_context_not_thousands_of_unanswered_inputs(database,monkeypatch):
    engine,store,service,_,_,_=setup(database,monkeypatch)
    try:
        with Session(engine) as s,s.begin():
            for n in range(150):
                s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=10000+n,direction='in',
                    body='An old imported question?',created_at=utcnow()-timedelta(days=30,minutes=n)))
        identity=add(service)
        assert [m['id'] for m in pending(engine,identity)]==[1]
        service.prepare(TENANT,ACCOUNT,identity,model(decision(text='How has your day been?',unanswered_input_ids=[])))
        assert row(engine,identity).status=='ready',row(engine,identity).reason
    finally:store.close()


def test_latest_unannotated_receipt_does_not_revive_an_older_annotation(database,monkeypatch):
    engine,store,service,authority,_,_=setup(database,monkeypatch)
    try:
        first=add(service)
        service.prepare(TENANT,ACCOUNT,first,model(decision(text='I hear your question.',unanswered_input_ids=[1])))
        assert send(service,authority,engine,first)[0]=='accepted'
        store.record(TENANT,ACCOUNT,event(mid=2,body='What about my earlier question?'));store.drain(TENANT,ACCOUNT)
        second=add(service,key='second',mid=2)
        service.prepare(TENANT,ACCOUNT,second,model(decision(text='You are right, I got that wrong.',unanswered_input_ids=[])))
        assert send(service,authority,engine,second,mid=9002)[0]=='accepted'
        # A legacy receipt has no progress annotation. It must not select a
        # different older event's annotation as if attached to this reply.
        with Session(engine) as s,s.begin():
            e=s.get(MoxyEvent,(ACCOUNT,second));payload=json.loads(e.payload_json)
            payload.pop('_moxy_conversation_progress',None);e.payload_json=json.dumps(payload)
        store.record(TENANT,ACCOUNT,event(mid=3,body='Thanks, how was your morning?'));store.drain(TENANT,ACCOUNT)
        third=add(service,key='third',mid=3)
        assert [m['id'] for m in pending(engine,third)]==[3]
    finally:store.close()


@pytest.mark.parametrize('body',['Check my latest post!','I am having trouble replying right now.'])
def test_imported_promotion_or_fallback_does_not_consume_current_questions(database,monkeypatch,body):
    engine,store,service,_,_,_=setup(database,monkeypatch)
    try:
        with Session(engine) as s,s.begin():
            s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=80,direction='out',body=body,created_at=utcnow()))
        store.record(TENANT,ACCOUNT,event(mid=2,body='You have not answered my question.'));store.drain(TENANT,ACCOUNT)
        identity=add(service,mid=2)
        assert {m['id'] for m in pending(engine,identity)}=={1,2}
    finally:store.close()


def test_new_output_requires_progress_but_legacy_decision_decoding_does_not():
    d=decision();d.pop('unanswered_input_ids',None)
    assert parse(json.dumps(d))['action']=='reply'
    with pytest.raises(ActionBlocked,match='invalid_conversation_progress'):
        parse(json.dumps(d),require_progress=True)


def test_malformed_optional_observation_cannot_crash_the_turn(database,monkeypatch):
    engine,store,service,authority,_,_=setup(database,monkeypatch)
    try:
        identity=add(service)
        service.prepare(TENANT,ACCOUNT,identity,model(decision(text='How has your afternoon been?',
            observations=[{'kind':[]}],unanswered_input_ids=[])))
        assert row(engine,identity).status=='ready',row(engine,identity).reason
        assert send(service,authority,engine,identity)[0]=='accepted'
    finally:store.close()


@pytest.mark.parametrize('permission,flags',[('sexting',[]),(None,['complaint'])])
def test_permitted_offer_survives_pilot_and_next_message(database,monkeypatch,permission,flags):
    engine,store,service,authority,_,_=pilot(database,monkeypatch)
    try:
        identity=add(service)
        service.prepare(TENANT,ACCOUNT,identity,model(decision('offer','Here is the outfit photograph.','first',
            sequence='fixture_level',permission=permission,flags=flags,unanswered_input_ids=[])))
        assert row(engine,identity).status=='ready',row(engine,identity).reason
        assert send(service,authority,engine,identity)[0]=='accepted'
        with Session(engine) as s:
            assert not s.scalar(select(OfOperatorCase.case_id).where(OfOperatorCase.scope=='all'))
        store.record(TENANT,ACCOUNT,event(mid=2,body='How was your afternoon?'));store.drain(TENANT,ACCOUNT)
        later=add(service,key='next',mid=2)
        service.prepare(TENANT,ACCOUNT,later,model(decision(text='What have you been up to?',unanswered_input_ids=[])))
        assert row(engine,later).status=='ready'
    finally:store.close()


def test_missing_creator_fact_is_scoped_review_not_permanent_chat_hold(database,monkeypatch):
    engine,store,service,authority,_,_=pilot(database,monkeypatch)
    try:
        identity=add(service)
        service.prepare(TENANT,ACCOUNT,identity,model(decision('handover','',reason='creator_fact_unavailable',unanswered_input_ids=[1])))
        with Session(engine) as s:
            cases=list(s.scalars(select(OfOperatorCase)))
            assert cases and all(c.scope!='all' for c in cases)
        store.record(TENANT,ACCOUNT,event(mid=2,body='How was your afternoon?'));store.drain(TENANT,ACCOUNT)
        later=add(service,key='next',mid=2)
        service.prepare(TENANT,ACCOUNT,later,model(decision(text='What have you been up to?',unanswered_input_ids=[])))
        assert row(engine,later).status=='ready'
        assert send(service,authority,engine,later)[0]=='accepted'
    finally:store.close()
