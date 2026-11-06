"""The recorded cheapest-card claim must not contradict offerable prices."""
from moxy_replacement.price_assertions import price_problem


def scene():
    fan = {'id': 2, 'direction': 'in', 'text': 'cheaper?'}
    return {
        'history': [fan], 'pending_fan_messages': [fan], 'fan_state': {},
        'products': {
            'offered': {'category': 'feet', 'price_cents': 2000},
            'lower': {'category': 'feet', 'price_cents': 1500},
            'unavailable': {'category': 'feet', 'price_cents': 500},
        },
        'offer_eligibility': {'offered': 'media_previously_sent_requires_explicit_resend',
                              'lower': 'ok', 'unavailable': 'already_owned_use_existing_access'},
        'card_state': [{'id': 9, 'direction': 'out', 'product_id': 'offered', 'price_cents': 2000}],
        'commercial_state': {'last_offer': {'message_id': 9, 'product_id': 'offered', 'price_cents': 2000},
                             'cheapest_offerable_cents_by_category': {'feet': 1500}},
    }


def reply(text):
    return {'action': 'reply', 'text': text, 'product_id': None,
            'messages': [{'text': text, 'media': False}]}


def test_recorded_cheapest_claim_is_refused_when_eligible_same_kind_is_cheaper():
    assert price_problem(scene(), reply('The one in the chat is already my cheapest.')) == 'price_claim_mismatch'


def test_cheapest_claim_can_use_a_real_cheapest_existing_card():
    snap = scene()
    snap['products']['lower']['price_cents'] = 2500
    snap['commercial_state']['cheapest_offerable_cents_by_category']['feet'] = 2500
    assert price_problem(snap, reply('The one in the chat is already my cheapest.')) is None


def test_unavailable_cheaper_item_does_not_invalidate_the_claim():
    snap = scene()
    snap['offer_eligibility']['lower'] = 'already_owned_use_existing_access'
    snap['commercial_state'].pop('cheapest_offerable_cents_by_category')
    assert price_problem(snap, reply('The one in the chat is already my cheapest.')) is None


def test_withdrawal_of_a_false_claim_is_allowed():
    assert price_problem(scene(), reply('I was wrong that it was my cheapest.')) is None


def test_quote_or_denial_of_the_old_claim_is_allowed():
    assert price_problem(scene(), reply('I said "The one in the chat is already my cheapest." I was wrong.')) is None
    assert price_problem(scene(), reply('It is not my cheapest.')) is None


def test_missing_or_ambiguous_card_does_not_guess_a_target():
    snap = scene()
    snap['card_state'].append({'id': 10, 'direction': 'out', 'product_id': 'lower', 'price_cents': 1500})
    assert price_problem(snap, reply('The one in the chat is already my cheapest.')) is None
    snap['card_state'] = []
    assert price_problem(snap, reply('The one in the chat is already my cheapest.')) is None


def test_claim_for_selected_item_uses_its_own_terms():
    proposal = {'action': 'offer', 'text': 'This is my cheapest.', 'product_id': 'offered',
                'messages': [{'text': 'This is my cheapest.', 'media': True}]}
    assert price_problem(scene(), proposal, {'price_cents': 2000}) == 'price_claim_mismatch'
    assert price_problem(scene(), proposal, {'price_cents': 1000}) is None
