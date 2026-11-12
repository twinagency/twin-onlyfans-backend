"""Plan C defect 2: claim checks are semantic (negation, grounding, price/payload equality), not bare word lists.

Pure unit tests, no database. Real claims.check_plain_text and the real judge() path.
"""
import json

import pytest

from conversation_engine.claims import Grounding, check_plain_text, grounding_from_snapshot
from conversation_engine.engine import SyntheticProposal
from conversation_engine.model_output import JudgeContext, judge
from conversation_engine.writer_context import CreatorProfile
from onlyfans_runtime.store import Refused

OWNED = Grounding(quantities=frozenset({3}), purchase_labels=('Beach Set',))
PRICES = frozenset({600})


def run(text, *, action='reply', mode='open', grounding=None, prices=PRICES, **extra):
    check_plain_text([text], SyntheticProposal(text, action=action, **extra), mode, prices, grounding=grounding)


ACCEPTED = [
    # negation / future / inquiry must not trip purchase or pitch checks
    ('No need to buy anything', {}),
    ('you do not have to buy anything babe', {}),
    ("I won't push you to unlock it", {}),
    ('I will check your purchase', {}),
    ("let me check your purchase real quick", {}),
    ("I'll look into your purchase and get back to you", {}),
    ('did you buy it yet?', {}),
    ('if you bought it you should see it in your chat', {}),
    ("it's not for sale right now", {}),
    ('no discount, sorry, the price is what it is', {'mode': 'open', 'action': 'offer'}),
    ('haha pay attention to me instead', {}),
    ('I do not do customs, sorry', {}),
    ('customs are not something I do', {}),
    ('do you want a custom video?', {}),
    ('I do not read every message myself, sorry', {}),
    # grounded support
    ('thanks for buying the Beach Set, hope you like it', {'grounding': OWNED}),
    ('you already bought the beach set babe', {'grounding': OWNED, 'purchase_claim': True}),
    # approved price and payload quantity only
    ('it is USD 6.00 for the set', {'action': 'offer', 'pitch': True}),
    ('three pics in this set', {'action': 'offer', 'grounding': OWNED, 'pitch': True}),
    ('3 photos in there', {'action': 'offer', 'grounding': OWNED, 'pitch': True}),
    # ordinary conversation containing digits / ordinary words
    ('I have 2 cats and I just woke up', {}),
    ('one sec, grabbing my coffee', {}),
    ('ten minutes and I am free', {}),
    ('what do you do for fun?', {}),
]

REJECTED = [
    # price: not the approved price, other currencies/units, spelled out
    ('it is $5 for the set', 'price'),
    ('only five dollars for you', 'price'),
    ('USD 7.00 for the set', 'price'),
    ('50 credits and it is yours', 'price'),
    ('just 3 stars', 'price'),
    # purchase / access claims without grounding
    ('thanks for buying it', 'purchase'),
    ('you already bought it', 'purchase'),
    ('thank you for your purchase', 'purchase'),
    ('I can see your purchase went through', 'purchase'),
    ('your payment came through', 'purchase'),
    ('it is already unlocked for you', 'purchase'),
    ('you now have access to the full set', 'purchase'),
    ('thanks for buying the Sunset Pack', 'purchase'),   # a label he does NOT own
    # pitch
    ('buy it now babe', 'verified offer'),
    ('you should unlock this one tonight', 'verified offer'),
    ('want to purchase the set?', 'verified offer'),
    ('there is a special offer on right now', 'verified offer'),
    # invented quantity
    ('I have six pictures for you', 'quantity'),
    ('i can send 10 videos', 'quantity'),
    ('five more clips if you want', 'quantity'),
    # customs
    ("I'll make you a custom video", 'custom'),
    ('yes I can do customs for you', 'custom'),
    ("sure, I'll film something personalized video just for you", 'custom'),
    # creator involvement / identity
    ('I personally read every message', 'who writes'),
    ('she reviews all my replies herself', 'who writes'),
    ("I'm actually her, the real creator", 'who writes'),
    ('she trained me herself', 'who writes'),
    ('nobody else reads these', 'who writes'),
]


@pytest.mark.parametrize('text,kw', ACCEPTED)
def test_accepted_sentences(text, kw):
    run(text, **kw)


@pytest.mark.parametrize('text,reason', REJECTED)
def test_rejected_sentences(text, reason):
    extra = {'pitch': True, 'action': 'offer'} if reason in {'quantity', 'custom', 'who writes'} else {}
    grounding = OWNED if 'Sunset' in text else None      # he owns the Beach Set only
    with pytest.raises(Refused, match=reason):
        run(text, grounding=grounding, **extra)


def test_paused_sales_blocks_a_pitch_but_not_negated_or_support_text():
    with pytest.raises(Refused, match='paused'):
        run('you could unlock it now', mode='paused', action='offer')
    run('no need to buy anything', mode='paused')
    run('I will check your purchase', mode='paused')


def test_declared_flags_still_enforced():
    with pytest.raises(Refused, match='price'):
        run('hi', price_claims=(999,))
    with pytest.raises(Refused, match='purchase'):
        run('hi', purchase_claim=True)                       # declared but nothing grounds it
    with pytest.raises(Refused, match='verified offer'):
        run('hi', pitch=True)
    with pytest.raises(Refused, match='paused'):
        run('hi', pitch=True, action='offer', mode='paused')


def test_quantity_must_match_payload_exactly_not_just_be_a_number():
    with pytest.raises(Refused, match='quantity'):
        run('six pictures in this set', action='offer', pitch=True, grounding=OWNED)   # PDF round 1: text six, payload three
    with pytest.raises(Refused, match='quantity'):
        run('three pics in this set', action='offer', pitch=True, grounding=Grounding(frozenset({6})))
    run('2 pics and 1 video', action='offer', pitch=True, grounding=OWNED)             # parts sum to the payload of three


def test_grounding_from_snapshot_uses_only_the_granted_offer_and_owned_items():
    snap = {'offer_grants': [{'grantId': 'g1', 'mediaCount': 3}, {'grantId': 'g2', 'mediaCount': 9}],
            'history': [{'direction': 'in', 'media_count': 7}, {'direction': 'out', 'media_count': 2}],
            'catalogue_search': {'items': [{'mediaIds': [1, 2, 3, 4]}]},
            'verified_purchases': [{'itemId': '1', 'label': 'Beach Set'}]}
    offer = SyntheticProposal('x', action='offer', offer_grant_id='g1')
    g = grounding_from_snapshot(snap, offer)
    assert g.quantities == {3, 2, 4} and g.purchase_labels == ('Beach Set',)
    assert 9 not in g.quantities and 7 not in g.quantities


# ---- the same checks on the real judge() path, including the PDF regressions -------------------------------
PROFILE = CreatorProfile.from_facts('Riley', {'age': '27'}, voice='warm', offers_permitted=True, approved_item_ids=('5',))


def _ctx(grants=(), **over):
    snap = {'account_id': 'a', 'fan_id': 1, 'sales_mode': 'open', 'sales_scope': [], 'current_message': {'id': 9, 'body': 'hey'},
            'history': [], 'memory': {'state': 'current', 'memory': {'facts': {}}},
            'offer_reference': {'state': 'none', 'offers': []}, 'catalogue_search': {'items': []},
            'offer_grants': [{'grantId': 'g1', 'itemId': '5', 'label': 'Beach Set', 'description': 'beach', 'mediaCount': 3,
                             'media': {'total': 3, 'pictures': 3, 'videos': 0}}],
            'verified_purchases': []}
    snap.update(over)
    return JudgeContext(snapshot=snap, profile=PROFILE, allowed_grant_ids=frozenset(grants))


def _raw(body, **extra):
    return json.dumps({'body': body, **extra})


def test_judge_accepts_grounded_support_and_negation():
    for body in ('no need to buy anything, just talk to me', 'I will check your purchase for you'):
        assert judge(_raw(body), _ctx()).outcome == 'ok', body
    ctx = _ctx(verified_purchases=[{'itemId': '5', 'label': 'Beach Set'}])
    assert judge(_raw('thanks for buying the beach set'), ctx).outcome == 'ok'


def test_judge_pdf_failure_text_says_six_pictures_but_payload_has_three():
    verdict = judge(_raw('six pictures of me at the beach', action='offer', offer_grant_id='g1', pitch=True), _ctx(grants=('g1',)))
    assert verdict.outcome == 'invalid' and any('quantity' in r for r in verdict.reasons), verdict.reasons
    ok = judge(_raw('three pictures of me at the beach', action='offer', offer_grant_id='g1', pitch=True), _ctx(grants=('g1',)))
    assert ok.outcome == 'ok', ok.reasons


@pytest.mark.parametrize('body', ["she reads and reviews every message personally", "I'm just an assistant but she trained me herself",
                                  "yes I can make you a custom video", "I personally type every reply"])
def test_judge_pdf_failure_unsupported_creator_involvement_and_customs(body):
    assert judge(_raw(body), _ctx()).outcome == 'invalid'


@pytest.mark.parametrize('body', [
    "Here you go, a short video clip for you.",
    "Here's a video for you.", "I sent you a short video.",
    "Sending you a video.", "Attached a video.",
])
def test_presented_media_kind_requires_grounded_kind(body):
    with pytest.raises(Refused, match='media type'):
        run(body, action='offer', grounding=Grounding(product_context=True, media_kinds=frozenset({'picture'})))
    run(body, action='offer', grounding=Grounding(product_context=True, media_kinds=frozenset({'video'})))


@pytest.mark.parametrize('body', [
    'Did I send you a video?', 'You said "Here you go, a short video clip for you."',
    'I never sent you a video.', 'If I sent you a video, would you watch it?',
    'I had a video call with my sister.', 'Here is a video game I like.',
    'Here you go, a photo for you.',
])
def test_presentation_checks_preserve_questions_reports_and_unrelated_chat(body):
    run(body, action='offer', grounding=Grounding(product_context=True, media_kinds=frozenset({'picture'})))


def test_other_video_price_information_does_not_ground_photo_card_wording():
    ctx = _ctx(grants=('g1',), price_information=[{'itemId':'other', 'label':'Pool video',
        'priceCents':600,'currency':'USD','media':{'videos':1,'pictures':0}}])
    verdict=judge(_raw('Here you go, a short video clip for you.',action='offer',offer_grant_id='g1',pitch=True),ctx)
    assert verdict.outcome=='invalid' and any('media type' in r for r in verdict.reasons)
