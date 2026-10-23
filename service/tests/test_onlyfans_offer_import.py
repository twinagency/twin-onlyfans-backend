"""Pure offer-term parsing, grouping and price tests; no database or network."""
import json

import pytest

from onlyfans_runtime.native_protocol import NativeProtocolError, message
from onlyfans_runtime.offer_terms import OfferObservation, from_history, media_key
from onlyfans_runtime.store import Refused

OWNER, FAN = 7001, 501


def row(**changes):
    base = {"id": 900, "fromUser": {"id": OWNER}, "createdAt": "2026-10-01T10:00:00+10:00", "text": "<p>hi</p>",
            "media": [{"id": 11}, {"id": 12}], "mediaCount": 2, "price": "19.99", "responseType": "message",
            "previews": [11], "lockedText": False, "isFromQueue": False}
    base.update(changes)
    return {k: v for k, v in base.items() if v is not ...}


def observe(value):
    return from_history(value, message(value, owner_id=OWNER, fan_id=FAN, history=True))


@pytest.mark.parametrize("price,cents", [("3", 300), ("10.07", 1007), ("19.99", 1999), ("200", 20000), (19.99, 1999)])
def test_exact_cents_from_source(price, cents):
    assert observe(row(price=price)).price_cents == cents


@pytest.mark.parametrize("price", ["10.075", "-1", "nan", "1e400", True, None])
def test_malformed_money_refuses_the_row(price):
    with pytest.raises(NativeProtocolError):
        observe(row(price=price))


def test_event_identity_and_payload_unchanged_by_offer_capture():
    value = row()
    event = message(value, owner_id=OWNER, fan_id=FAN, history=True)
    stripped = {k: v for k, v in value.items() if k not in {"previews", "lockedText", "isFromQueue"}}
    assert event.payload() == message(stripped, owner_id=OWNER, fan_id=FAN, history=True).payload()
    assert json.loads(event.payload()) == {"body": "hi", "created_at": "2026-10-01T00:00:00+00:00", "direction": "out",
        "fan_id": FAN, "media_ids": [11, 12], "message_id": 900, "price_cents": 1999}


def test_unknown_is_distinct_from_explicit_empty_and_false():
    explicit = observe(row(previews=[], lockedText=False))
    assert explicit.preview_ids == () and explicit.locked_text is False and explicit.reasons == ()
    unknown = observe(row(previews=..., lockedText=...))
    assert unknown.preview_ids is None and unknown.locked_text is None
    assert unknown.reasons == ("locked_text_unknown", "previews_unknown")
    assert observe(row(lockedText=True)).reasons == ("locked_text",)
    assert observe(row(previews=[13])).reasons == ("previews_unknown", "unsupported_previews")


def test_shape_reasons_and_non_offers():
    assert observe(row(previews=[11, 12])).reasons == ("no_paid_content",)
    assert observe(row(price="1")).reasons == ("price_out_of_range",)
    many = [{"id": i} for i in range(1, 22)]
    assert "too_many_attachments" in observe(row(media=many, mediaCount=21, previews=[])).reasons
    assert observe(row(price="0")) is None
    assert observe(row(media=[], mediaCount=0, previews=[])) is None
    incoming = row(fromUser={"id": FAN})
    assert observe(incoming) is None, "inbound fan attachments are not creator products"


def test_fingerprint_covers_order_previews_lock_price_and_context():
    base = observe(row())
    assert base.source_context == "direct" and observe(row(isFromQueue=True)).source_context == "queue"
    variants = [row(media=[{"id": 12}, {"id": 11}]), row(previews=[]), row(lockedText=True), row(price="20"),
                row(isFromQueue=True)]
    prints = {observe(v).fingerprint() for v in variants} | {base.fingerprint()}
    assert len(prints) == 6
    assert media_key([12, 11]) == media_key([11, 12]) == base.media_key


def test_observation_validation_rejects_forgery():
    good = observe(row())
    for patch in [{"source_kind": "post2"}, {"media_ids": (11, 11)}, {"preview_ids": (99,)},
                  {"price_cents": 0}, {"currency": "EUR"}, {"source_at": "2026-10-01T10:00:00"},
                  {"reasons": ("made_up",)}]:
        with pytest.raises(Refused):
            OfferObservation(**{**good.__dict__, **patch}).normalized()
