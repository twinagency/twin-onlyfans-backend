"""Step 4.1: the normal worker path of a COMMERCIAL account.

Inbound webhook -> scheduler/worker -> real preparation from the account's standing catalogue approval (ZERO per-fan grants
before the turn) -> writer (fake model) -> named-operator approval -> intent -> send through the real transport (fake provider).
`test_commercial_terms_sale_path_pg.py` keeps its component coverage (direct preparation); this file proves the wiring.
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from commercial_worker_fixture import FAN_A, FAN_B, OPERATOR, World, build_world, key, world  # noqa: F401
from conversation_engine import approval, pilot_scope
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineOfferGrant
from onlyfans_runtime.store import Refused
from test_commercial_preparation_pg import TENANT
from test_onlyfans_foundation_pg import database  # noqa: F401
from conversation_engine.writer_context import set_active_profiles


def grants(world):
    return world.rows(OfEngineOfferGrant)


def test_a_normal_turn_prepares_the_grant_from_the_standing_catalogue_and_sends_one_exact_card(world):
    sunset = world.product('Sunset photo', [31, 32], previews=[31], standard=1200, discount=900,
                           description='Sunset photo, two pictures with one preview.')
    world.launch()
    assert grants(world) == []                                                   # standing approval only; no per-fan grant exists
    scope = pilot_scope.describe(world.store, TENANT, world.account)
    assert scope['config']['contract_version'] == 'commercial.v1' and scope['status'] == 'active'

    staged, mid = world.say('show me the sunset photo please')
    assert staged['outcome'] == 'awaiting_approval', staged
    rows = grants(world)
    assert len(rows) == 1 and rows[0].item_id == sunset and rows[0].fan_id == FAN_A and rows[0].used_intent_id is None
    assert rows[0].coverage_ref == world.snapshot['evidence_ref']                # the account's durable catalogue reference
    assert rows[0].expires_at.isoformat() == world.snapshot['valid_until']       # ... and its approved expiry
    assert json.loads(rows[0].authorization_json)['terms'] == {'revision': 1, 'variant': 'standard', 'priceCents': 1200}
    assert world.fans_mock.posts == []                                           # the operator has not approved yet

    world.approve_turn(staged)
    world.unthrottle()
    assert world.pilot.tick()['dispatched'] == ['accepted']
    assert len(world.cards()) == 1
    sent = world.cards()[0]['body']
    assert sent['price'] == 12.0 and sent['mediaFiles'] == ['31', '32'] and sent['previews'] == ['31']
    assert 'USD 12.00' in sent['text']
    intent = world.rows(OfTextIntent)[0]
    assert intent.status == 'accepted' and intent.price_cents == 1200 and intent.provider_message_id == 7001
    assert world.rows(OfEngineOfferGrant)[0].used_intent_id == intent.intent_id


def test_launch_needs_an_approved_catalogue_that_is_still_the_one_the_agency_approved(world):
    with pytest.raises(Refused, match='commercial_product_contract_unavailable'):
        world.launch()                                                           # no approved product at all: nothing can sell
    assert world.rows(OfEngineOfferGrant) == []


def test_a_catalogue_changed_after_the_approval_cannot_launch(database, tmp_path, monkeypatch):
    w = build_world(database, tmp_path, monkeypatch, i=2)
    try:
        w.product('Sunset photo', [31, 32], previews=[31])
        with pytest.raises(Refused, match='changed since its approval'):
            w.launch(after_snapshot=lambda x: x.product('Later photo', [41], standard=1500))
        assert w.rows(OfEngineOfferGrant) == []
    finally:
        set_active_profiles(None)
        w.store.close()


def test_an_expired_catalogue_approval_gives_no_grant_but_the_conversation_continues(world, monkeypatch):
    from datetime import datetime
    import conversation_engine.commerce as commerce
    world.product('Sunset photo', [31, 32], previews=[31])
    world.launch()
    soon = datetime.utcnow() + timedelta(days=2)                                # past the approval's explicit expiry
    monkeypatch.setattr(commerce, 'utcnow', lambda: soon)
    result = commerce.prepare_scoped_offer(world.store, TENANT, world.account, FAN_A, 1, world.pilot.worker.engine)
    assert result == {'state': 'unavailable', 'reason': 'offer_coverage_expired', 'commercial': True}
    staged, _ = world.say('show me the sunset photo please')                     # ordinary chat is not stopped by the missing coverage
    assert staged['outcome'] == 'awaiting_approval'
    assert world.rows(OfEngineOfferGrant) == [] and 'APPROVED OFFER: none' in world.model_prompts[-1]
    world.approve_turn(staged)
    world.unthrottle()
    assert world.pilot.tick()['dispatched'] == ['accepted'] and world.cards() == []   # a plain reply went out, no card


def test_preparation_and_other_audiences_get_no_offer_authority(world):
    import conversation_engine.commerce as commerce
    world.product('Sunset photo', [31, 32], previews=[31])
    assert commerce.prepare_scoped_offer(world.store, TENANT, world.account, FAN_A, 1, None) is None     # preparation: no scope authority
    world.launch()
    outside = commerce.prepare_scoped_offer(world.store, TENANT, world.account, 999, 1, world.pilot.worker.engine)
    assert outside == {'state': 'unavailable', 'reason': 'offer_not_authorized', 'commercial': True}
    assert world.rows(OfEngineOfferGrant) == []
