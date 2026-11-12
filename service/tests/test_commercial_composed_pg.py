"""Step 4 composed proof, through the REAL worker path with fake model and fake provider only.

For a photo, a video, a mixed bundle and the cheaper-ask discount variant:
inbound question -> accurate answer -> approved product/price -> the correct paid card exactly once -> exact matching unlock ->
ownership/access update -> continued conversation without a second card for the owned item (and no duplicate acknowledgement).
Zero per-fan grants exist before the first turn; every grant is created by ordinary preparation from the standing catalogue approval.
"""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from commercial_worker_fixture import FAN_A, FAN_B, FAN_C, FAN_D, world  # noqa: F401
from conversation_engine.ownership_gate import owned_state
from db.models import Message
from db.onlyfans_engine_models import OfEngineAccessEvidence, OfEngineOfferGrant
from db.onlyfans_fans_intake_models import OfFansEvidence
from test_commercial_sale_rules_pg import intents, send_ok
from test_onlyfans_foundation_pg import database  # noqa: F401

VARIANTS = [
    # fan, ask, expected price (USD), expected media, expected previews, kind, variant, gross amount on the unlock
    (FAN_A, 'show me the beach photo', 12.00, ['31'], [], 'single_photo', 'standard', 12),
    (FAN_B, 'send me the pool video', 25.00, ['41'], [], 'single_video', 'standard', 25),
    (FAN_C, 'I want the weekend bundle', 40.00, ['51', '52', '53'], ['53'], 'bundle_mixed', 'standard', 40),
    (FAN_D, 'show me the beach photo, can you do it cheaper?', 9.00, ['31'], [], 'single_photo', 'discount', 9),
]


def outbound(w, fan):
    with Session(w.database[1]) as s:
        return [m for m in s.scalars(select(Message).where(Message.account_id == w.account, Message.fan_id == fan,
                                                           Message.direction == 'out').order_by(Message.message_id))]


def test_question_answer_product_card_exact_unlock_ownership_and_continued_chat_for_every_product_kind(world):
    world.product('Beach photo', [31], standard=1200, discount=900, description='Beach photo, one picture.')
    world.product('Pool video', [41], standard=2500, kinds={41: 'video'}, description='Pool video, one clip.')
    world.product('Weekend bundle', [51, 52, 53], previews=[53], standard=4000, kinds={52: 'video'},
                  description='Weekend bundle, two paid items and one preview.')
    world.product('Weekend photo', [51], standard=1500, description='Weekend photo, one picture.')
    world.launch(facts=(('home_city', 'Brisbane'),))
    with Session(world.database[1]) as s:
        assert list(s.scalars(select(OfEngineOfferGrant))) == []                       # standing approval only: no per-fan grant

    for fan, ask, price, media, previews, kind, variant, gross in VARIANTS:
        # 1. an ordinary question: answered from her approved facts, no card, no price
        staged, tick = world.converse('where are you from?', fan)
        assert tick is not None, (staged, world.model_prompts[-1][-900:])
        send_ok(world, tick)
        assert outbound(world, fan)[-1].body == 'I am from Brisbane.'
        assert 'APPROVED OFFER: none' in world.model_prompts[-1] and [p for p in world.cards() if f'/chats/{fan}/' in p['path']] == []
        # 2. he asks for the product: the application chose the product and the amount, the writer only referenced it
        staged, tick = world.converse(ask, fan)
        send_ok(world, tick)
        sent = [p['body'] for p in world.cards() if f'/chats/{fan}/' in p['path']]
        assert len(sent) == 1 and sent[0]['price'] == price and sent[0]['mediaFiles'] == media and sent[0]['previews'] == previews
        assert f'USD {price:.2f}' in sent[0]['text']
        intent = next(i for i in intents(world) if i.fan_id == fan and i.price_cents > 0)
        frozen = json.loads(intent.catalog_snapshot_json)['commercial']
        assert (frozen['kind'], frozen['variant'], intent.status) == (kind, variant, 'accepted')
        # 3. the exact matching unlock (gross equals the stored card): access confirmed, settlement not claimed
        world.deliver(world.unlock_event(intent.provider_message_id, gross, fan), f'unlock-{fan}')
        world.pilot.tick()
        proof = [a for a in world.rows(OfEngineAccessEvidence) if a.fan_id == fan]
        assert len(proof) == 1 and proof[0].status == 'confirmed' and proof[0].intent_id == intent.intent_id
        assert [r.reason for r in world.rows(OfFansEvidence, OfFansEvidence.fan_id == fan, OfFansEvidence.kind == 'unlock')] == [
            'unlock_confirmed_finance_unlinked']
        # 4. ownership/access update: the purchased media is owned and no longer offerable
        with Session(world.database[1]) as s:
            owned, unknown = owned_state(s, world.account, fan)
        assert unknown is None and owned == {int(m) for m in media}
        # 5. continued conversation: a compliment gets one ordinary reply; asking again gets no second card
        before = len(outbound(world, fan))
        staged, tick = world.converse('thank you, that was amazing', fan)
        send_ok(world, tick)
        staged, tick = world.converse(ask.split(',')[0], fan)
        assert tick is not None, (staged, world.model_prompts[-1][-1200:])
        send_ok(world, tick)
        assert 'APPROVED OFFER: none' in world.model_prompts[-1] and 'VERIFIED PURCHASES: ' in world.model_prompts[-1]
        assert len([p for p in world.cards() if f'/chats/{fan}/' in p['path']]) == 1
        assert len(outbound(world, fan)) == before + 2                                   # one reply per message, nothing extra
        # a replayed unlock changes nothing and sends nothing
        posts = len(world.fans_mock.posts)
        world.deliver(world.unlock_event(intent.provider_message_id, gross, fan), f'unlock-replay-{fan}')
        world.pilot.tick()
        assert len(world.fans_mock.posts) == posts and len([a for a in world.rows(OfEngineAccessEvidence) if a.fan_id == fan]) == 1

    # a whole bundle is excluded when any included paid asset is owned: the single that shares media 51 is not offered to the bundle buyer
    staged, tick = world.converse('send me the weekend photo', FAN_C)
    send_ok(world, tick)
    assert len([p for p in world.cards() if '/chats/403/' in p['path']]) == 1
    # one card per fan overall, four exact amounts, no grant outside the four journeys
    assert sorted(p['body']['price'] for p in world.cards()) == [9.0, 12.0, 25.0, 40.0]
    assert len(list(world.rows(OfEngineOfferGrant))) == 4
