import json
import pytest
from conversation_engine.model_output import judge,JudgeContext
from conversation_engine.writer_context import CreatorProfile
@pytest.mark.parametrize('text,action,grants,currency,expected',[
 ('The photo is USD 3.00.','reply',True,'USD','ok'),
 ('The photo is $3.00.','reply',True,'USD','ok'),
 ('The photo is 3.00 CAD.','reply',True,'USD','invalid'),
 ('The photo is $3.00 EUR.','reply',True,'USD','invalid'),
 ('The photo is AUD 3.00.','reply',True,'USD','invalid'),
 ('The photo is CAD 3.00.','reply',True,'USD','invalid'),
 ('The photo is 3.00 EUR.','reply',True,'USD','invalid'),
 ('The photo is £3.00.','reply',True,'USD','invalid'),
 ('The photo is €3.00.','reply',True,'USD','invalid'),
 ('The photo is three dollars.','reply',True,'USD','ok'),
 ('The photo is USD 3.00. My bag weighs three pounds.','reply',True,'USD','ok'),
 ('The photo is USD 4.00.','reply',True,'USD','invalid'),
 ('The photo is EUR 3.00.','reply',True,'USD','invalid'),
 ('The photo is GBP 3.00.','reply',True,'USD','invalid'),
 ('The photo is USD 3.00.','offer',True,'USD','invalid'),
 ('The photo is USD 3.00.','reply',False,'USD','invalid'),
 ('The photo is USD 3.00.','reply',True,'EUR','invalid'),
])
def test_informational_price_does_not_change_currency_terms_or_send_authority(text,action,grants,currency,expected):
 grant={'grantId':'grant-7','itemId':'7','label':'Garden photo','priceCents':300,'currency':currency,
        'media':{'pictures':1,'videos':0,'total':1}}
 snap={'current_message':{'id':1,'body':'How much is the photo?'},'history':[],'sales_mode':'open',
       'offer_reference':{'offers':[]},'offer_grants':[grant] if grants else []}
 profile=CreatorProfile(name='Fixture',limits=('No meetups',),offers_permitted=True,approved_item_ids=('7',))
 ctx=JudgeContext(snap,profile,allowed_grant_ids=frozenset({'grant-7'}))
 proposal={'body':text,'action':action}
 if action=='offer':proposal['offer_grant_id']='grant-7'
 verdict=judge(json.dumps(proposal),ctx)
 assert verdict.outcome==expected,verdict.reasons
