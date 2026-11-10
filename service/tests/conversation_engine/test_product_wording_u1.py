"""U1: unsupported product wording (price without a currency, media type, duration) is not accepted by the shared judge.

Pure unit tests through the real judge() path; no database, no network. The referent's facts come only from the scoped
snapshot (current price information, the selected grant, matched/sent cards).
"""
import json

import pytest

from conversation_engine.model_output import JudgeContext, judge
from conversation_engine.writer_context import CreatorProfile

PROFILE = CreatorProfile.from_facts('Riley', {'age': '27', 'job': 'barista', 'home_city': 'Perth', 'limits': 'no meetups'},
                                    voice='warm, teasing', offers_permitted=True, approved_item_ids=('5',))
PRICE_INFO = [{'itemId': '5', 'label': 'photo', 'priceCents': 300, 'currency': 'USD'}]
CARD = {'itemId': '5', 'label': 'beach photo', 'priceCents': 300, 'currency': 'USD', 'sendState': 'accepted',
        'accessState': 'unconfirmed', 'financialState': 'not_established', 'financialReview': False}


def snap(message='how much is the photo?', **over):
    base = {'account_id': 'a', 'fan_id': 1, 'sales_mode': 'open', 'sales_scope': [], 'delivery_mode': 'text_only',
            'current_message': {'id': 9, 'body': message}, 'history': [],
            'memory': {'state': 'current', 'memory': {'facts': {}}},
            'offer_reference': {'state': 'none', 'offers': []}, 'catalogue_search': {'items': []}, 'offer_grants': [],
            'price_information': PRICE_INFO, 'verified_purchases': []}
    base.update(over)
    return base


def verdict(body, snapshot=None, profile=PROFILE):
    return judge(json.dumps({'body': body}), JudgeContext(snapshot=snapshot or snap(), profile=profile))


def accepted(body, snapshot=None):
    v = verdict(body, snapshot)
    assert v.outcome == 'ok', (body, v.outcome, v.reasons)


def rejected(body, snapshot=None, why='unsupported'):
    v = verdict(body, snapshot)
    assert v.outcome == 'invalid' and any(why in r for r in v.reasons), (body, v.outcome, v.reasons)


# ---- the preserved text-only examples and their wording variants ----------------------------------------------------------
@pytest.mark.parametrize('body', ['The photo costs four.', 'The photo costs 4.', 'The photo costs 4.00', 'It costs four.',
                                  'the photo is 4', 'Four for the photo, babe.', 'It is only five for the photo.',
                                  'the price is four', 'it goes for 4 babe'])
def test_wrong_price_without_currency_is_rejected(body):
    rejected(body, why='price claim')


@pytest.mark.parametrize('body', ['It is a ten-minute video.', "it's a 10 minute clip", 'The video is ten minutes long.',
                                  'It runs about ten minutes.'])
def test_unsupported_duration_or_media_type_is_rejected(body):
    v = verdict(body)
    assert v.outcome == 'invalid' and any('media' in r for r in v.reasons), (body, v.reasons)


def test_wrong_media_type_is_rejected_when_the_scoped_referent_is_a_photo():
    rejected('It is a video, babe.', why='media type claim')
    rejected('they are videos', why='media type claim')


def test_wrong_duration_is_rejected_even_when_the_type_is_right():
    grant = {'grantId': 'g', 'itemId': '5', 'label': 'clip', 'priceCents': 300, 'currency': 'USD',
             'media': {'total': 1, 'pictures': 0, 'videos': 1}}
    s = snap(offer_grants=[grant])
    rejected('It is a ten-minute video.', s, why='duration claim')
    accepted("it's a video", s)


# ---- supported facts stay answerable ---------------------------------------------------------------------------------------
@pytest.mark.parametrize('body', ['The photo costs three.', 'The photo costs 3.', 'It costs three dollars.', 'It is $3.00 for the photo.',
                                  'The photo is 3.', 'It costs 3 and it is a photo.'])
def test_correct_supported_price_is_answerable(body):  # ("Three for the photo" stays with the existing quantity rule: ambiguous)
    accepted(body)


def test_correct_media_type_and_quantity_are_answerable_from_the_grant():
    grant = {'grantId': 'g', 'itemId': '5', 'label': 'beach set', 'priceCents': 300, 'currency': 'USD',
             'media': {'total': 3, 'pictures': 2, 'videos': 1}}
    s = snap(offer_grants=[grant])
    accepted("it's a photo and a video", s)
    accepted('they are photos', s)
    accepted('it costs three', s)
    rejected('it costs five', s, why='price claim')


def test_media_type_from_the_label_of_the_scoped_price_information():
    accepted("It's a photo.")           # price_information label says photo
    rejected("It's a video.", why='media type claim')


# ---- not claims: negation, quotes, attribution, hypotheticals, questions, unrelated talk ------------------------------------
@pytest.mark.parametrize('body', [
    'That is not a video.', "it isn't a video babe", 'no, it is not a video, just the photo', "it's never a video",
    'You said it costs four, but it does not.', 'you said "the photo costs four" but nope', 'if it cost four I would send it anyway',
    'maybe it would cost four if it were a video', 'do you think it costs four?', 'is it a ten-minute video?',
    "it doesn't cost four", 'it does not cost four, it is three',
    'I have a ten minute video call later', 'It is a ten minute drive from here', "it's a video game night for me",
    'I have two cats and I just woke up', 'it costs four hours of work to edit', 'that costs 4 percent more',
    'ten minutes and I am free', 'one sec, grabbing my coffee',
])
def test_negated_quoted_hypothetical_and_unrelated_wording_is_not_a_product_claim(body):
    accepted(body)


def test_unrelated_conversation_without_any_product_context_is_untouched():
    s = snap(message='what did you do today?', price_information=[])
    accepted('It is a ten-minute drive to the cafe.', s)
    accepted('I watched a ten minute video on baking, it was a good one', s)
    accepted('It costs 4 to park there, can you believe it', s)


def test_missing_photo_or_missing_product_does_not_prohibit_ordinary_conversation():
    s = snap(message='hey, what are you up to?', price_information=[])
    accepted('Hey you, just got home from a long shift. How was your day?', s)
    accepted("I don't have a photo for you right now, but I'm happy to chat all night", s)


# ---- existing-card facts come from the scoped snapshot, not from a newly approved offer ----------------------------------
def test_existing_card_facts_come_from_the_scoped_snapshot():
    reference = {'state': 'matched', 'match': 'named', 'offers': [{'offerId': '1', 'itemId': '5', 'intentId': 'i', 'label': 'beach photo',
                                                                      'priceCents': 300, 'currency': 'USD'}], 'cards': [CARD]}
    s = snap(message='how much was the beach photo you sent?', price_information=[], offer_reference=reference)
    accepted('The beach photo was three.', s)
    rejected('The beach photo was four.', s, why='price claim')
    accepted("it's a photo", s)
    rejected("it's a video", s, why='media type claim')
    # no reference and no price information: nothing supports any price, and the model cannot supply one
    rejected('The beach photo was three.', snap(message='how much was the beach photo you sent?', price_information=[]), why='price claim')


def test_unknown_facts_get_the_normal_correction_reasons_not_an_invented_limitation():
    v = verdict('The photo costs four.')
    assert v.outcome == 'invalid' and v.proposal is not None
    assert all('cannot' not in r.lower() and 'limit' not in r.lower() for r in v.reasons)


# ---- unsupported creator facts: re-run against the same judge and profile (classified separately, see report) -------------
@pytest.mark.parametrize('body', ["I'm 24 and I love it", 'I live in Paris', "i'm from Sydney", "I'm 31 years old",
                                  'my address is 5 Smith Street', "I'm a real person, promise"])
def test_known_unsupported_creator_facts_still_rejected(body):
    assert verdict(body, snap(message='hey tell me about you', price_information=[])).outcome == 'invalid'


@pytest.mark.parametrize('body', ["I'm 27 and I love it", 'I live in Perth', 'I work as a barista'])
def test_documented_creator_facts_remain_answerable(body):
    assert verdict(body, snap(message='hey tell me about you', price_information=[])).outcome == 'ok'


def test_fan_fact_echo_is_not_a_creator_claim():
    accepted("you're 34 years old? wow", snap(message='I am 34 years old', price_information=[]))


# ---- an approved one-photo / USD 3 context (the judge cases from the review) -----------------------------------------------
def test_approved_three_dollar_photo_context():
    grant = {'grantId': 'g1', 'itemId': '5', 'label': 'photo', 'priceCents': 300, 'currency': 'USD',
             'media': {'total': 1, 'pictures': 1, 'videos': 0}}
    s = snap(offer_grants=[grant], price_information=[])
    rejected('The photo is USD 4.00.', s, why='price claim')
    rejected('The photo costs four.', s, why='price claim')
    rejected('It is a ten-minute video.', s)
    accepted('The photo costs three.', s)
    accepted('It is USD 3.00.', s)
    accepted("it's a photo", s)
    accepted('That is not a video.', s)
