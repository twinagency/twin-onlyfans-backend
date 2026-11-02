"""Real campaign audience/receipt integration with offline Moxy writers."""
import json
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database, event
from test_moxy_integrated_pg import setup, model, decision, status
from test_onlyfans_campaigns_pg import create, key
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from db.onlyfans_moxy_models import MoxyEvent
from db.onlyfans_campaign_models import OfCampaignRecipient
from db.onlyfans_delivery_models import OfTextIntent
from onlyfans_runtime.campaigns import CampaignService
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from onlyfans_runtime.store import Refused


def campaign_event(engine):
    with Session(engine) as s:
        return s.scalar(select(MoxyEvent).where(MoxyEvent.kind=='campaign')).event_id


def test_campaign_writer_receipt_and_recipient_share_one_intent(database, monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    campaigns=CampaignService(store);created=create(campaigns)
    campaigns.queue_due(TENANT,ACCOUNT);campaigns.queue_due(TENANT,ACCOUNT)
    identity=campaign_event(engine)
    m=model(decision(text='How has your training been?'))
    service.prepare(TENANT,ACCOUNT,identity,m)
    assert 'An operator-authored campaign message.' in str(m.calls)
    intent=service.queue(TENANT,ACCOUNT,identity)
    with Session(engine) as s:
        recipient=s.get(OfCampaignRecipient,(ACCOUNT,created['campaignId'],101))
        assert recipient.intent_id==intent and recipient.status=='queued'
        assert s.scalar(select(func.count()).select_from(MoxyEvent))==1
    wire=SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT,ACCOUNT,intent,wire)=='accepted'
    detail=campaigns.detail(TENANT,ACCOUNT,created['campaignId'])
    assert detail['campaign']['counts']['accepted']==1
    assert detail['campaign']['status']=='completed'
    assert wire.calls[0].body=='How has your training been?'
    campaigns.queue_due(TENANT,ACCOUNT)
    assert len(wire.calls)==1
    store.close()


@pytest.mark.parametrize('stage',['generation','ready','queued'])
def test_campaign_cancel_fences_each_stage(database,monkeypatch,stage):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    campaigns=CampaignService(store);created=create(campaigns)
    campaigns.queue_due(TENANT,ACCOUNT);identity=campaign_event(engine)
    def cancel():
        campaigns.action(TENANT,ACCOUNT,created['campaignId'],command_key=key('cancel'),
            actor='fixture-operator',expected_revision=1,action='cancel')
    service.prepare(TENANT,ACCOUNT,identity,model(decision(),on_generate=cancel if stage=='generation' else None))
    wire=SyntheticTextTransport(ACCOUNT)
    if stage=='queued':
        intent=service.queue(TENANT,ACCOUNT,identity);cancel()
        assert authority.dispatch(TENANT,ACCOUNT,intent,wire)=='cancelled'
    else:
        if stage=='ready':cancel()
        with pytest.raises(Refused):service.queue(TENANT,ACCOUNT,identity)
    assert wire.calls==[]
    assert campaigns.detail(TENANT,ACCOUNT,created['campaignId'])['campaign']['counts']['cancelled']==1
    store.close()


def test_no_send_decision_finishes_recipient_with_visible_reason(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    campaigns=CampaignService(store);created=create(campaigns)
    campaigns.queue_due(TENANT,ACCOUNT)
    assert service.step(TENANT,ACCOUNT,model(decision('none','')))==0
    detail=campaigns.detail(TENANT,ACCOUNT,created['campaignId'])
    assert detail['campaign']['counts']['skipped']==1
    assert detail['recipients'][0]['reason']=='moxy_no_send_decision'
    with Session(engine) as s:assert s.scalar(select(func.count()).select_from(OfTextIntent))==0
    store.close()


def test_new_chat_invalidates_campaign_draft_before_send(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    campaigns=CampaignService(store);create(campaigns);campaigns.queue_due(TENANT,ACCOUNT)
    identity=campaign_event(engine);service.prepare(TENANT,ACCOUNT,identity,model(decision()))
    intent=service.queue(TENANT,ACCOUNT,identity)
    store.record(TENANT,ACCOUNT,event(mid=2,body='Please wait, I have a question.'));store.drain(TENANT,ACCOUNT)
    wire=SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT,ACCOUNT,intent,wire)=='cancelled'
    assert wire.calls==[]
    store.close()


def test_campaign_unknown_receipt_never_counts_as_completed_or_repeats(database,monkeypatch):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    campaigns=CampaignService(store);created=create(campaigns);campaigns.queue_due(TENANT,ACCOUNT)
    identity=campaign_event(engine);service.prepare(TENANT,ACCOUNT,identity,model(decision()))
    intent=service.queue(TENANT,ACCOUNT,identity)
    wire=SyntheticTextTransport(ACCOUNT,outcome='accepted_lost_response')
    assert authority.dispatch(TENANT,ACCOUNT,intent,wire)=='unknown'
    campaigns.queue_due(TENANT,ACCOUNT)
    detail=campaigns.detail(TENANT,ACCOUNT,created['campaignId'])
    assert detail['campaign']['counts']['unknown']==1 and detail['campaign']['status']!='completed'
    assert service.step(TENANT,ACCOUNT,model(decision()))==0
    assert len(wire.calls)==1
    store.close()
