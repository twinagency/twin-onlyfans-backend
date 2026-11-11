"""Step 3: one approved-terms contract for the prompt, the output judge, the human-edit check and the claims.

Pure (no database). Counterexamples c, f: wording follows the card's approved facts, legacy bytes do not move.
"""
import hashlib
import json

import pytest

from conversation_engine.model_output import JudgeContext, edit_problems, judge
from conversation_engine.writer_context import CreatorProfile, build_context

IDENT = 'This chat is written by an AI assistant that works for her team.'
WRITER = CreatorProfile.from_facts('Riley', {'age': '27', 'limits': 'no meetups'}, role_statement=IDENT,
                                   offers_permitted=True, approved_item_ids=('5',))
GRANT = '22222222-2222-2222-2222-222222222222'


def card(variant='standard', kind='single_photo', std=1200, disc=900, pics=1, vids=0, previews=0):
    total = pics + vids
    price = disc if variant == 'discount' else std
    return {'grantId': GRANT, 'itemId': '5', 'label': 'beach set', 'description': 'a day at the beach',
            'priceCents': price, 'currency': 'USD', 'mediaCount': total + previews,
            'media': {'total': total + previews, 'pictures': pics, 'videos': vids},
            'kind': kind, 'variant': variant, 'standardCents': std, 'discountCents': disc if disc else None,
            'discountApproved': variant == 'discount', 'isBundle': kind.startswith('bundle'), 'productCount': 1}


def snap(message='hey', cards=(), **over):
    base = {'account_id': 'a', 'fan_id': 1, 'sales_mode': 'open', 'sales_scope': [], 'terms_mode': 'commercial.v1',
            'current_message': {'id': 9, 'body': message}, 'history': [],
            'memory': {'state': 'current', 'memory': {'facts': {}}},
            'offer_reference': {'state': 'none', 'offers': []}, 'catalogue_search': {'items': []},
            'offer_grants': list(cards), 'verified_purchases': [], 'price_information': []}
    base.update(over)
    return base


def ctx(message='hey', cards=(), **over):
    s = snap(message, cards, **over)
    return JudgeContext(snapshot=s, profile=WRITER, allowed_grant_ids=frozenset(c['grantId'] for c in cards),
                        allowed_scope_ids=frozenset())


def offer(body):
    return json.dumps({'body': body, 'action': 'offer', 'offer_grant_id': GRANT, 'pitch': True})


def reply(body, **extra):
    return json.dumps({'body': body, **extra})


def verdict(raw, c):
    v = judge(raw, c)
    return v.outcome, ' | '.join(v.reasons)


# ---- discount / bundle wording is gated by the card's approved facts, never free ----------------------------------

@pytest.mark.parametrize('body', ['i can do a discount for you', 'want a cheaper one instead', 'a lower price just for you',
                                   'take the bundle, half price', '20% off if you take it now'])
def test_standard_card_cannot_mention_any_discount_cheaper_or_bundle(body):
    assert verdict(offer(body), ctx('is it cheaper?', [card('standard')]))[0] == 'invalid'


def test_discount_wording_needs_the_discount_variant_and_still_no_percent_or_half_price():
    discount = [card('discount')]
    assert verdict(offer('since you asked, i can do this one a little cheaper for you'), ctx('can you do cheaper?', discount))[0] == 'ok'
    for banned in ('20% off just for you', 'half price for you', 'knock a few dollars off', 'pay me later is fine',
                   'a payment plan works too'):
        assert verdict(offer(banned), ctx('can you do cheaper?', discount))[0] == 'invalid', banned


def test_bundle_wording_needs_a_bundle_card_and_counts_are_media_not_products():
    bundle = [card('standard', kind='bundle_photo', pics=6)]
    assert verdict(offer('this bundle has 6 pics from the shoot'), ctx('what do you have', bundle))[0] == 'ok'
    assert verdict(offer('this bundle has 6 pics from the shoot'), ctx('what do you have', [card('standard', pics=1)]))[0] == 'invalid'
    for wrong in ('you get 6 products in this one', 'it is 6 bundles', 'two bundles for you'):
        outcome, why = verdict(offer(wrong), ctx('what do you have', bundle))
        assert outcome == 'invalid', wrong
    assert verdict(offer('one bundle, 6 pics, it is a good one'), ctx('what do you have', bundle))[0] == 'ok'


def test_human_edit_check_uses_the_same_gate_as_the_model_check():
    standard = ctx('hey', [card('standard')])
    discount = ctx('cheaper?', [card('discount')])
    assert edit_problems(['i can do it cheaper for you'], standard) != []
    assert edit_problems(['i can do it cheaper for you'], discount) == []
    assert edit_problems(['20% off for you'], discount) != []
    assert edit_problems(['half price for you'], discount) != []


def test_prices_follow_the_card_variant_and_invented_amounts_fail():
    assert verdict(reply('it is $12', price_claims=[1200]), ctx('how much is it?', price_information=[
        {'itemId': '5', 'label': 'beach set', 'priceCents': 1200, 'currency': 'USD'}]))[0] == 'ok'
    assert verdict(reply('it is $10', price_claims=[1000]), ctx('how much is it?', price_information=[
        {'itemId': '5', 'label': 'beach set', 'priceCents': 1200, 'currency': 'USD'}]))[0] == 'invalid'
    # a card's text never states an amount (the application renders it); replies may state only approved amounts
    assert verdict(offer('it is $9 for you'), ctx('cheaper?', [card('standard')]))[0] == 'invalid'
    assert verdict(reply('it is $12', price_claims=[1200]), ctx('how much?', [card('standard')]))[0] == 'ok'
    assert verdict(reply('it is $9', price_claims=[900]), ctx('how much?', [card('standard')]))[0] == 'invalid'
    both = ctx('cheaper?', [card('discount')])
    assert verdict(reply('normally $12, for you $9', price_claims=[1200, 900]), both)[0] == 'ok'
    assert verdict(reply('normally $12, for you $8', price_claims=[1200, 800]), both)[0] == 'invalid'


# ---- the prompt follows the same facts, legacy rows keep their exact bytes ------------------------------------------

def test_commercial_prompt_names_the_variant_and_legacy_prompt_bytes_do_not_move():
    from conversation_engine import prompt_provider_a_original as p
    assert hashlib.sha256(p.RULES.encode()).hexdigest() == LEGACY_RULES_SHA
    assert hashlib.sha256(p.RULES_TEXT_ONLY.encode()).hexdigest() == LEGACY_TEXT_SHA
    from conversation_engine.prompt_registry import get_prompt_provider
    provider = get_prompt_provider('a-original')
    standard = provider.render(build_context(snap('show me', [card('standard')]), WRITER)).messages
    discount = provider.render(build_context(snap('cheaper?', [card('discount')]), WRITER)).messages
    legacy_snap = snap('show me', [card('standard')])
    legacy_snap.pop('terms_mode')
    old = provider.render(build_context(legacy_snap, WRITER)).messages
    assert 'DISCOUNT APPROVED' in discount[1]['content'] and 'DISCOUNT APPROVED' not in standard[1]['content']
    assert 'No discounts, cheaper options' in old[0]['content'] and 'No discounts, cheaper options' not in standard[0]['content']
    assert 'never invent a discount, percentage, bundle or cheaper option' in standard[0]['content']
    assert 'DISCOUNT APPROVED' not in old[1]['content']


def test_legacy_snapshot_without_terms_mode_keeps_the_old_restrictions():
    legacy = ctx('hey', [card('standard')], terms_mode='')
    for body in ('i can do a discount for you', 'take the bundle, half price', 'want a cheaper one instead'):
        assert verdict(offer(body), legacy)[0] == 'invalid', body


LEGACY_RULES_SHA = '0bc9cac56af93f8f4db2fa34dd2ebe5d93233feb34cfbdb091423a94c914ebfb'  # sha256 of RULES at be7108d
LEGACY_TEXT_SHA = '69b34a2833fd02aaf6f7b485aa9ddd33e8c42306b7e9a3fbd4415826f4aaf9a5'  # sha256 of RULES_TEXT_ONLY at be7108d
