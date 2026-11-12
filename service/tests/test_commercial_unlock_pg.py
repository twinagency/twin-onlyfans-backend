"""Step 4.4: exact unlock and access handling for cards the commercial engine sent.

A card is matched by creator/account/binding + fan + provider message id + intent + frozen grant/terms. An amount alone, a nearby
timestamp, a product name or a staff card never links. A supplied gross amount (and currency, when supplied) must equal the stored
card; net-of-fee is never compared as the price. Access confirmation and financial settlement stay distinct; early and replayed
unlocks are retained / applied once; a reversal is retained and never infers loss of access.
"""
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from commercial_worker_fixture import FAN_A, FAN_B, FAN_C, FAN_D, build_world, world  # noqa: F401
from conversation_engine.ownership_gate import owned_state
from db.models import ContentOffer, Transaction
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineAccessEvidence, OfEngineOfferGrant
from db.onlyfans_fans_intake_models import OfFansEvidence
from onlyfans_runtime.store import Refused
from test_commercial_preparation_pg import TENANT
from test_commercial_sale_rules_pg import catalogue, grants, intents, send_ok
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_engine_real_worker_pg import iso


def card(w, fan=FAN_A, ask='show me the beach photo'):
    staged, tick = w.converse(ask, fan)
    send_ok(w, tick)
    return next(i for i in intents(w) if i.fan_id == fan and i.price_cents > 0)


def unlock(w, message_id, amount, fan=FAN_A, key=None, **changes):
    try:
        result = w.deliver(w.unlock_event(message_id, amount, fan, **changes), key)
    except Refused as exc:
        result = {'refused': str(exc)}
    w.pilot.tick()
    return result


def unlocks(w):
    return w.rows(OfFansEvidence, OfFansEvidence.kind == 'unlock')


def access(w):
    return w.rows(OfEngineAccessEvidence)


def owned(w, fan=FAN_A):
    with Session(w.database[1]) as s:
        return owned_state(s, w.account, fan)


def test_an_exact_unlock_confirms_access_and_excludes_the_media_while_settlement_stays_separate(world):
    catalogue(world)
    world.launch()
    sent = card(world)
    assert owned(world) == (set(), None)
    unlock(world, sent.provider_message_id, 12, amountNet=9.6)                           # gross 12.00 equals the card; net is never the price
    row = unlocks(world)[0]
    assert (row.status, row.reason, row.intent_id, row.grant_id) == ('applied', 'unlock_confirmed_finance_unlinked', sent.intent_id,
                                                                    grants(world)[0].grant_id)
    proof = access(world)
    assert len(proof) == 1 and (proof[0].status, proof[0].applied, proof[0].intent_id, proof[0].message_id) == (
        'confirmed', True, sent.intent_id, sent.provider_message_id) and json.loads(proof[0].media_json) == [31]
    assert owned(world) == ({31}, None)                                                  # projected into the existing ownership read
    with Session(world.database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0              # access is not a settled payment
    # a later money event is a separate fact: never matched to the card by amount or time
    world.deliver({'event': 'transactions.new', 'account_id': world.provider, 'payload': {
        'id': 'tx-1', 'created_at': '2026-11-12 02:00:00', 'type': 'message', 'amount': 12, 'currency': 'USD', 'status': 'done',
        'fan': {'id': str(FAN_A)}}}, 'tx-evt-1')
    world.pilot.tick()
    assert unlocks(world)[0].status == 'applied' and len(access(world)) == 1
    assert owned(world) == ({31}, None)


def test_duplicate_and_replayed_unlocks_apply_once(world):
    catalogue(world)
    world.launch()
    sent = card(world)
    for n in range(3):
        unlock(world, sent.provider_message_id, 12, key=f'unlock-replay-{n}')            # same provider fact, new delivery keys
    unlock(world, sent.provider_message_id, 12, key='unlock-replay-0')                   # and an exact replay
    assert len(unlocks(world)) == 1 and len(access(world)) == 1 and access(world)[0].applied is True
    assert owned(world) == ({31}, None)


def test_an_unlock_that_beats_the_receipt_is_retained_until_its_card_is_linked(world):
    catalogue(world)
    world.launch()
    seen = {}

    def fan_unlocks_while_the_post_is_in_flight(count):
        seen['result'] = world.deliver(world.unlock_event(world.fans_mock.next_id - 1, 12), 'unlock-in-flight')
    staged, mid = world.say('show me the beach photo', FAN_A)
    world.approve_turn(staged)
    world.unthrottle()
    world.fans_mock.hooks.append(fan_unlocks_while_the_post_is_in_flight)                  # delivered between the POST and its receipt
    send_ok(world, world.pilot.tick())
    world.fans_mock.hooks.clear()
    assert seen['result']['outcome'] == 'evidence'
    sent = next(i for i in intents(world) if i.price_cents > 0)
    row = unlocks(world)[0]
    assert row.status == 'pending' and row.reason in ('no_matching_send', 'awaiting_commerce')       # retained, never held for review
    world.pilot.tick()
    row = unlocks(world)[0]
    assert (row.status, row.intent_id) == ('applied', sent.intent_id) and len(access(world)) == 1


def test_wrong_creator_fan_message_amount_currency_and_staff_card_never_link(world):
    catalogue(world)
    world.launch()
    a, b, c = card(world, FAN_A), card(world, FAN_B), card(world, FAN_C)
    unlock(world, a.provider_message_id, 5, FAN_A)                                       # amount differs from the card
    unlock(world, b.provider_message_id, 12, FAN_B, currency='EUR')                      # a supplied currency must match the card
    unlock(world, c.provider_message_id, 12, FAN_D)                                      # another fan names this fan's message
    unlock(world, 9999, 12, FAN_A, key='unknown-message')                                # no send of ours carries this id
    unlock(world, a.provider_message_id, 12, FAN_A, user_id='someone-else', key='wrong-creator')   # not this creator
    assert access(world) == []
    for fan in (FAN_A, FAN_B, FAN_C):
        assert owned(world, fan)[0] == set()                                              # nothing was projected as owned
    by_reason = {(r.fan_id, r.message_id): (r.status, r.reason) for r in unlocks(world)}
    assert by_reason[(FAN_A, a.provider_message_id)] == ('held', 'amount_mismatch')
    assert by_reason[(FAN_B, b.provider_message_id)] == ('held', 'currency_mismatch')
    assert by_reason[(FAN_D, c.provider_message_id)] == ('held', 'fan_mismatch')
    assert by_reason[(FAN_A, 9999)] == ('pending', 'no_matching_send')
    assert all(r.intent_id is None for r in unlocks(world))                                # none of them bound to any intent


def test_a_net_amount_alone_is_not_a_price_and_a_missing_amount_is_not_invented(world):
    catalogue(world)
    world.launch()
    a, b = card(world, FAN_A), card(world, FAN_B)
    unlock(world, a.provider_message_id, 14, FAN_A, amountNet=12)                        # net equals the card price, gross does not
    assert access(world) == [] and unlocks(world)[0].reason == 'amount_mismatch'
    payload = world.unlock_event(b.provider_message_id, 12, FAN_B)
    del payload['payload']['amountGross']
    payload['payload']['amountNet'] = 9.6                                                # the provider supplied no gross amount
    world.deliver(payload, 'unlock-no-gross')
    world.pilot.tick()
    row = next(r for r in unlocks(world) if r.fan_id == FAN_B)
    assert row.amount_cents is None and row.status == 'applied'                          # linked by id; no price was fabricated for it


def test_a_reversal_is_retained_and_never_revokes_access_or_reopens_the_media_for_sale(world):
    catalogue(world)
    world.launch()
    sent = card(world)
    unlock(world, sent.provider_message_id, 12)
    world.deliver({'event': 'chargebacks.new', 'account_id': world.provider, 'payload': {
        'payment': {'id': 'tx-9', 'amount': 12, 'user': {'id': FAN_A}, 'createdAt': iso()}}}, 'chargeback-1')
    world.pilot.tick()
    cb = world.rows(OfFansEvidence, OfFansEvidence.kind == 'chargeback')
    assert len(cb) == 1 and cb[0].status == 'pending' and cb[0].reason == 'unlinked_reversal'      # retained for a person
    proof = access(world)
    assert len(proof) == 1 and proof[0].status == 'confirmed' and proof[0].applied is True        # access is not inferred lost
    owned_media, unknown = owned(world)
    assert unknown == 'unresolved_reversal'                                                      # unknown, never "unowned"
    before = len(world.model_prompts)
    staged, tick = world.converse('show me the beach photo again', FAN_A)                       # ordinary chat continues ...
    send_ok(world, tick)
    assert 'APPROVED OFFER: none' in world.model_prompts[before]                                # ... but no paid action is available
    assert len([p for p in world.cards() if '/chats/401/' in p['path']]) == 1                    # no second card for the media


def test_an_accepted_but_unprojected_card_is_repaired_without_resend_and_its_early_unlock_still_links(world, monkeypatch):
    from model_transport.pilot_supervised import build_supervised_pilot
    catalogue(world)
    world.launch()
    staged, mid = world.say('show me the beach photo', FAN_A)
    world.approve_turn(staged)
    world.unthrottle()
    authority = world.pilot.worker.authority
    original = authority.materialize

    def crash(*args, **kwargs):
        raise RuntimeError('crash after the accepted receipt was committed, before projection')
    monkeypatch.setattr(authority, 'materialize', crash)
    with pytest.raises(RuntimeError, match='crash after the accepted receipt'):
        world.pilot.worker.send_approved(TENANT, world.account, staged['decisionId'])
    assert len(world.cards()) == 1
    card_id = world.fans_mock.next_id - 1
    world.deliver(world.unlock_event(card_id, 12), 'unlock-before-projection')           # the unlock beats the projection
    assert access(world) == [] and unlocks(world)[0].status in ('pending',)              # retained, not held for review, not applied
    monkeypatch.setattr(authority, 'materialize', original)
    restarted = build_supervised_pilot(world.store, world.env, model_transport=world.model_transport,
                                       fans_transport=world.fans_mock.transport)
    for _ in range(3):
        restarted.tick()
    assert len(world.cards()) == 1                                                       # repaired from the saved receipt, never resent
    sent = next(i for i in intents(world) if i.price_cents > 0)
    assert sent.status == 'accepted' and sent.projected_at is not None and sent.provider_message_id == card_id
    row = unlocks(world)[0]
    assert (row.status, row.intent_id) == ('applied', sent.intent_id) and len(access(world)) == 1
