"""A resend requires current evidence of the actual previously sent item.

All creator rules, purchases, media and messages here are synthetic fixtures.
"""
import json

import pytest

from moxy_replacement.configuration import ActionBlocked
from moxy_replacement.runtime import correction_brief, validate_decision
from moxy_replacement.writer import parse
from test_moxy_foundation_authority_regressions import authority_scene
from test_moxy_integrated_pg import decision


def scene(text, *, cards=('first',), **facts):
    settings, snap, _ = authority_scene(text, **facts)
    ids = {'first': 101, 'second': 102}
    snap['card_state'] = [dict(id=9000+i, direction='out', product_id=key,
                               paid=False, price_cents=snap['inventory'][key]['price_cents'])
                          for i, key in enumerate(cards)]
    snap['fan_state']['previously_sent_assets'] = [ids[key] for key in cards]
    snap['fan_state']['paid_assets_by_product'] = {k: [v] for k, v in ids.items()}
    snap['fan_state']['exposure_history_verified'] = True
    return settings, snap


def resend(quote, *, kind='resend', key='first'):
    return parse(json.dumps(decision('resend', 'Here is the photo.', key,
                                     request_kind=kind, request_quote=quote)))


@pytest.mark.parametrize('text', [
    'How was your day?',
    'Please resend the first photo tomorrow.',
    'My friend said "please resend the first photo".',
    'I would like to talk about the first photo.',
])
def test_resend_label_and_verbatim_quote_do_not_create_a_request(text):
    settings, snap = scene(text)
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, resend(text), planning=True)


def test_one_verified_previous_card_can_be_requested_again():
    text = 'Send it again.'
    settings, snap = scene(text)
    terms = validate_decision(settings, snap, resend(text), planning=True)
    assert terms['media_ids'] == [101] and terms['price_cents'] == 7500


def test_one_previous_item_is_not_made_ambiguous_by_unsent_catalogue_items():
    text = 'Please resend that still image.'
    settings, snap = scene(text)
    terms = validate_decision(settings, snap, resend(text), planning=True)
    assert terms['media_ids'] == [101]


def test_two_matching_previously_sent_items_remain_ambiguous():
    text = 'Please resend that still image.'
    settings, snap = scene(text, cards=('first', 'second'))
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, resend(text), planning=True)


def test_two_previous_cards_make_an_unspecified_resend_ambiguous():
    text = 'Send it again.'
    settings, snap = scene(text, cards=('first', 'second'))
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, resend(text), planning=True)


def test_a_new_product_cannot_be_presented_as_a_previously_sent_card():
    text = 'Please resend the first photo.'
    settings, snap = scene(text, cards=())
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, resend(text), planning=True)


def test_stale_saved_resend_state_does_not_replace_a_current_request():
    settings, snap = scene('How was your day?', requested_resend_product='first')
    with pytest.raises(ActionBlocked):
        validate_decision(settings, snap, resend(None, kind='none'), planning=True)


def test_one_explicit_resend_does_not_erase_saved_chat_preferences():
    text = 'Send it again.'
    settings, snap = scene(text, ordinary_chat_only=True, sales_refused=True)
    terms = validate_decision(settings, snap, resend(text), planning=True)
    assert terms['media_ids'] == [101]
    assert snap['fan_state']['ordinary_chat_only'] is True
    assert snap['fan_state']['sales_refused'] is True


@pytest.mark.parametrize('facts,reason', [
    ({'owned_assets': [101]}, 'already_owned_use_existing_access'),
    ({'commerce_uncertain': True}, 'commerce_needs_reconciliation'),
    ({'human_owned': True}, 'human_owned'),
])
def test_resend_does_not_bypass_purchase_uncertainty_or_human_hold(facts, reason):
    text = 'Send it again.'
    settings, snap = scene(text, **facts)
    with pytest.raises(ActionBlocked, match=reason):
        validate_decision(settings, snap, resend(text), planning=True)


def test_missing_resend_metadata_keeps_the_proposed_card_for_one_evidence_repair():
    text = 'Send it again.'
    _, snap = scene(text)
    brief = correction_brief('resend_not_requested', resend(None, kind='none'), snap)
    assert brief['kind'] == 'factual'
    assert brief['product_id'] is None
    assert brief['keep'] == {'action': 'resend', 'product_id': 'first', 'sequence': None}
    assert brief['keep_terms']['price_cents'] == 7500


def test_a_repeated_offer_to_a_current_unique_request_can_be_repaired_as_a_resend():
    text = 'Send it again.'
    settings, snap = scene(text)
    original = parse(json.dumps(decision('offer', 'Here is the photo.', 'first',
                                         request_kind='direct', request_quote=text)))
    brief = correction_brief('media_previously_sent_requires_explicit_resend', original, snap)
    assert brief['kind'] == 'factual'
    assert brief['keep'] == {'action': 'resend', 'product_id': 'first', 'sequence': None}
    assert brief['keep_terms']['price_cents'] == 7500
    snap['event']['payload']['rejected'] = brief
    terms = validate_decision(settings, snap, resend(text), planning=True)
    assert terms['media_ids'] == [101] and terms['price_cents'] == 7500
