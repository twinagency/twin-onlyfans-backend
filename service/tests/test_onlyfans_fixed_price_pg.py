"""Fixed catalogue prices survive selection, caption, intent, wire and receipt.

Offline PostgreSQL and the existing loopback/synthetic transports only.
"""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT, FAN
from test_onlyfans_media_pg import ASK, intent, manual, native, no_commerce
from db.models import CatalogItem, ContentOffer, LadderQuote, LadderState, Message
from db.onlyfans_delivery_models import OfNativePlan, OfTextIntent
from onlyfans_runtime import media_delivery, native_sales
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.native_protocol import cents, request
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import Dispatch, SyntheticTextTransport


def fix(engine, item, price):
    with Session(engine) as s, s.begin():
        row = s.get(CatalogItem, item)
        row.pricing_mode, row.price_cents = "fixed", price


@pytest.mark.parametrize("price,wire", [(300, "3.0"), (1007, "10.07"), (1999, "19.99"), (20000, "200.0")])
def test_exact_cents_round_trip_through_serialized_request(price, wire):
    dispatch = Dispatch(ACCOUNT, FAN, "intent", "attempt", "token", "Body text", "hash", "manual", utcnow(),
                        media_ids=(11, 12), preview_ids=(11,), price_cents=price)
    raw = request(dispatch)
    body = json.loads(raw)
    assert f'"price":{wire}'.encode() in raw and cents(body["price"]) == price


@pytest.mark.parametrize("rung", [0, 3])
def test_native_fixed_sale_keeps_saved_amount_everywhere(database, monkeypatch, rung):
    _, engine = database
    store, authority, claim, planner, item = native(database, monkeypatch)
    fix(engine, item, 1007)
    with Session(engine) as s, s.begin():
        s.add(LadderState(account_id=ACCOUNT, fan_id=FAN, rung_index=rung))
    model = SyntheticNativeModel("Here is the available choice.", sale={"item_id": item, "quote": ASK})
    plan = planner.prepare(TENANT, claim, model)
    with Session(engine) as s:
        saved = s.get(OfNativePlan, (ACCOUNT, plan))
        assert saved.status == "ready", saved.reason
        sale = json.loads(saved.sale_json)
        assert sale["price_cents"] == 1007 and "native_quote" not in sale
        assert sale["item"]["pricing_mode"] == "fixed"
        assert "USD $10.07 to unlock" in saved.body
    identity = planner.queue(TENANT, ACCOUNT, plan)
    with Session(engine) as s:
        row = s.get(OfTextIntent, (ACCOUNT, identity))
        assert row.price_cents == 1007 and json.loads(row.catalog_snapshot_json)["price_cents"] == 1007
    wire = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, identity, wire) == "accepted"
    assert wire.calls[0].price_cents == 1007
    with Session(engine) as s:
        assert s.scalar(select(ContentOffer)).price_cents == 1007
        quote = s.scalar(select(LadderQuote))
        assert (quote.kind, quote.price_cents, quote.base_cents, quote.pre_clamp_cents, quote.arm_mult,
                quote.band_lo, quote.band_hi, quote.paid) == ("fixed", 1007, 1007, 1007, 1.0, 0, 0, False)
        lad = s.get(LadderState, (ACCOUNT, FAN))
        assert lad.rung_index == rung, "a fixed offer never advances the dynamic ladder"
        assert lad.last_ask_at is not None, "it still counts as an ask for pacing"
        assert s.get(Message, (ACCOUNT, FAN, 9001)).price_cents == 1007
    store.close()


def test_native_fixed_selection_ignores_quote_arithmetic(database, monkeypatch):
    _, engine = database
    store, authority, claim, planner, item = native(database, monkeypatch)
    fix(engine, item, 1999)
    from automations import ai_chatter
    monkeypatch.setattr(ai_chatter, "_quote_item", lambda *a, **k: pytest.fail("fixed items are never quoted"))
    with Session(engine) as s:
        assert native_sales.choice(s, ACCOUNT, FAN, item, utcnow())["price_cents"] == 1999
    store.close()


def test_manual_override_must_equal_fixed_amount(database):
    _, engine = database
    store, authority, item = manual(database)
    fix(engine, item, 1999)
    with pytest.raises(Refused, match="sticker price|fixed catalogue price differs"):
        authority.prepare(TENANT, ACCOUNT, FAN, command_key="media-x", expected_revision=0, body="Set.",
            origin="manual", actor="synthetic-operator", catalog_item_id=item, quoted_price_cents=2500)
    identity = intent(authority, item)
    wire = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, identity, wire) == "accepted"
    assert wire.calls[0].price_cents == 1999
    with Session(engine) as s:
        assert s.scalar(select(LadderQuote)) is None, "manual sends keep their existing quote behaviour"
    store.close()


@pytest.mark.parametrize("change", ["price", "mode"])
def test_fixed_terms_changed_after_intent_cancel_before_wire(database, change):
    _, engine = database
    store, authority, item = manual(database)
    fix(engine, item, 1999)
    identity = intent(authority, item)
    with Session(engine) as s, s.begin():
        row = s.get(CatalogItem, item)
        if change == "price":
            row.price_cents = 2000
        else:
            row.pricing_mode = "dynamic"
    assert authority.admit(TENANT, ACCOUNT, identity) == "cancelled"
    no_commerce(engine)
    store.close()


def test_dynamic_snapshot_is_byte_identical_to_before(database):
    _, engine = database
    store, authority, item = manual(database)
    with Session(engine) as s:
        terms = media_delivery.catalog_terms(s, ACCOUNT, item)
    assert set(terms) == {"item", "media_ids", "preview_ids", "price_cents", "files"}
    assert "pricing_mode" not in terms["item"], "queued dynamic intents and campaigns stay valid"
    store.close()
