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
    cleared=build_context(snapshot(memory={'state':'current','memory':base}),profile()).memory_lines
    assert 'Oldtown' not in str(cleared) and 'Operator-cleared' in str(cleared)
    base['overrides']['home_city']['operation']='set'
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
    import threading
    w, _, _, _, pilot=supervised
    stage(pilot,1000,'hello')
    reviewed, resume = threading.Event(), threading.Event()
    failures=[]
    def stale_worker():
        try:
            preserve_contact_stops(w.store,TENANT,ACCOUNT)
            reviewed.set()
            if not resume.wait(10):raise RuntimeError('race barrier timed out')
            decision=pilot.worker.engine.plan(TENANT,ACCOUNT,FAN,1002)
            approval.create_draft_approval(w.store,TENANT,ACCOUNT,decision,created_by='engine')
        except BaseException as exc:
            failures.append(exc)
    thread=threading.Thread(target=stale_worker)
    thread.start()
    try:
        assert reviewed.wait(10)
        # A separate transaction commits after the other worker's review.
        for mid,body in [(1001,'stop messaging me'),(1002,'hello again')]:
            w.store.record(TENANT,ACCOUNT,event(mid=mid,body=body))
        w.store.drain(TENANT,ACCOUNT)
    finally:
        resume.set();thread.join(15)
    assert not thread.is_alive()
    assert len(failures)==1 and isinstance(failures[0],Refused)
    assert 'pilot_contact_review_pending' in str(failures[0])
    with Session(w.database[1]) as s,s.begin():
        runtime=w.store._runtime(s,TENANT,ACCOUNT)
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


def test_old_unanswered_request_reaches_actual_model_request(supervised):
    import uuid
    from db.onlyfans_engine_models import OfEngineRequest
    from onlyfans_runtime.store import utcnow
    w, _, _, seen, pilot=supervised
    pilot.worker.engine.select_simulated(TENANT,ACCOUNT,FAN)
    question='Please check the missing beach photo'
    w.store.record(TENANT,ACCOUNT,event(mid=1100,body=question))
    w.store.drain(TENANT,ACCOUNT)
    with Session(w.database[1]) as s,s.begin():
        runtime=w.store._runtime(s,TENANT,ACCOUNT)
        s.add(OfEngineRequest(account_id=ACCOUNT,request_id=str(uuid.uuid4()),fan_id=FAN,
            binding_id=runtime.binding_id,source_message_id=1100,status='open',owner='engine',
            next_step='prepare_reply',revision=0,created_at=utcnow(),updated_at=utcnow()))
    for mid in range(1101,1121):
        w.store.record(TENANT,ACCOUNT,event(mid=mid,body='An intervening ordinary message'))
    w.store.drain(TENANT,ACCOUNT)
    stage(pilot,1121,'hello again')
    prompt=seen[-1]['messages'][-1]['content']
    history=prompt.split('LATEST:')[0]
    assert question not in history
    assert question in prompt.split('UNRESOLVED REQUESTS AND ACTIONS')[1]
    assert w.fans.posts==[]


@pytest.mark.parametrize('raw', ['{', '{"body":"\\u0000","direction":"in","created_at":"2026-11-08T04:00:00+00:00"}',
    '{"body":"\\ud800","direction":"in","created_at":"2026-11-08T04:00:00+00:00"}',
    '{"direction":"in","created_at":"tomorrow"}', '{"direction":"in","created_at":"2026-02-30T00:00:00+00:00"}'])
def test_ingress_metadata_parser_does_not_abort_transaction(database,raw):
    from sqlalchemy import text
    with database[1].begin() as c:
        assert c.scalar(text('SELECT of_engine_pilot_ingress_metadata(:raw)'),{'raw':raw}) is None
        assert c.scalar(text('SELECT 1'))==1


@pytest.mark.parametrize('body', ["I can't wait to offer discounts.","I don't mind offering a discount."])
def test_negative_words_do_not_hide_an_affirmative_discount(body):
    from conversation_engine.model_output import edit_problems
    ctx=JudgeContext(snapshot(),profile())
    assert judge(json.dumps({'body':body}),ctx).outcome=='invalid'
    assert any('discount' in x for x in edit_problems([body],ctx))


@pytest.mark.parametrize('body', ["I don't offer discounts.","Discounts aren't available.",
    'I live in Los Angeles babe.','I live in Los Angeles too.'])
def test_plain_refusals_and_location_vocatives_remain_usable(body):
    assert judge(json.dumps({'body':body}),JudgeContext(snapshot(),profile())).outcome=='ok'


@pytest.mark.parametrize('body', ['I live in London babe.','I live in London too.',
    'I live in London, not Los Angeles.','I live in Los Angeles London.'])
def test_wrong_location_is_not_rescued_by_a_vocative_or_approved_city_mention(body):
    assert judge(json.dumps({'body':body}),JudgeContext(snapshot(),profile())).outcome=='invalid'


def test_selected_event_dates_and_evidence_are_preserved():
    memory={'state':'current','memory':{'learnedProfile':{'memories':[{'field':'travel','value':'trip to Rome',
        'subject':'fan','status':'upcoming','event_start':'2026-12-05','event_end':'2026-12-10',
        'last_confirmed':'2026-11-08','evidence':[{'source':'msg:7','at':'2026-11-08','quote':'I fly December 5'}]}]}}}
    prompt=SpecPromptProvider().render(build_context(snapshot(memory=memory),profile())).messages[1]['content']
    for value in ['2026-12-05','2026-12-10','2026-11-08','I fly December 5','upcoming']:
        assert value in prompt


def test_request_budget_reserves_unknown_action_and_active_question():
    from conversation_engine.writer_context import request_context_from
    requests=[{'request_id':'old'+str(i),'source_message_id':i,'source_body':'x'*300,
        'status':'open','owner':'engine','next_step':'waiting'} for i in range(20)]
    requests.append({'request_id':'current','source_message_id':99,'source_body':'Current question?',
        'status':'open','owner':'engine','next_step':'prepare_reply'})
    s=snapshot(open_requests=requests,pending_actions=[{'decision_id':'unknown-action',
        'request_id':'old0','generation':0,'action':'offer','outcome':'unknown'}])
    lines=request_context_from(s,PromptBudget())
    rendered='\n'.join(lines)
    assert 'unknown-action' in rendered and 'Current question?' in rendered
    assert 'omitted' in rendered
    assert len(rendered)<=PromptBudget().request_chars


def test_malformed_ingress_metadata_is_a_fan_scoped_hold_not_a_query_failure(supervised):
    from db.onlyfans_models import OfIngress
    from onlyfans_runtime.store import utcnow
    w, _, _, seen, pilot=supervised
    stage(pilot,1299,'hello',fan=102)  # establish the provider link without sending
    before=len(seen)
    with Session(w.database[1]) as s,s.begin():
        runtime=w.store._runtime(s,TENANT,ACCOUNT)
        s.add(OfIngress(account_id=ACCOUNT,event_key='d'*64,binding_id=runtime.binding_id,
            fan_id=FAN,message_id=1300,source='ws',payload='{bad-json',status='applied',
            received_at=utcnow(),applied_at=utcnow()))
    result=preserve_contact_stops(w.store,TENANT,ACCOUNT)
    assert result['pending']
    with Session(w.database[1]) as s,s.begin():
        runtime=w.store._runtime(s,TENANT,ACCOUNT)
        assert pilot_scope.delivery_block(s,runtime,FAN)=='pilot_contact_review_pending'
        assert pilot_scope.delivery_block(s,runtime,102) is None
    assert len(seen)==before and w.fans.posts==[]


def test_malformed_review_backlog_does_not_hide_the_other_fan(supervised):
    from db.onlyfans_models import OfIngress
    from onlyfans_runtime.store import utcnow
    from conversation_engine.pilot_ingress import review_pending
    w, _, _, _, _=supervised
    with Session(w.database[1]) as s,s.begin():
        runtime=w.store._runtime(s,TENANT,ACCOUNT)
        for i in range(5):
            s.add(OfIngress(account_id=ACCOUNT,event_key=str(i)*64,binding_id=runtime.binding_id,
                fan_id=FAN,message_id=1400+i,source='ws',payload='{bad-json',status='applied',
                received_at=utcnow(),applied_at=utcnow()))
    w.store.record(TENANT,ACCOUNT,event(mid=1500,fan=102,body='hello'))
    w.store.drain(TENANT,ACCOUNT)
    result=preserve_contact_stops(w.store,TENANT,ACCOUNT,limit=2)
    assert result=={'reviewed':1,'pending':True}
    with Session(w.database[1]) as s:
        runtime=w.store._runtime(s,TENANT,ACCOUNT,lock=False)
        scope,config=pilot_scope.require(s,runtime)
        assert review_pending(s,runtime,scope,config,FAN)
        assert not review_pending(s,runtime,scope,config,102)


def test_dense_learned_memory_keeps_essential_fact_dates_and_uncertainty():
    item={'field':'travel','value':'trip to Rome '+'x'*175,'subject':'fan','status':'uncertain',
        'event_start':'2026-12-05','event_end':'2026-12-10','last_confirmed':'2026-11-08',
        'review_at':'2026-11-09','active_until':'2026-12-10','potentially_stale':True,
        'alternatives':['a'*150,'b'*150,'c'*150],
        'evidence':[{'source':'source-'+'s'*100,'at':'2026-11-08','quote':'q'*200}],
        'contextEvidence':[{'quote':'w'*200}]}
    view={'state':'current','memory':{'learnedProfile':{'memories':[item]}}}
    lines=build_context(snapshot(memory=view),profile()).memory_lines
    rendered='; '.join(lines)
    for value in ['trip to Rome','2026-12-05','2026-12-10','uncertain','conflicting','omitted']:
        assert value in rendered
    assert len(rendered)<=PromptBudget().memory_chars


@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_stop_and_greeting_after_dispatch_grant_cannot_reach_provider(supervised,monkeypatch):
    from onlyfans_runtime.text_delivery import TextAuthority
    w, _, _, _, pilot=supervised
    staged=stage(pilot,1600,'hello')
    info=staged['approval']
    approval.authorize_automatic(w.store,TENANT,ACCOUNT,info['requestId'],info['generation'],expected_version=info['version'])
    original=TextAuthority.consume_dispatch
    reached=[]
    def interleaved(self,tenant,grant,**kw):
        if not reached:
            reached.append(grant)
            for mid,body in [(1601,'stop messaging me'),(1602,'hello again')]:
                w.store.record(TENANT,ACCOUNT,event(mid=mid,body=body))
            w.store.drain(TENANT,ACCOUNT)
        return original(self,tenant,grant,**kw)
    monkeypatch.setattr(TextAuthority,'consume_dispatch',interleaved)
    try:
        pilot.worker.send_approved(TENANT,ACCOUNT,staged['decisionId'])
    except Refused:
        pass
    assert reached, 'the existing dispatch-grant boundary must be reached'
    assert w.fans.posts==[]


def _render_memory_budget(memory, budget=PromptBudget(), history=()):
    ctx=build_context(snapshot(memory={'state':'current','memory':memory},history=list(history)),profile(),budget=budget)
    prompt=SpecPromptProvider(budget).render(ctx).messages[1]['content']
    assert len('; '.join(ctx.memory_lines))<=budget.memory_chars
    return ctx,prompt


def _selected_memory(field,value,**extra):
    return {'field':field,'value':value,'subject':'fan','status':'current',
            'evidence':[{'source':'msg:'+field,'quote':value}],**extra}


def test_multiple_competing_memories_keep_correction_and_report_omissions_in_prompt():
    entries=[_selected_memory('location','Oldtown'),
        _selected_memory('travel','trip to Rome',status='uncertain',event_start='2026-12-05',
            event_end='2026-12-10',alternatives=['date unconfirmed'])]
    entries += [_selected_memory('interest'+str(i),'long relevant detail '+str(i)+' '+('x'*170)) for i in range(6)]
    memory={'facts':{'home_city':{'value':'Perth','source':'operator'}},
        'overrides':{'home_city':{'operation':'set'}},'learnedProfile':{'memories':entries}}
    ctx,prompt=_render_memory_budget(memory)
    for value in ['Perth','trip to Rome','uncertain','2026-12-05','2026-12-10','omitted']:
        assert value in prompt
    assert 'Oldtown' not in prompt
    assert any('interest'+str(i) not in '; '.join(ctx.memory_lines) for i in range(6))


@pytest.mark.parametrize('operation',['clear','set'])
def test_rendered_operator_correction_precedence_survives_old_chat(operation):
    memory={'overrides':{'home_city':{'operation':operation}},
        'learnedProfile':{'memories':[_selected_memory('location','Oldtown')]}}
    if operation=='set':memory['facts']={'home_city':{'value':'Perth','source':'operator'}}
    ctx,prompt=_render_memory_budget(memory,history=[{'id':1,'direction':'in','body':'I live in Oldtown'}])
    assert 'Oldtown' in prompt.split('LATEST:')[0]  # retained history stays untrusted evidence
    assert 'Oldtown' not in '; '.join(ctx.memory_lines)
    if operation=='clear':assert 'Operator-cleared fields (do not reuse older statements): his city' in prompt
    else:assert 'his city: Perth' in prompt and 'current corrections override older chat' in prompt


def test_oversized_entry_does_not_hide_a_later_small_memory():
    entries=[_selected_memory('travel','large '+'x'*400,status='uncertain',event_start='2026-12-05',
        event_end='2026-12-10',last_confirmed='2026-11-08'),_selected_memory('hobbies','tennis',evidence=[{'source':'msg:2'}])]
    ctx,prompt=_render_memory_budget({'learnedProfile':{'memories':entries}},PromptBudget(memory_chars=300))
    assert 'tennis' in prompt and 'omitted' in prompt
    assert 'large' not in '; '.join(ctx.memory_lines)


def test_a_clipped_value_is_not_presented_as_complete_memory():
    value='A long personal preference '+'x'*240
    _,prompt=_render_memory_budget({'learnedProfile':{'memories':[_selected_memory('hobbies',value,evidence=[{'source':'msg:3'}])]}})
    assert value not in prompt and '...' in prompt and 'omitted' in prompt


def test_unavailable_memory_remains_explicit_in_rendered_prompt():
    ctx=build_context(snapshot(memory={'state':'unavailable','reason':'source_unavailable'}),profile())
    prompt=SpecPromptProvider().render(ctx).messages[1]['content']
    assert not ctx.memory_available and 'WHAT SHE KNOWS ABOUT HIM: unavailable' in prompt
