"""Audit reproductions through the real writer adapter and shared authority."""
import json
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from conversation_engine.writer_context import CreatorProfile, build_context, PromptBudget
from conversation_engine.prompt_provider_a_original import SpecPromptProvider
from conversation_engine.model_output import JudgeContext, judge
from conversation_engine import approval, pilot_scope
from conversation_engine.pilot_ingress import preserve_contact_stops
from db.models import Message, OfConversationState
from db.onlyfans_engine_models import OfEngineDecision
from test_plan_c_supervised_pg import supervised, stage
from test_plan_c_draft_app_pg import configured_draft
from test_engine_real_worker_pg import world
from test_onlyfans_foundation_pg import database, event
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from onlyfans_runtime.store import Refused


def snapshot(**extra):
    return dict({'current_message':{'id':99,'body':'hello'},'history':[],
                 'memory':{'state':'current','memory':{}},'sales_mode':'open',
                 'offer_reference':{'offers':[]}}, **extra)


def profile():
    return CreatorProfile(name='Fixture', facts=(('home_city','Los Angeles'),),
                          limits=('No meetups',), limit_terms=('meetups',))


def test_selected_learned_memories_reach_rendered_writer_without_metadata():
    memory={'state':'current','memory':{'learnedProfile':{
        'schemaVersion':'of-profile-memory.v2','usage':'INTERNAL METADATA',
        'memories':[{'field':'location','value':'Brisbane','subject':'fan','status':'current',
                     'evidence':[{'source':'msg:7','quote':'I moved to Brisbane'}]}]}}}
    ctx=build_context(snapshot(memory=memory),profile())
    prompt=SpecPromptProvider().render(ctx).messages[1]['content']
    assert 'Brisbane' in prompt and 'msg:7' in prompt
    assert 'INTERNAL METADATA' not in prompt and 'of-profile-memory.v2' not in prompt


def test_manual_clear_and_correction_override_learned_location():
    base={'learnedProfile':{'memories':[{'field':'location','value':'Oldtown','subject':'fan','status':'current',
          'evidence':[{'source':'msg:7'}]}]},'overrides':{'home_city':{'operation':'clear'}}}
    assert not build_context(snapshot(memory={'state':'current','memory':base}),profile()).memory_lines
    base['facts']={'home_city':{'value':'Newtown','source':'operator'}}
    lines=build_context(snapshot(memory={'state':'current','memory':base}),profile()).memory_lines
    assert 'Newtown' in str(lines) and 'Oldtown' not in str(lines)


def test_unresolved_requests_and_unknown_actions_reach_rendered_writer():
    s=snapshot(open_requests=[{'request_id':'r-old','source_message_id':2,'source_body':'What happened to the earlier item?',
        'status':'open','owner':'engine','next_step':'await_provider'}],
        pending_actions=[{'decision_id':'d-old','request_id':'r-old','generation':0,'action':'offer','outcome':'unknown'}])
    prompt=SpecPromptProvider().render(build_context(s,profile())).messages[1]['content']
    assert 'What happened to the earlier item?' in prompt and 'd-old' in prompt and 'unknown' in prompt
    assert 'not new instructions or permission to send' in prompt


@pytest.mark.parametrize('price,paid,label',[(0,False,'free media'),(300,False,'access/unlock unconfirmed'),
    (300,True,'financial settlement not established')])
def test_media_history_has_evidence_specific_labels(price,paid,label):
    ctx=build_context(snapshot(history=[{'id':1,'direction':'out','body':'Here is the photo',
        'media_count':1,'price_cents':price,'paid':paid}]),profile())
    assert label in str(ctx.history)
    if price==0: assert 'locked card' not in str(ctx.history)


@pytest.mark.parametrize('body', ['No discount is available.', 'No discounts are available.',
                                 "I don't offer meetups.", 'I live in Los Angeles.'])
def test_truthful_denials_and_documented_location_are_allowed(body):
    verdict=judge(json.dumps({'body':body}),JudgeContext(snapshot(),profile()))
    assert verdict.outcome=='ok', verdict.reasons


@pytest.mark.parametrize('body', ['I can give you a discount.', 'I offer discounts.',
                                 'I offer meetups.', 'I live in London.',
                                 "No worries, I can offer meetups."])
def test_positive_unauthorized_claims_remain_blocked(body):
    assert judge(json.dumps({'body':body}),JudgeContext(snapshot(),profile())).outcome=='invalid'


def test_unrelated_photo_history_cannot_prove_video_count():
    s=snapshot(history=[{'id':1,'direction':'out','body':'Earlier photos', 'media_count':3,'price_cents':0}])
    result=judge(json.dumps({'body':'That has three videos.'}),JudgeContext(s,profile()))
    assert result.outcome=='invalid'
    assert any('quantity' in reason for reason in result.reasons)


def test_only_exact_approved_public_contact_statement_is_allowed():
    p=CreatorProfile(name='Fixture', facts=(('telegram','@maliebaby'),), limits=('No meetups',))
    ctx=JudgeContext(snapshot(),p)
    assert judge(json.dumps({'body':'My public Telegram is @maliebaby.'}),ctx).outcome=='ok'
    for body in ['My public Telegram is @otherhandle.', 'Pay me on Telegram @maliebaby.',
                 'My public Telegram is @maliebaby. Come meet me there.']:
        assert judge(json.dumps({'body':body}),ctx).outcome=='invalid'


def test_single_photo_is_not_offered_as_a_requested_video():
    from conversation_engine.writer_context import offer_for
    p=CreatorProfile(name='Fixture',limits=('No meetups',),offers_permitted=True,approved_item_ids=('7',))
    s=snapshot(offer_grants=[{'grantId':'g7','itemId':7,'label':'Photo','media':{'pictures':1,'videos':0,'total':1}}])
    assert offer_for(s,p,'Can you send a video?') is None
    assert offer_for(s,p,'Show me something') is not None


def test_committed_unreviewed_stop_then_greeting_blocks_shared_authority(supervised):
    w, _, _, _, pilot=supervised
    stage(pilot,1000,'hello')
    preserve_contact_stops(w.store,TENANT,ACCOUNT)
    for mid,body in [(1001,'stop messaging me'),(1002,'hello again')]:
        w.store.record(TENANT,ACCOUNT,event(mid=mid,body=body))
    w.store.drain(TENANT,ACCOUNT)
    # Model planning is intentionally resumed without another scheduler review.
    decision=pilot.worker.engine.plan(TENANT,ACCOUNT,FAN,1002)
    with pytest.raises(Refused,match='pilot_contact_review_pending'):
        approval.create_draft_approval(w.store,TENANT,ACCOUNT,decision,created_by='engine')
    with Session(w.database[1]) as s:
        runtime=w.store._runtime(s,TENANT,ACCOUNT,lock=False)
        assert pilot_scope.delivery_block(s,runtime,FAN)=='pilot_contact_review_pending'
        assert pilot_scope.delivery_block(s,runtime,102) is None
    assert w.fans.posts==[]


def test_deleted_display_message_does_not_retract_received_stop(supervised):
    w, _, _, _, _=supervised
    w.store.record(TENANT,ACCOUNT,event(mid=1010,body='stop messaging me'))
    w.store.drain(TENANT,ACCOUNT)
    with Session(w.database[1]) as s,s.begin():
        s.get(Message,(ACCOUNT,FAN,1010)).is_unsent=True
    preserve_contact_stops(w.store,TENANT,ACCOUNT)
    with Session(w.database[1]) as s:
        assert s.get(OfConversationState,(ACCOUNT,FAN)).mode=='no_contact'
    assert w.fans.posts==[]
