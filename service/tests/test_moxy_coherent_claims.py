"""Adverse and legitimate wording through the actual combined decision validator."""
import json
from copy import deepcopy
import pytest
from moxy_replacement.writer import parse
from moxy_replacement.runtime import validate_decision
from moxy_replacement.configuration import ActionBlocked
from moxy_replacement.pilot import route_specialist
from test_moxy_integrated_pg import decision
from test_moxy_foundation_authority_regressions import authority_scene, parsed_offer


@pytest.mark.parametrize('text', [
    'I just sent it to you, check your DMs.',
    'sending it again now', "I'll send you the video now.",
    'ill make you a custom video for free', 'I can give you 20% off.',
    "I'll refund your payment.", 'I have already issued your refund.',
])
def test_unsupported_commitment_is_a_correctable_claim_not_automatic_handover(text):
    settings, snap, _ = authority_scene('Can you help with that card?')
    settings['operational']['assigned_operator'] = 'fixture-operator'
    settings['pilot'] = {'fan_ids': [101], 'product_ids': ['first','second'], 'supervisor': 'fixture-operator'}
    proposed = parse(json.dumps(decision(text=text, permission=None, flags=[])))
    routed = route_specialist(settings, proposed)
    assert routed == proposed, 'A bad draft is not authority for a persistent human takeover'
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, routed, planning=True)


@pytest.mark.parametrize('fan,text', [
    ('My dinner cost $20.', '$20 for dinner sounds reasonable.'),
    ('My rent is $800.', 'Your rent is $800.'),
    ('Do you have trip photos?', "I don't have any pictures from that trip."),
    ('Can you help with that card?', 'I can check whether a refund is possible.'),
    ('Can you help with that card?', "I haven't sent another card."),
    ('Can you help with that card?', "I won't promise you a discount."),
])
def test_ordinary_conversation_and_noncommitments_remain_valid(fan,text):
    settings, snap, _ = authority_scene(fan)
    assert validate_decision(settings,snap,parse(json.dumps(decision(text=text))),planning=True) is None


@pytest.mark.parametrize('text', ['Here is a 10 minute video.', 'Here are 20 photos.',
                                  'Here is a video with 3 photos.'])
def test_selected_card_claim_cannot_borrow_truth_from_the_fan_request(text):
    settings, snap, _ = authority_scene()
    with pytest.raises(ActionBlocked):
        validate_decision(settings,snap,parsed_offer(text),planning=True)


@pytest.mark.parametrize('text', ['Card 77 is a video.', 'Card 77 has 20 photos.',
                                  'Card 77 is 10 minutes long.', 'Card 77 is 20 bucks.',
                                  'Card 77 is 20$.', 'Card 77 is half off.'])
def test_existing_card_claims_use_its_recorded_product_and_price(text):
    settings, snap, _ = authority_scene('What is in card 77?')
    snap['card_state']=[{'id':77,'product_id':'first','price_cents':7500,'paid':False}]
    with pytest.raises(ActionBlocked):
        validate_decision(settings,snap,parse(json.dumps(decision(text=text))),planning=True)


def test_ordinal_existing_card_price_is_answerable_without_another_offer():
    settings, snap, _ = authority_scene('How much was that first one?')
    snap['card_state']=[{'id':77,'product_id':'first','price_cents':7500,'paid':False},
                        {'id':78,'product_id':'second','price_cents':8500,'paid':False}]
    result=parse(json.dumps(decision(text='The first one is $75.')))
    assert validate_decision(settings,snap,result,planning=True) is None
    assert result['product_id'] is None and result['action']=='reply'


@pytest.mark.parametrize('permission,flags', [('sexting',[]),(None,['complaint'])])
def test_eligible_offer_annotations_do_not_replace_action(permission,flags):
    settings,snap,_=authority_scene(spender=True,vip=True)
    settings['operational']['assigned_operator']='fixture-operator'
    settings['pilot']={'fan_ids':[101],'product_ids':['first','second'],'supervisor':'fixture-operator'}
    result=parsed_offer();result.update(permission=permission,flags=flags)
    routed=route_specialist(settings,result)
    assert routed==result
    assert validate_decision(settings,snap,routed,planning=True)['price_cents']==7500


def test_price_and_noncommercial_amount_can_share_a_reply():
    settings,snap,_=authority_scene('My dinner was $20. Please send the first photo.')
    result=parsed_offer('Your dinner was $20. This photo is $75.', request='Please send the first photo.')
    assert validate_decision(settings,snap,result,planning=True)['price_cents']==7500


def test_unrelated_chat_after_invitation_cannot_become_paid_acceptance():
    settings,snap,authority=authority_scene('okay')
    snap['invitation']={'product_id':'first','sequence':None,'fingerprint':authority.fingerprint}
    snap['history']=[{'id':998,'direction':'out','text':'Want the first photo?'},
                     {'id':999,'direction':'in','text':'How was your afternoon?'},
                     {'id':1000,'direction':'out','text':'How was yours?'},snap['history'][0]]
    with pytest.raises(ActionBlocked):
        validate_decision(settings,snap,parsed_offer(kind='accept',request='okay'),planning=True)
