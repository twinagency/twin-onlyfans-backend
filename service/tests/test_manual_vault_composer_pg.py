"""Manual vault sends on disposable PostgreSQL; no provider or production credentials."""
import json
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_onlyfans_vault_pg import sync
from test_onlyfans_connections_pg import TENANT, ACTOR
from db.models import Fan, OfConversationState, CatalogItem, Message, VaultItem, VaultSend, ContentOffer
from db.onlyfans_models import OfRuntime
from db.onlyfans_delivery_models import OfTextIntent
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.manual_media import normalize
from onlyfans_runtime.text_delivery import TextAuthority, SyntheticTextTransport

FAN = 101

def prepared(database, monkeypatch):
    store, account, _, _ = sync(database, monkeypatch)
    with Session(database[1]) as s, s.begin():
        runtime = s.get(OfRuntime, account)
        runtime.enabled = True
        runtime.recovery_needed = False
        runtime.last_sync_at = utcnow()
        s.add(Fan(account_id=account, fan_id=FAN)); s.flush()
        s.add(OfConversationState(account_id=account, fan_id=FAN, mode="human_controlled",
            revision=0, takeover_state="none", requires_attention=False))
    authority = TextAuthority(store)
    authority.configure(TENANT, account, expected_revision=0, enabled=True, media_enabled=True,
        limit_units=20, throttle_seconds=1, actor=ACTOR, reason="Synthetic manual vault test")
    return store, authority, account

def spec(price=1234):
    return {"mediaIds":["11","13"],"previewIds":["11"] if price else [],"priceCents":price}

def prepare(authority, account, value, key="manual-media-one", body="Selected scenic media."):
    return authority.prepare(TENANT, account, FAN, command_key=key, expected_revision=0,
        body=body, origin="manual", actor=ACTOR, manual_media=value)

@pytest.mark.parametrize("value", [
    {"mediaIds":[],"previewIds":[],"priceCents":500},
    {"mediaIds":["11","11"],"previewIds":[],"priceCents":500},
    {"mediaIds":[11],"previewIds":[],"priceCents":500},
    {"mediaIds":["11"],"previewIds":["12"],"priceCents":500},
    {"mediaIds":["11"],"previewIds":["11"],"priceCents":500},
    {"mediaIds":["11"],"previewIds":["11"],"priceCents":0},
    {"mediaIds":["11"],"previewIds":[],"priceCents":True},
    {"mediaIds":["11"],"previewIds":[],"priceCents":299},
    {"mediaIds":["11"],"previewIds":[],"priceCents":20001},
    {"mediaIds":["11"],"previewIds":[],"priceCents":500,"url":"https://untrusted.invalid"},
    {"mediaIds":["9223372036854775808"],"previewIds":[],"priceCents":500},
])
def test_invalid_terms_are_refused(value):
    with pytest.raises(Refused): normalize(value)

@pytest.mark.parametrize("price", [0, 300, 1234, 20000])
def test_explicit_price_files_previews_and_exactly_once_receipt(database, monkeypatch, price):
    store, authority, account = prepared(database, monkeypatch)
    identity = prepare(authority, account, spec(price))
    assert prepare(authority, account, spec(price)) == identity
    with Session(database[1]) as s:
        row = s.get(OfTextIntent, (account, identity))
        assert row.price_cents == price and json.loads(row.media_json) == [11,13]
        assert s.get(CatalogItem,row.catalog_item_id).enabled is False
        assert s.scalar(select(func.count()).select_from(CatalogItem)) == 1
    wire = SyntheticTextTransport(account)
    assert authority.dispatch(TENANT,account,identity,wire) == "accepted"
    assert authority.dispatch(TENANT,account,identity,wire) == "accepted"
    assert len(wire.calls) == 1
    assert wire.calls[0].price_cents == price and wire.calls[0].media_ids == (11,13)
    with Session(database[1]) as s:
        message = s.get(Message,(account,FAN,9001))
        assert message.price_cents == price and message.media_count == 2
        assert s.scalar(select(func.count()).select_from(VaultSend)) == 2
        assert s.scalar(select(func.count()).select_from(ContentOffer)) == (1 if price else 0)
    store.close()

def test_changed_file_is_not_sent(database,monkeypatch):
    store,authority,account=prepared(database,monkeypatch)
    identity=prepare(authority,account,spec())
    with Session(database[1]) as s,s.begin():
        s.get(VaultItem,(account,13)).removed_at=utcnow()
    wire=SyntheticTextTransport(account)
    assert authority.dispatch(TENANT,account,identity,wire) == "cancelled"
    assert wire.calls == []
    store.close()

def test_price_change_cannot_reuse_command_and_ai_cannot_use_manual_media(database,monkeypatch):
    store,authority,account=prepared(database,monkeypatch)
    prepare(authority,account,spec())
    with pytest.raises(Refused,match="idempotency"):
        prepare(authority,account,spec(1300))
    with pytest.raises(Refused,match="invalid manual media selection"):
        authority.prepare(TENANT,account,FAN,command_key="not-ai",expected_revision=0,
            body="Scenic media.",origin="automated",actor=ACTOR,manual_media=spec())
    store.close()

def test_ambiguous_send_is_never_retried(database,monkeypatch):
    store,authority,account=prepared(database,monkeypatch)
    identity=prepare(authority,account,spec())
    wire=SyntheticTextTransport(account,outcome="accepted_lost_response")
    assert authority.dispatch(TENANT,account,identity,wire) == "unknown"
    assert authority.dispatch(TENANT,account,identity,wire) == "unknown"
    assert len(wire.calls) == 1
    store.close()

@pytest.mark.parametrize("price", [0, 1200])
def test_media_without_caption(database, monkeypatch, price):
    store, authority, account = prepared(database, monkeypatch)
    identity = prepare(authority, account, spec(price), body="")
    wire = SyntheticTextTransport(account)
    assert authority.dispatch(TENANT, account, identity, wire) == "accepted"
    assert len(wire.calls) == 1 and wire.calls[0].body == ""
    from onlyfans_runtime.native_protocol import request
    payload = json.loads(request(wire.calls[0]))
    assert payload["mediaFiles"] == [11, 13] and payload["price"] == price / 100
    store.close()
