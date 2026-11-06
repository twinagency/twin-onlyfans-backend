"""'the one i sent' refers to the existing card; it never proves a new delivery."""
from moxy_replacement.grounding import commitment_problem

def snap(cards,latest='hey you there?'):
    return {'card_state':cards,'history':[],'event':{'payload':{}},
            'pending_fan_messages':[{'id':9,'direction':'in','text':latest}]}
CARD={'id':77,'product_id':'p1','price_cents':7500,'currency':'USD'}
REPLY={'action':'reply','product_id':None}

def test_back_reference_to_the_single_existing_card_is_not_a_new_claim():
    assert commitment_problem(snap([CARD]),REPLY,['the one i sent is still locked for you']) is None

def test_no_card_at_all_still_requires_a_receipt():
    assert commitment_problem(snap([]),REPLY,['the one i sent is still locked for you']) == 'sent_claim_without_receipt'

def test_two_different_cards_stay_ambiguous():
    other={'id':78,'product_id':'p2','price_cents':9000,'currency':'USD'}
    assert commitment_problem(snap([CARD,other]),REPLY,['the one i sent is still locked for you']) == 'sent_claim_without_receipt'

def test_a_just_sent_claim_still_needs_a_receipt_from_this_plan():
    assert commitment_problem(snap([CARD]),REPLY,['the one i just sent is still locked for you']) == 'sent_claim_without_receipt'
