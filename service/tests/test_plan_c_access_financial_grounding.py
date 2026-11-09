import json
import pytest
from conversation_engine.model_output import judge,JudgeContext
from conversation_engine.writer_context import CreatorProfile
@pytest.mark.parametrize('body,state,send,review,expected',[
 ('You have access to the Garden photo.','confirmed','accepted',False,'ok'),
 ('You unlocked the Garden photo.','confirmed','accepted',False,'ok'),
 ('It is unlocked, the Garden photo.','confirmed','accepted',False,'ok'),
 ('You paid for the Garden photo.','confirmed','accepted',False,'invalid'),
 ('You bought the Garden photo.','confirmed','accepted',False,'invalid'),
 ('You own the Garden photo.','confirmed','accepted',False,'invalid'),
 ('You have access to the Garden photo.','revoked','accepted',False,'invalid'),
 ('You have access to the Garden photo.','unconfirmed','accepted',False,'invalid'),
 ('You have access to the Garden photo.','confirmed','unknown',False,'invalid'),
 ('You have access to the Garden photo.','confirmed','accepted',True,'invalid'),
 ('You have access to the Ocean photo.','confirmed','accepted',False,'invalid'),
 ('You paid for the Garden photo and you have access to the Garden photo.','confirmed','accepted',False,'invalid'),
])
def test_access_evidence_never_becomes_financial_or_send_authority(body,state,send,review,expected):
 snap={'current_message':{'id':1,'body':'Can I open the Garden photo?'},'history':[],'sales_mode':'open',
       'offer_grants':[],'verified_purchases':[],
       'offer_reference':{'offers':[],'cardsCoverage':'complete','cards':[
        {'label':'Garden photo','itemId':'7','intentId':'sent-card','messageId':'700','sendState':send,
         'accessState':state,'financialState':'not_established','financialReview':review}]}}
 verdict=judge(json.dumps({'body':body}),JudgeContext(snap,CreatorProfile(name='Fixture')))
 assert verdict.outcome==expected,verdict.reasons
