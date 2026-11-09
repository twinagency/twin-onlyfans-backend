"""Actual provider HTML representation stays within the existing stop barrier."""
import pytest
from sqlalchemy.orm import Session
from conversation_engine.controls import explicit_stop
from conversation_engine.pilot_ingress import preserve_contact_stops
from conversation_engine import pilot_scope, approval
from db.models import OfConversationState
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import TextAuthority
from test_plan_c_supervised_pg import supervised, stage
from test_plan_c_draft_app_pg import configured_draft
from test_engine_real_worker_pg import world, inbound
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT, FAN


@pytest.mark.parametrize('body', ['stop messaging me', '<p>stop messaging me</p>',
    '<p>please <strong>stop messaging me</strong>.</p>', '<p>Don&#39;t contact me again.</p>'])
def test_narrow_explicit_stop_accepts_provider_visible_text(body):
    assert explicit_stop(body)


@pytest.mark.parametrize('body', ['hello', '<p>hello</p>', '<p>please stop selling to me</p>',
    '<p>he said stop messaging me</p>', 'I love <3'])
def test_unrelated_text_is_not_a_contact_stop(body):
    assert not explicit_stop(body)


@pytest.mark.parametrize('body', ['<p>stop messaging me', '<script>stop messaging me</script>',
                                  '<p><img src=x>stop messaging me</p>', '<p>' + 'x'*4001 + '</p>'])
def test_unsupported_markup_needs_review(body):
    with pytest.raises(Refused, match='contact_stop_format_unclassified'):
        explicit_stop(body)


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('source', ['signed_webhook','recovered_history'])
def test_html_stop_then_greeting_prevents_reply(supervised, source):
    w, _, _, seen, pilot = supervised
    stage(pilot, 4500, 'hello')
    before = len(seen)
    if source == 'signed_webhook':
        stage(pilot, 4501, '<p>stop messaging me</p>')
        try:
            stage(pilot, 4502, '<p>hello again</p>')
        except Refused:
            pass
    else:
        for mid,body in [(4501,'<p>stop messaging me</p>'),(4502,'<p>hello again</p>')]:
            assert pilot.worker.intake.ingest_history_item(TENANT, ACCOUNT, FAN, inbound(mid,body)['payload']) == 'accepted'
        w.store.drain(TENANT, ACCOUNT)
        preserve_contact_stops(w.store, TENANT, ACCOUNT)
        try:
            pilot.worker.turn(TENANT, ACCOUNT, FAN, 4502)
        except Refused:
            pass
    with Session(w.database[1]) as s:
        assert s.get(OfConversationState,(ACCOUNT,FAN)).mode == 'no_contact'
    assert len(seen) == before
    assert w.fans.posts == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_html_stop_after_dispatch_grant_fences_wire(supervised, monkeypatch):
    w, _, _, _, pilot = supervised
    staged = stage(pilot, 4600, 'hello')
    info = staged['approval']
    approval.authorize_automatic(w.store,TENANT,ACCOUNT,info['requestId'],info['generation'],expected_version=info['version'])
    original = TextAuthority.consume_dispatch
    reached = []
    def interleaved(self,tenant,grant,**kw):
        if not reached:
            reached.append(True)
            for mid,body in [(4601,'<p>stop messaging me</p>'),(4602,'<p>hello again</p>')]:
                assert pilot.worker.intake.ingest_history_item(TENANT,ACCOUNT,FAN,inbound(mid,body)['payload']) == 'accepted'
            w.store.drain(TENANT,ACCOUNT)
            with Session(w.database[1]) as s,s.begin():
                runtime = w.store._runtime(s,TENANT,ACCOUNT)
                assert pilot_scope.delivery_block(s,runtime,FAN) == 'pilot_contact_review_pending'
        return original(self,tenant,grant,**kw)
    monkeypatch.setattr(TextAuthority,'consume_dispatch',interleaved)
    try:
        pilot.worker.send_approved(TENANT,ACCOUNT,staged['decisionId'])
    except Refused:
        pass
    assert reached and not w.fans.posts


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_unsupported_html_remains_unreviewed_and_blocks_send(supervised):
    w,_,_,seen,pilot=supervised
    stage(pilot,4700,'hello')
    before=len(seen)
    try:
        stage(pilot,4701,'<p><img src=x>stop messaging me</p>')
    except Refused:
        pass
    with Session(w.database[1]) as s,s.begin():
        runtime=w.store._runtime(s,TENANT,ACCOUNT)
        assert pilot_scope.delivery_block(s,runtime,FAN) == 'pilot_contact_review_pending'
    assert len(seen)==before and not w.fans.posts


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_previously_reviewed_html_stop_can_use_audited_existing_control(supervised, monkeypatch):
    from conversation_engine import pilot_ingress
    from db.onlyfans_models import OfControlAudit
    from test_plan_c_scope_pg import ACTOR
    w,_,_,_,pilot=supervised
    with monkeypatch.context() as m:
        from conversation_engine import controls
        m.setattr(pilot_ingress, 'explicit_stop', lambda body: False)
        m.setattr(controls, 'explicit_stop', lambda body: False)
        stage(pilot,4801,'<p>stop messaging me</p>')
    # Immutable old review is not silently rewritten by the new classifier.
    assert preserve_contact_stops(w.store,TENANT,ACCOUNT)['reviewed'] == 0
    with Session(w.database[1]) as s:
        state=s.get(OfConversationState,(ACCOUNT,FAN))
        assert state.mode=='automated'
        revision=state.revision
    w.store.control(TENANT,ACCOUNT,FAN,mode='no_contact',expected_revision=revision,
        command_key='html-stop-reviewed-4801',actor=ACTOR,
        reason='Previously reviewed signed message 4801 requested no contact',action='mark_no_contact')
    with Session(w.database[1]) as s:
        assert s.get(OfConversationState,(ACCOUNT,FAN)).mode=='no_contact'
        audit=s.get(OfControlAudit,(ACCOUNT,'html-stop-reviewed-4801'))
        assert audit is not None and '4801' in audit.reason
    assert w.fans.posts == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_unclassifiable_html_holds_only_affected_fan(supervised):
    from test_engine_real_worker_pg import FAN2
    w,_,_,_,pilot=supervised
    stage(pilot,4900,'hello',fan=FAN)
    stage(pilot,4901,'<p><img src=x>stop messaging me</p>',fan=FAN)
    with Session(w.database[1]) as s,s.begin():
        runtime=w.store._runtime(s,TENANT,ACCOUNT)
        assert pilot_scope.delivery_block(s,runtime,FAN)=='pilot_contact_review_pending'
        assert pilot_scope.delivery_block(s,runtime,FAN2) is None
    other=stage(pilot,4902,'hello there',fan=FAN2)
    assert other['outcome']=='awaiting_approval',other
    pilot.tick()
    assert len(w.fans.posts)==1
    assert w.fans.posts[0]['path'].endswith('/chats/'+str(FAN2)+'/messages')
