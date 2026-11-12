"""Step 4.2 / 4.3 through the real worker of a commercial account: one immutable set of terms for every consumer, protected
refusals that keep ordinary chat, one active engine offer per chat, retained old cards, decline/renewed interest, stale unsent authority.

Everything arrives as a signed webhook and goes through intake -> scheduler/worker -> writer (fake model) -> named-operator approval
-> intent -> real transport (fake provider). No grant is seeded.
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from commercial_terms_fixture import approve
from commercial_worker_fixture import FAN_A, FAN_B, FAN_C, FAN_D, OPERATOR, build_world, key, world  # noqa: F401
from conversation_engine import approval, commerce, pilot_scope
from conversation_engine.writer_context import set_active_profiles
from db.models import ContentOffer, Message, VaultItem
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineOfferGrant, OfEngineOwner
from onlyfans_runtime.catalogue_terms import withdraw_terms
from onlyfans_runtime.store import Refused, utcnow
from test_commercial_preparation_pg import TENANT
from test_onlyfans_foundation_pg import database  # noqa: F401


def catalogue(w):
    """A photo, a video and a mixed bundle, approved through the production terms writer."""
    ids = {'photo': w.product('Beach photo', [31], standard=1200, discount=900, description='Beach photo, one picture.'),
           'video': w.product('Pool video', [41], standard=2500, kinds={41: 'video'}, description='Pool video, one clip.'),
           'bundle': w.product('Weekend bundle', [51, 52, 53], previews=[53], standard=4000, discount=3500, kinds={52: 'video'},
                               description='Weekend bundle, two paid items and one preview.')}
    return ids


def grants(w, fan=None):
    return w.rows(OfEngineOfferGrant, *([OfEngineOfferGrant.fan_id == fan] if fan else []))


def intents(w):
    return w.rows(OfTextIntent)


def send_ok(w, tick):
    assert tick == {'expired': 0, 'projected': 0, 'planned': [], 'dispatched': ['accepted']}, tick


# ---------------------------------------------------------------------------------------------------- 4.2 immutable terms
def test_specific_photo_video_and_bundle_requests_get_the_matching_card_with_frozen_exact_terms(world):
    ids = catalogue(world)
    world.launch()
    for fan, ask in ((FAN_A, 'show me the beach photo'), (FAN_B, 'send me the pool video'), (FAN_C, 'I want the weekend bundle')):
        staged, tick = world.converse(ask, fan)
        send_ok(world, tick)
    posts = {p['path'].split('/chats/')[1].split('/')[0]: p['body'] for p in world.cards()}
    assert posts['401']['price'] == 12.0 and posts['401']['mediaFiles'] == ['31'] and posts['401']['previews'] == []
    assert posts['402']['price'] == 25.0 and posts['402']['mediaFiles'] == ['41'] and posts['402']['previews'] == []
    assert posts['403']['price'] == 40.0 and posts['403']['mediaFiles'] == ['51', '52', '53'] and posts['403']['previews'] == ['53']
    kinds = {i.fan_id: (json.loads(i.catalog_snapshot_json)['commercial']['kind'], i.price_cents) for i in intents(world)}
    assert kinds == {FAN_A: ('single_photo', 1200), FAN_B: ('single_video', 2500), FAN_C: ('bundle_mixed', 4000)}
    # the grant hash, the intent payload and the receipt agree on the same frozen terms
    for grant in grants(world):
        intent = next(i for i in intents(world) if i.fan_id == grant.fan_id)
        assert grant.used_intent_id == intent.intent_id and json.loads(grant.authorization_json)['terms']['priceCents'] == intent.price_cents
    # a video request is never answered with the photo: no suitable approved product means no card, and chat continues
    staged, tick = world.converse('show me a beach video', FAN_D)
    send_ok(world, tick)
    assert grants(world, FAN_D) == [] and all(p['body'].get('price', 0) == 0 for p in world.fans_mock.posts if '/chats/404/' in p['path'])


def test_the_writer_cannot_state_an_amount_and_the_card_carries_the_application_amount(world):
    catalogue(world)

    def greedy(latest, prompt, approved, proposal):
        if approved and 'YOUR LAST DRAFT WAS REJECTED' not in prompt:
            return {**proposal, 'body': 'Here you go, only $5 for the photo.'}
        return proposal
    world.choose = greedy
    staged, tick = None, None
    world.launch()
    staged, tick = world.converse('show me the beach photo', FAN_A)
    send_ok(world, tick)
    sent = world.cards()[0]['body']
    assert sent['price'] == 12.0 and '$5' not in sent['text'] and 'USD 12.00' in sent['text']
    assert len(world.model_prompts) == 2 and 'YOUR LAST DRAFT WAS REJECTED' in world.model_prompts[1]     # one correction, then the exact card


def test_unavailable_unpriced_or_unknown_products_block_only_the_paid_action_not_the_chat(world):
    ids = catalogue(world)
    world.launch()
    # withdrawn by the agency
    withdraw_terms(world.store, TENANT, world.account, ids['photo'], command_key=key(0x9001), actor=__import__('commercial_terms_fixture').ACTOR)
    staged, tick = world.converse('show me the beach photo', FAN_A)
    send_ok(world, tick)
    assert grants(world, FAN_A) == [] and world.cards() == [] and 'APPROVED OFFER: none' in world.model_prompts[-1]
    # unknown ownership: a paid message whose media cannot be named
    with Session(world.database[1]) as s, s.begin():
        s.add(Message(account_id=world.account, fan_id=FAN_B, message_id=88001, direction='out', body='a paid card', is_paid=True,
                      price_cents=1500, media_ids='[]', media_count=1, created_at=utcnow() - timedelta(hours=2), is_unsent=False))
    staged, tick = world.converse('send me the pool video', FAN_B)
    send_ok(world, tick)
    assert grants(world, FAN_B) == [] and world.cards() == []
    # another chat is unaffected
    staged, tick = world.converse('I want the weekend bundle', FAN_C)
    send_ok(world, tick)
    assert len(world.cards()) == 1


def test_incomplete_history_coverage_blocks_the_card_but_not_the_reply(database, tmp_path, monkeypatch):
    w = build_world(database, tmp_path, monkeypatch, i=2, read=(FAN_A,))
    try:
        catalogue(w)
        w.launch()
        staged, tick = w.converse('show me the beach photo', FAN_B)                 # FAN_B's history was never imported
        send_ok(w, tick)
        assert w.cards() == [] and 'APPROVED OFFER: none' in w.model_prompts[-1]
        staged, tick = w.converse('show me the beach photo', FAN_A)                 # a fan with complete coverage is served
        send_ok(w, tick)
        assert len(w.cards()) == 1
    finally:
        set_active_profiles(None)
        w.store.close()


def test_takeover_and_stop_are_never_bypassed_to_make_a_sale(world):
    catalogue(world)
    world.launch()
    from db.models import OfConversationState
    with Session(world.database[1]) as s:
        revision = s.get(OfConversationState, (world.account, FAN_A)).revision
    world.store.control(TENANT, world.account, FAN_A, mode='human_controlled', expected_revision=revision,
                        command_key='b1-takeover', actor=OPERATOR, reason='staff takeover', action='request_takeover')
    result, mid = world.say('show me the beach photo', FAN_A)
    assert result.get('outcome') != 'awaiting_approval' and world.model_prompts == [] and grants(world, FAN_A) == []
    result, mid = world.say('please stop texting me', FAN_B)
    assert result.get('outcome') != 'awaiting_approval' and grants(world, FAN_B) == [] and world.cards() == []


# ---------------------------------------------------------------------------------------------------- 4.3 one active offer
def test_one_active_engine_offer_per_chat_and_an_old_card_is_never_resent_or_repriced(world):
    ids = catalogue(world)
    world.launch()
    staged, tick = world.converse('show me the beach photo', FAN_A)
    send_ok(world, tick)
    first = intents(world)[0]
    assert (first.price_cents, first.status) == (1200, 'accepted')
    # a second product while the first card is live: no second grant, no second card
    staged, mid = world.say('what about the pool video?', FAN_A)
    assert staged['outcome'] == 'awaiting_approval' and 'APPROVED OFFER: none' in world.model_prompts[-1]
    again = commerce.prepare_scoped_offer(world.store, TENANT, world.account, FAN_A, mid, world.pilot.worker.engine)
    assert again == {'state': 'active_offer', 'itemId': str(ids['photo']), 'commercial': True}
    assert len(grants(world, FAN_A)) == 1
    world.approve_turn(staged)
    world.unthrottle()
    send_ok(world, world.pilot.tick())
    # "cheaper" about the card already sent is a reply: not a resend, not a repricing
    staged, mid = world.say('the beach photo, can you do it cheaper?', FAN_A)
    assert staged['outcome'] == 'awaiting_approval'
    assert commerce.prepare_scoped_offer(world.store, TENANT, world.account, FAN_A, mid, world.pilot.worker.engine) == {
        'state': 'existing_card', 'itemId': str(ids['photo']), 'commercial': True}
    world.approve_turn(staged)
    world.unthrottle()
    send_ok(world, world.pilot.tick())
    assert [i.price_cents for i in intents(world) if i.price_cents > 0] == [1200] and len(world.cards()) == 1
    with Session(world.database[1]) as s:
        assert [o.price_cents for o in s.scalars(select(ContentOffer))] == [1200]


def test_a_second_card_is_refused_at_every_gate_even_when_an_unsent_grant_for_it_already_exists(world):
    ids = catalogue(world)
    world.launch()
    staged_b, mid_b = world.say('show me the pool video', FAN_A)                  # grant B prepared; the operator does not approve it
    assert staged_b['outcome'] == 'awaiting_approval' and len(grants(world, FAN_A)) == 1
    staged_a, tick = world.converse('send me the beach photo instead', FAN_A)     # the newer message retires draft B, card A goes out
    send_ok(world, tick)
    pool_grant = next(g for g in grants(world, FAN_A) if g.item_id == ids['video'])
    assert pool_grant.used_intent_id is None and pool_grant.expires_at > utcnow()
    with Session(world.database[1]) as s, s.begin():
        runtime = world.store._runtime(s, TENANT, world.account)
        owner = s.get(OfEngineOwner, (world.account, FAN_A))
        with pytest.raises(Refused, match='another offer is active'):
            commerce.verified_terms(s, runtime, owner, s.get(OfEngineOfferGrant, (world.account, pool_grant.grant_id)))
    staged, mid = world.say('and the pool video?', FAN_A)                         # the writer is not even shown the stale grant
    assert 'APPROVED OFFER: none' in world.model_prompts[-1]
    assert len(world.cards()) == 1


def test_a_decline_pauses_pitches_and_a_clear_renewed_request_reopens_only_that_product(world):
    ids = catalogue(world)
    world.launch()
    staged, tick = world.converse('show me the beach photo', FAN_A)
    send_ok(world, tick)
    staged, tick = world.converse('no thanks', FAN_A)
    send_ok(world, tick)
    with Session(world.database[1]) as s:
        assert s.get(OfEngineOwner, (world.account, FAN_A)).sales_mode == 'paused'
    staged, tick = world.converse('show me a photo', FAN_A)                       # vague interest while paused: still no pitch
    send_ok(world, tick)
    assert len(world.cards()) == 1 and 'APPROVED OFFER: none' in world.model_prompts[-1]
    staged, tick = world.converse('ok actually I want the pool video', FAN_A)     # explicit, specific, renewed interest
    send_ok(world, tick)
    assert len(world.cards()) == 2
    sent = [p['body'] for p in world.cards()]
    assert sent[1]['price'] == 25.0 and sent[1]['mediaFiles'] == ['41']
    with Session(world.database[1]) as s:
        owner = s.get(OfEngineOwner, (world.account, FAN_A))
        assert owner.sales_mode == 'reopened_scoped' and json.loads(owner.sales_scope_json) == [str(ids['video'])]
    old, new = sorted((i for i in intents(world) if i.price_cents > 0), key=lambda i: i.created_at)
    assert (old.price_cents, old.status, new.price_cents, new.status) == (1200, 'accepted', 2500, 'accepted')   # the old card is retained as sent


# ---------------------------------------------------------------------------------------------------- 4.3 stale unsent authority
def staged_card(w, fan=FAN_A, ask='show me the beach photo'):
    staged, mid = w.say(ask, fan)
    assert staged['outcome'] == 'awaiting_approval', staged
    return staged, mid


def nothing_sent(w):
    w.unthrottle()
    try:
        outcome = w.pilot.tick()['dispatched']
    except Refused:
        outcome = ['scope_unavailable']
    assert outcome != ['accepted'] and w.cards() == [], outcome


def test_a_price_change_before_the_wire_invalidates_the_unsent_card(world):
    ids = catalogue(world)
    world.launch()
    staged, mid = staged_card(world)
    approve(world.store, ids['photo'], standard=1500, tenant=TENANT, account=world.account)      # the agency re-prices
    try:
        world.approve_turn(staged)
    except Refused:
        pass
    nothing_sent(world)


def test_a_content_change_before_the_wire_invalidates_the_unsent_card(world):
    ids = catalogue(world)
    world.launch()
    staged, mid = staged_card(world)
    with Session(world.database[1]) as s, s.begin():
        s.get(VaultItem, (world.account, 31)).content_hash = 'h-31-replaced'
    try:
        world.approve_turn(staged)
    except Refused:
        pass
    nothing_sent(world)


def test_a_newer_fan_message_invalidates_the_unsent_card_but_not_an_accepted_one(world):
    catalogue(world)
    world.launch()
    staged_a, tick = world.converse('show me the beach photo', FAN_B)
    send_ok(world, tick)                                                           # an accepted card (evidence that must survive)
    staged, mid = staged_card(world, FAN_A)
    world.say('wait, how are you today?', FAN_A)
    try:
        world.approve_turn(staged)
    except Refused:
        pass
    world.unthrottle()
    assert world.pilot.tick()['dispatched'] != ['accepted'] or len(world.cards()) == 1
    assert [p['path'] for p in world.cards()] == ['/api/acct_prep_1/chats/402/messages']
    retained = [i for i in intents(world) if i.fan_id == FAN_B]
    assert len(retained) == 1 and retained[0].status == 'accepted' and retained[0].price_cents == 1200


def test_revoking_the_scope_before_the_wire_cancels_the_unsent_card(world):
    catalogue(world)
    world.launch()
    staged, mid = staged_card(world)
    world.approve_turn(staged)
    pilot_scope.set_status(world.store, TENANT, world.account, 'stopped', expected_revision=world.scope_revision,
                           command_key=key(0x9101), actor=OPERATOR, reason='operator revokes before the wire')
    nothing_sent(world)
