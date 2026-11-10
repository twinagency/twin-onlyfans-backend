"""V5 repair A: product price/type/duration assertions introduced by the OUTGOING draft are checked even when the fan did not
mention a product. Pure judge-level tests (no database, no network). Facts still come only from the scoped snapshot; missing
evidence never supports an assertion; text-only gains no media, offer authority or price snapshot."""
import json

import pytest

from conversation_engine.model_output import JudgeContext, judge
from conversation_engine.writer_context import CreatorProfile

PROFILE = CreatorProfile.from_facts('Riley', {'age': '27', 'job': 'barista', 'home_city': 'Perth', 'limits': 'no meetups'},
                                    voice='warm, teasing', offers_permitted=True, approved_item_ids=('5',))
GRANT = {'grantId': 'g1', 'itemId': '5', 'label': 'photo', 'priceCents': 300, 'currency': 'USD',
         'media': {'total': 1, 'pictures': 1, 'videos': 0}}
CARD = {'itemId': '5', 'label': 'beach photo', 'priceCents': 300, 'currency': 'USD', 'sendState': 'accepted',
        'accessState': 'unconfirmed', 'financialState': 'not_established', 'financialReview': False}
REFERENCE = {'state': 'matched', 'match': 'named', 'offers': [{'offerId': '1', 'itemId': '5', 'intentId': 'i', 'label': 'beach photo',
                                                                 'priceCents': 300, 'currency': 'USD'}], 'cards': [CARD]}


def snap(message='hello', **over):
    base = {'account_id': 'a', 'fan_id': 1, 'sales_mode': 'open', 'sales_scope': [], 'delivery_mode': 'text_only',
            'current_message': {'id': 9, 'body': message}, 'history': [],
            'memory': {'state': 'current', 'memory': {'facts': {}}},
            'offer_reference': {'state': 'none', 'offers': []}, 'catalogue_search': {'items': []}, 'offer_grants': [],
            'price_information': [], 'verified_purchases': []}
    base.update(over)
    return base


def outcome(body, snapshot):
    v = judge(json.dumps({'body': body}), JudgeContext(snapshot=snapshot, profile=PROFILE))
    return v.outcome, v.reasons


@pytest.mark.parametrize('message', ['hello', 'what are you up to?', 'good morning', 'tell me about your day'])
@pytest.mark.parametrize('body,why', [
    ('The photo costs four.', 'price claim'), ('It costs four for the photo.', 'price claim'), ('the photo is 4', 'price claim'),
    ('Four for the photo, babe.', 'price claim'), ('the price is four', 'price claim'), ('The video costs 5.', 'price claim'),
    ('It is a ten-minute video.', 'media'), ("it's a 10 minute clip", 'media'), ('The video is ten minutes long.', 'media'),
    ("It's a video, babe.", 'media type claim'), ('they are photos', 'media type claim'),
])
def test_draft_introduced_product_claim_without_any_evidence_is_rejected(message, body, why):
    result, reasons = outcome(body, snap(message))
    assert result == 'invalid' and any(why in r for r in reasons), (message, body, result, reasons)


def test_no_evidence_means_even_the_true_looking_value_is_unsupported():
    # a text-only turn carries no price snapshot, so no price can be restated from nothing
    result, _ = outcome('The photo costs three.', snap('hello'))
    assert result == 'invalid'


@pytest.mark.parametrize('body', [
    'Hey you, good to hear from you', 'It costs 4 to park there, can you believe it', 'I watched a ten minute video on baking, it was a good one',
    'It is a ten-minute drive to the cafe.', 'I have two coffees in me and I just woke up', 'ten minutes and I am free',
    'that costs 4 percent more', 'it costs four hours of work to edit', "it's a video game night for me",
    'do you think the photo costs four?', 'is it a ten-minute video?', 'if the photo cost four I would not be shocked',
    "the photo doesn't cost four", 'That is not a video.', 'you said "the photo costs four" but nope',
    'maybe it would cost four if it were a video', 'I took a photo of my coffee this morning',
])
def test_ordinary_numbers_negation_quotes_questions_and_hypotheticals_stay_ok(body):
    result, reasons = outcome(body, snap('hello'))
    assert result == 'ok', (body, reasons)


@pytest.mark.parametrize('message', ['hello', 'how much is the photo?', 'tell me about you'])
def test_full_mode_accurate_usd3_photo_wording_stays_ok(message):
    s = snap(message, offer_grants=[GRANT], delivery_mode='full')
    for body in ('The photo costs three.', 'It is USD 3.00.', "it's a photo", 'That is not a video.'):
        assert outcome(body, s)[0] == 'ok', (message, body)
    for body in ('The photo costs four.', 'It is a ten-minute video.', "it's a video"):
        assert outcome(body, s)[0] == 'invalid', (message, body)


def test_existing_card_discussion_uses_the_scoped_card_not_the_incoming_text():
    s = snap('hello', offer_reference=REFERENCE)
    assert outcome('The beach photo was three.', s)[0] == 'ok'
    assert outcome('The beach photo was four.', s)[0] == 'invalid'
    assert outcome("it's a video", s)[0] == 'invalid'


def test_relevant_incoming_question_still_grounds_from_price_information():
    s = snap('how much is the photo?', price_information=[{'itemId': '5', 'label': 'photo', 'priceCents': 300, 'currency': 'USD'}])
    assert outcome('The photo costs three.', s)[0] == 'ok'
    assert outcome('The photo costs four.', s)[0] == 'invalid'


def test_text_only_gains_no_authority_from_the_check():
    from conversation_engine.claims import grounding_from_snapshot
    g = grounding_from_snapshot(snap('hello'))
    assert not g.current_offer_prices and not g.media_kinds and not g.durations and not g.quantities
