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

from commercial_worker_fixture import FAN_A, FAN_B, OPERATOR, World, build_world, key  # noqa: F401
from conversation_engine import approval, pilot_scope
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineOfferGrant
from onlyfans_runtime.store import Refused
from test_commercial_preparation_pg import TENANT
from test_onlyfans_foundation_pg import database  # noqa: F401
from conversation_engine.writer_context import set_active_profiles


@pytest.fixture
def world(database, tmp_path, monkeypatch):
    w = build_world(database, tmp_path, monkeypatch)
    yield w
    set_active_profiles(None)
    w.store.close()


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
