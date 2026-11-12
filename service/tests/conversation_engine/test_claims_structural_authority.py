"""F1b: product truth (kind, count, duration, price, bundle) is bound to the ONE selected authorized grant.

Pure unit tests, no database, no network. Every case runs through BOTH the real model judge() and the shared plain-text gate
that the operator edit uses. The wording shapes deliberately differ (imperative, verbless, passive, noun phrase, first person,
plural, slang nouns): the contract is structural, so no case is a phrase that the implementation may special-case.

Controls (questions, quotations, negation, conditionals, unrelated conversation, truthful descriptions) must stay accepted.
A second product in the same snapshot (a decoy grant) must never authorize wording about the selected one.
"""
import json

import pytest

from conversation_engine.claims import check_plain_text, grounding_from_snapshot
from conversation_engine.engine import SyntheticProposal
from conversation_engine.model_output import JudgeContext, judge
from conversation_engine.writer_context import CreatorProfile
from onlyfans_runtime.store import Refused

PROFILE = CreatorProfile.from_facts('Riley', {'age': '27'}, voice='warm', offers_permitted=True, approved_item_ids=('5', '6'))


def grant(grant_id, item, label, cents, pictures, videos, **extra):
    media = {'total': pictures + videos, 'pictures': pictures, 'videos': videos}
    return {'grantId': grant_id, 'itemId': item, 'label': label, 'description': label, 'mediaCount': media['total'],
            'media': media, 'priceCents': cents, 'currency': 'USD', **extra}


PHOTOS = grant('g-photos', '5', 'Beach Set', 1200, 3, 0)
CLIP = grant('g-clip', '5', 'Pool Clip', 2000, 0, 1, durationSeconds=90)
MIXED = grant('g-mixed', '5', 'Beach Bundle', 3000, 2, 1, isBundle=True, productCount=1, discountApproved=False,
              kind='bundle', variant='standard', standardCents=3000)
# A different product that is also in the snapshot: video, a duration, a price, a bundle. It must authorize nothing.
DECOY = grant('g-decoy', '6', 'Decoy Video Bundle', 500, 0, 4, durationSeconds=600, isBundle=True, productCount=1)


def snapshot(selected, *, message='the beach set', terms=False):
    grants = [selected, DECOY]
    return {'account_id': 'w1', 'fan_id': 1, 'sales_mode': 'open', 'sales_scope': [], 'current_message': {'id': 9, 'body': message},
            'history': [], 'memory': {'state': 'current', 'memory': {'facts': {}}},
            'offer_reference': {'state': 'matches', 'offers': [{'itemId': g['itemId'], 'label': g['label'], 'priceCents': g['priceCents'],
                                                                 'media': g['media']} for g in grants]},
            'catalogue_search': {'items': []}, 'verified_purchases': [], 'offer_grants': grants,
            **({'terms_mode': 'commercial.v1'} if terms else {})}


def outcome(path, selected, body, *, message='the beach set', terms=True):
    snap = snapshot(selected, message=message, terms=terms)
    ctx = JudgeContext(snapshot=snap, profile=PROFILE, allowed_grant_ids=frozenset({selected['grantId']}))
    if path == 'judge':
        verdict = judge(json.dumps({'body': body, 'action': 'offer', 'offer_grant_id': selected['grantId'], 'pitch': True}), ctx)
        return verdict.outcome == 'ok', list(verdict.reasons)
    proposal = SyntheticProposal(body, action='offer', offer_grant_id=selected['grantId'], pitch=True)
    prices = {offer['priceCents'] for offer in snap['offer_reference']['offers']}        # what approval.edit passes today
    try:
        check_plain_text([body], proposal, 'open', prices, grounding=grounding_from_snapshot(snap, proposal))
        return True, []
    except Refused as exc:
        return False, [str(exc)]


PATHS = ['judge', 'shared_gate']

# (selected grant, text, accepted)
CASES = [
    # --- false KIND on a photo-only grant: every sentence shape, not a list of phrases ---
    (PHOTOS, 'Enjoy this video.', False), (PHOTOS, 'A little video clip for you.', False), (PHOTOS, 'Your video is attached.', False),
    (PHOTOS, 'Check out this clip.', False), (PHOTOS, 'Here are your vids, babe.', False), (PHOTOS, 'Watch this reel.', False),
    (PHOTOS, 'Hope you like the video I made.', False), (PHOTOS, 'Sending you a quick vid.', False),
    (PHOTOS, 'Short and sweet, a hot clip.', False), (PHOTOS, 'New video, just dropped.', False),
    (PHOTOS, 'One video and two photos.', False), (PHOTOS, 'Not just photos, a video too.', False),
    (PHOTOS, "Don't miss this video.", False),
    (PHOTOS, 'A short film for you.', False), (PHOTOS, 'Enjoy the movie.', False), (PHOTOS, 'Two clips for you.', False),
    # --- false COUNT ---
    (PHOTOS, 'Nine photos for you.', False), (PHOTOS, 'One photo for you.', False), (PHOTOS, 'Six pictures in this set.', False),
    (PHOTOS, 'Three videos for you.', False), (PHOTOS, 'Another 5 pics after this.', False), (PHOTOS, 'Ten items in this set.', False),
    # --- unsupported DURATION (no grant carries one) ---
    (PHOTOS, 'Enjoy this ten-minute photo set.', False),
    (MIXED, 'Enjoy this ten-minute video.', False), (MIXED, 'It lasts ten minutes.', False), (MIXED, 'A 5 min clip for you.', False),
    # --- false PRICE, including another card's price ---
    (PHOTOS, 'It is USD 5.00 for this set.', False), (PHOTOS, 'It is $5 for this set.', False), (PHOTOS, 'Only five dollars for you.', False),
    (PHOTOS, 'The photos cost four.', False), (PHOTOS, 'It is $20 for this set.', False),
    # --- truthful photo ---
    (PHOTOS, 'Here you go, three photos for you.', True), (PHOTOS, 'Enjoy these photos.', True), (PHOTOS, 'Enjoy this set.', True),
    (PHOTOS, 'A little photo set for you.', True), (PHOTOS, 'Your pictures are attached.', True), (PHOTOS, 'It is USD 12.00 for this set.', True),
    (PHOTOS, 'Three pictures in this set.', True), (PHOTOS, 'Three items in this set.', True), (PHOTOS, 'Hope you like them.', True),
    # --- truthful video, including its approved duration ---
    (CLIP, 'Enjoy this video.', True), (CLIP, 'A little video clip for you.', True), (CLIP, 'One video for you.', True),
    (CLIP, 'It lasts ninety seconds.', True), (CLIP, 'Enjoy this 90 second video.', True),
    (CLIP, 'Enjoy this photo.', False), (CLIP, 'Two videos for you.', False), (CLIP, 'Enjoy this two minute video.', False),
    (CLIP, 'It lasts two minutes.', False), (CLIP, 'It is USD 5.00.', False),
    # --- mixed bundle: each kind and count is the grant's own ---
    (MIXED, 'Two photos and a video in this bundle.', True), (MIXED, 'Enjoy these photos and the video.', True),
    (MIXED, 'Three items in this bundle.', True), (MIXED, 'It is USD 30.00 for the bundle.', True),
    (MIXED, 'Two videos and a photo.', False), (MIXED, 'Five photos and a video.', False), (MIXED, 'Four items for you.', False),
    (MIXED, 'It is USD 12.00 for the bundle.', False),
    # --- controls: questions, quotations, attribution, negation, conditionals, unrelated conversation ---
    (PHOTOS, 'Would you prefer a photo or a video?', True), (PHOTOS, 'Is this a video?', True), (PHOTOS, 'Do you like videos?', True),
    (PHOTOS, 'You said "Your video is attached."', True), (PHOTOS, 'You said it was a video but it is not.', True),
    (PHOTOS, 'It is not a video.', True), (PHOTOS, 'This is no video, just photos.', True), (PHOTOS, 'I do not have any videos for you.', True),
    (PHOTOS, 'If it were a video it would cost more.', True), (PHOTOS, 'Video games are fun.', True),
    (PHOTOS, 'Two coffees sounds good.', True), (PHOTOS, 'Ten minutes and I am free.', True), (PHOTOS, 'I have a ten minute video call later.', True),
    (PHOTOS, 'Good to hear from you.', True), (PHOTOS, 'It is a ten minute drive from here.', True),
]


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('case', CASES, ids=[f'{c[0]["grantId"]}:{c[1]}:{"ok" if c[2] else "no"}' for c in CASES])
def test_product_claims_are_bound_to_the_selected_grant(path, case):
    selected, body, expected = case
    accepted, reasons = outcome(path, selected, body)
    assert accepted is expected, (path, selected['grantId'], body, reasons)


# --- the selected grant, not a text-ranked neighbour, is the authority -------------------------------------------------------
def test_judge_uses_the_selected_grant_even_when_the_fan_text_ranks_another_one_first():
    two = {**snapshot(PHOTOS, message='send me the video please'), 'offer_grants': [PHOTOS, CLIP]}
    ctx = JudgeContext(snapshot=two, profile=PROFILE, allowed_grant_ids=frozenset({'g-photos', 'g-clip'}))
    def verdict(grant_id, body):
        return judge(json.dumps({'body': body, 'action': 'offer', 'offer_grant_id': grant_id, 'pitch': True}), ctx)
    assert verdict('g-photos', 'Three photos for you.').outcome == 'ok'          # facts of g-photos, not of the video the text ranks first
    assert verdict('g-clip', 'One video for you.').outcome == 'ok'
    assert verdict('g-photos', 'One video for you.').outcome == 'invalid'
    assert verdict('g-clip', 'Three photos for you.').outcome == 'invalid'


def test_bundle_and_discount_wording_follow_the_selected_grant_not_the_fan_text_ranking():
    snap = {**snapshot(PHOTOS, message='send me a bundle', terms=True), 'offer_grants': [PHOTOS, MIXED]}
    ctx = JudgeContext(snapshot=snap, profile=PROFILE, allowed_grant_ids=frozenset({'g-photos', 'g-mixed'}))
    def verdict(grant_id, body):
        return judge(json.dumps({'body': body, 'action': 'offer', 'offer_grant_id': grant_id, 'pitch': True}), ctx)
    assert verdict('g-photos', 'A little bundle for you.').outcome == 'invalid'        # the selected card is a single set
    assert verdict('g-mixed', 'A little bundle for you.').outcome == 'ok'


def test_a_missing_or_stale_grant_authorizes_no_product_claim():
    snap = snapshot(PHOTOS)
    snap['offer_grants'] = [DECOY]                      # the selected grant is not in the current authorized set
    proposal = SyntheticProposal('Three photos for you.', action='offer', offer_grant_id='g-photos', pitch=True)
    with pytest.raises(Refused):
        check_plain_text([proposal.body], proposal, 'open', {1200}, grounding=grounding_from_snapshot(snap, proposal))
    # prose that makes no product claim is still fine
    ok = SyntheticProposal('Here you go.', action='offer', offer_grant_id='g-photos', pitch=True)
    check_plain_text([ok.body], ok, 'open', {1200}, grounding=grounding_from_snapshot(snap, ok))
