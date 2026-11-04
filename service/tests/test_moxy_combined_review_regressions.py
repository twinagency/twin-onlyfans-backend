from moxy_replacement.grounding import current_record_problem
from moxy_replacement.product_grounding import product_problem

def test_scoped_item_format_is_not_creator_wide_ban():
 snap={'history':[{'id':1,'direction':'in','text':'Repeat price and is it a photo or video?'}],'products':{},'inventory':{},'instructions':{}}
 assert product_problem(snap,{'action':'reply','product_id':None},["that one's $20 and it's a single photo, babe. no video on that one."]) is None

def test_recorded_cents_and_old_card_access_failures_are_refused():
 assert current_record_problem({'products':{'x':{'price_cents':3000}}},['3000 is my cheapest right now'])=='unsupported_price_units'
 assert current_record_problem({'products':{'x':{'price_cents':3000}}},['$30 is my cheapest right now']) is None
 snap={'card_state':[{'id':123,'price_cents':7500,'paid':False}],'commercial_state':{'current_input':{'text':'message 123'}}}
 assert current_record_problem(snap,["nothing to open and nothing to unlock"])=='unsupported_card_access_denial'
 assert current_record_problem(snap,['That card is still locked and waiting.']) is None
 assert current_record_problem(snap,["it's not something you buy"])=='unsupported_card_access_denial'


def test_recorded_manual_sex_description_requires_permission_but_warmth_does_not():
 from moxy_replacement.grounding import explicit_sexting
 snap={'history':[{'direction':'in','text':'Would you hold my dick?'}]}
 assert explicit_sexting(snap,['my hand wrapped around it, feeling you get hard for me'])
 assert not explicit_sexting(snap,["I'd pull you close and kiss you slowly."])
 assert not explicit_sexting({'history':[{'direction':'in','text':'How was your workout?'}]},['My hands worked hard today.'])
 assert not explicit_sexting({'history':[{'id':1,'direction':'in','text':'Would you hold my dick?'},{'id':2,'direction':'out','text':'Let us talk about something else.'},{'id':3,'direction':'in','text':'How was your workout?'}]},['My hands worked hard today.'])


def test_negative_card_feature_requires_explicit_asset_evidence():
 snap={'card_state':[{'id':123,'product_id':'old','price_cents':7500,'paid':False}],'commercial_state':{'current_input':{'text':'message 123'}},'products':{'old':{'assets':[{'facts':['A mirror selfie with a green garment.']}]}}}
 assert current_record_problem(snap,['no toys, just the photo'])=='unsupported_asset_absence'
 assert current_record_problem(snap,['A mirror selfie with a green garment.']) is None
 snap['products']['old']['assets'][0]['facts'].append('No toys are visible.')
 assert current_record_problem(snap,['no toys, just the photo']) is None
