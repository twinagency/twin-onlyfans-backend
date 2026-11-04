"""Durable card summaries and source-grounded relevance; no model or transport."""
import pytest
from copy import deepcopy
from moxy_replacement.state import commercial_state
from moxy_replacement.product_grounding import product_problem


def snap():
 return {'history':[{'id':500,'direction':'in','text':'Is that old card worth it?','at':'2026-11-05T00:00:00'}],
  'card_state':[{'id':10,'direction':'out','text':'Old locked photo','at':'2026-09-01T00:00:00','price_cents':7500,'paid':False,'product_id':'old'},
                {'id':20,'direction':'out','text':'Paid video','at':'2026-10-01T00:00:00','price_cents':6000,'paid':True,'product_id':'paid'}],
  'fan_state':{'purchased_products':['paid'],'owned_assets':[200]}}


def test_unpaid_card_outside_rolling_window_stays_in_summary():
 s=snap();s['history']=[{'id':i,'direction':'in','text':'ordinary chat'} for i in range(100,200)]
 cs=commercial_state(s)
 assert cs['sent_unpaid_cards']==[{'message_id':10,'product_id':'old','price_cents':7500,'sent_at':'2026-09-01T00:00:00','card_kind':'priced_card','price_display':'$75.00'}]
 assert cs['last_offer']['message_id']==20 and cs['last_offer']['paid'] is True
 assert cs['verified_purchases']==['paid'] and cs['owned_assets']==[200]
 assert 'old' not in cs['verified_purchases']


def test_durable_card_record_beats_stale_or_duplicate_rolling_card():
 s=snap();s['history'].append({**s['card_state'][0],'paid':True})
 assert commercial_state(s)['sent_unpaid_cards'][0]['message_id']==10
 s['card_state']=[]
 assert 'last_offer' not in commercial_state(s) and 'sent_unpaid_cards' not in commercial_state(s)
 del s['card_state']
 assert commercial_state(s)['last_offer']['paid'] is True


def test_card_record_order_is_chronological_and_identity_deduplicated():
 s=snap();s['card_state']=[s['card_state'][1],s['card_state'][0],s['card_state'][0]]
 cs=commercial_state(s)
 assert cs['last_offer']['message_id']==20
 assert len(cs['sent_unpaid_cards'])==1


def product_snapshot(request='show me your tits'):
 return {'history':[{'id':1,'direction':'in','text':request}],
  'products':{'booty':{'category':'covered_booty','assets':[{'facts':['Woman in leggings showing her covered buttocks while twerking.']}]}},
  'inventory':{'booty':{'files':[{'kind':'photo'}]}},
  'instructions':{'rules':[{'key':'prohibited_categories','requires':{},'value':['kitty','bootyhole','xxx']}]}}


@pytest.mark.parametrize('text',[
 "here, this is the closest I've got to bare tits", "Here is your breast photo", "this twerk one is what you asked for"])
def test_booty_card_does_not_satisfy_breast_request(text):
 assert product_problem(product_snapshot(),{'action':'offer','product_id':'booty'},[text])=='product_request_mismatch'


def test_different_alternative_is_named_honestly():
 s=product_snapshot()
 assert product_problem(s,{'action':'alternative','product_id':'booty'},["I can't send a breast photo right now. This is a covered booty photo instead."]) is None
 # Saying only alternative/closest cannot substitute for identifying the actual content.
 assert product_problem(s,{'action':'alternative','product_id':'booty'},["Here is something different, the closest I've got to bare tits."])=='product_request_mismatch'


def test_generic_photo_request_and_body_matching_request_stay_allowed():
 for text in ['Can you send me a photo?','send me an ass pic']:
  assert product_problem(product_snapshot(text),{'action':'offer','product_id':'booty'},['Here is that covered booty photo.']) is None


def test_video_request_requires_video_or_an_honest_different_format():
 s=product_snapshot('send me an ass video')
 assert product_problem(s,{'action':'offer','product_id':'booty'},['Here you go.'])=='product_request_mismatch'
 assert product_problem(s,{'action':'alternative','product_id':'booty'},['This is a covered booty photo instead.']) is None


@pytest.mark.parametrize('line',["I don't do bare tits, that's not on my menu.","all my ass stuff is photos",'no ass videos on the menu',"I never make breast content"])
def test_pilot_absence_is_not_a_creator_wide_boundary(line):
 assert product_problem(product_snapshot(),{'action':'reply','product_id':None},[line])=='unsupported_catalogue_prohibition'


def test_actual_creator_restrictions_and_scoped_availability_remain():
 s=product_snapshot()
 for line in ["I don't send kitty or bootyhole content.","I don't do XXX content.","I can't send an ass video from this selection right now.","I don't share my personal phone number.","I don't have an exact date for when we started talking."]:
  assert product_problem(s,{'action':'reply','product_id':None},[line]) is None,line


def test_acceptance_cannot_relabel_the_unrelated_proposed_item():
 s=product_snapshot();s['history'] += [{'id':2,'direction':'out','text':"I've got something close if you're in the mood"},{'id':3,'direction':'in','text':'ok'},{'id':4,'direction':'in','text':'sure'}]
 assert product_problem(s,{'action':'offer','product_id':'booty'},["This is the closest I've got to bare tits."])=='product_request_mismatch'


def test_new_personal_chat_does_not_inherit_an_old_breast_request():
 s=product_snapshot();s['history'] += [{'id':2,'direction':'out','text':'An earlier reply'},{'id':3,'direction':'in','text':'How was your day?'}]
 assert product_problem(s,{'action':'invite','product_id':'booty'},['Want a covered booty photo instead?']) is None


def test_acceptance_of_an_honestly_named_retained_alternative_stays_valid():
 s=product_snapshot();s['invitation']={'product_id':'booty','sequence':None}
 s['history'] += [{'id':2,'direction':'out','text':'I can offer a covered booty photo instead.'},{'id':3,'direction':'in','text':'sure'}]
 assert product_problem(s,{'action':'offer','product_id':'booty'},['Here you go.']) is None


def test_source_conditional_nipple_rule_is_preserved_without_blanket_breast_ban():
 from moxy_replacement.configuration import load
 s=product_snapshot();s['instructions']=load('amelia').public_context()
 assert product_problem(s,{'action':'reply','product_id':None},["I don't offer nipple content unless you're a high-spending VIP."]) is None
 assert product_problem(s,{'action':'reply','product_id':None},["I don't do bare tits."])=='unsupported_catalogue_prohibition'


def test_retraction_of_invented_boundary_is_allowed_but_new_comma_claim_is_checked():
 s=product_snapshot();d={'action':'reply','product_id':None}
 assert product_problem(s,d,["I was wrong when I said I don't do breast content."]) is None
 assert product_problem(s,d,["I was wrong when I said I don't do breast content, but I never offer ass videos."])=='unsupported_catalogue_prohibition'
