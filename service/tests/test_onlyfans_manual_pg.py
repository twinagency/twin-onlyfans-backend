"""Operator manual-message API over real PG and the common intent ledger."""
import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, seed
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured
from db.models import (CatalogItem, ContentOffer, Fan, GrokCall, Message,
    OfConversationState, Transaction, VaultSend)
from db.onlyfans_delivery_models import OfTextIntent
from onlyfans_read.contract import conversation_id_for
from onlyfans_runtime.text_delivery import TextAuthority, SyntheticTextTransport


def context(database, monkeypatch, *, mode="human_controlled", enabled=True):
    from onlyfans_commands.api import router
    from onlyfans_commands.settings import HEADER, SCHEMA
    store, authority = configured(database, mode=mode)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("ONLYFANS_API_TOKEN", "synthetic-read-token")
    monkeypatch.setenv("ONLYFANS_CONTROL_TOKEN", "synthetic-control-token")
    monkeypatch.setenv("ONLYFANS_CONTROL_TENANT", TENANT)
    monkeypatch.setenv("ONLYFANS_MANUAL_ENABLED", "1" if enabled else "0")
    app = FastAPI(); app.include_router(router); app.state.control_store = store
    headers = {"Authorization": "Bearer synthetic-control-token", HEADER: SCHEMA}
    url = f"/onlyfans/control/v1/accounts/{ACCOUNT}/conversations/{conversation_id_for(ACCOUNT, FAN)}"
    body = dict(action="manual_message", expectedRevision=0,
        commandKey=hashlib.sha256(b"synthetic-manual-1").hexdigest(),
        actor="op-session:" + "a" * 64, body="An operator-authored reply.")
    return store, authority, app, headers, url, body


def test_manual_ack_is_queued_and_exact_retry_never_creates_another_send(database, monkeypatch):
    store, authority, app, headers, url, body = context(database, monkeypatch)
    with Session(database[1]) as s, s.begin():
        s.get(OfConversationState, (ACCOUNT, FAN)).takeover_state = "effective"
    with TestClient(app) as client:
        first = client.post(url, headers=headers, json=body)
        assert first.status_code == 200
        ack = first.json()
        assert ack["outcome"] == "queued" and ack["intentId"] and ack["recordedRevision"] == 0
        assert client.post(url, headers=headers, json=body).json() == ack
        with Session(database[1]) as s:
            assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 1
            assert s.scalar(select(func.count()).select_from(Message)) == 0
        wire = SyntheticTextTransport(ACCOUNT)
        assert authority.dispatch(TENANT, ACCOUNT, ack["intentId"], wire) == "accepted"
        # An acknowledgement is historical evidence, not an instruction to send
        # again or a current-state badge. Its exact retry remains the same.
        assert client.post(url, headers=headers, json=body).json() == ack
        assert len(wire.calls) == 1
        changed = {**body, "body": "Different text with the same request key."}
        assert client.post(url, headers=headers, json=changed).status_code == 409
        with Session(database[1]) as s:
            assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 1
            assert s.scalar(select(func.count()).select_from(Message).where(Message.direction == "out")) == 1
    store.close()


def test_lost_manual_ack_retry_recovers_same_durable_intent(database, monkeypatch):
    store, _, app, headers, url, body = context(database, monkeypatch)
    original = TextAuthority.prepare
    called = []
    def lose_first_ack(self, *args, **kwargs):
        identity = original(self, *args, **kwargs)
        called.append(identity)
        if len(called) == 1:
            raise RuntimeError("Synthetic response lost after commit")
        return identity
    monkeypatch.setattr(TextAuthority, "prepare", lose_first_ack)
    with TestClient(app) as client:
        assert client.post(url, headers=headers, json=body).status_code == 500
        retry = client.post(url, headers=headers, json=body)
        assert retry.status_code == 200
        assert called == [retry.json()["intentId"]] * 2
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 1
        assert s.scalar(select(func.count()).select_from(Message)) == 0
    store.close()


@pytest.mark.parametrize("mode", ["automated", "paused", "no_contact"])
def test_manual_requires_effective_human_control_and_respects_no_contact(database, monkeypatch, mode):
    store, _, app, headers, url, body = context(database, monkeypatch, mode=mode)
    with TestClient(app) as client:
        assert client.post(url, headers=headers, json=body).status_code == 409
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
    store.close()


@pytest.mark.parametrize("change", ["stale_revision", "takeover_requested", "muted"])
def test_manual_rejects_stale_or_blocked_operator_snapshot(database, monkeypatch, change):
    store, _, app, headers, url, body = context(database, monkeypatch)
    with Session(database[1]) as s, s.begin():
        if change == "stale_revision":
            s.get(OfConversationState, (ACCOUNT, FAN)).revision = 1
        elif change == "takeover_requested":
            s.get(OfConversationState, (ACCOUNT, FAN)).takeover_state = "requested"
        else:
            s.get(Fan, (ACCOUNT, FAN)).is_muted = True
    with TestClient(app) as client:
        assert client.post(url, headers=headers, json=body).status_code == 409
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
    store.close()


def test_manual_purpose_gate_foreign_scope_and_strict_json(database, monkeypatch):
    store, _, app, headers, url, body = context(database, monkeypatch, enabled=False)
    other = seed(database[0], database[1], account="acc-b", tenant="tenant-b")
    with TestClient(app) as client:
        assert client.post(url, headers=headers, json=body).status_code == 503
        monkeypatch.setenv("ONLYFANS_MANUAL_ENABLED", "1")
        assert client.post(url, headers={**headers, "Authorization": "Bearer synthetic-read-token"}, json=body).status_code == 401
        foreign = f"/onlyfans/control/v1/accounts/acc-b/conversations/{conversation_id_for('acc-b', FAN)}"
        assert client.post(foreign, headers=headers, json=body).status_code == 404
        raw_headers = {**headers, "Content-Type": "application/json"}
        assert client.post(url, headers=raw_headers, content='{"action":"manual_message","action":"manual_message"}').status_code == 400
        assert client.post(url, headers=raw_headers, content='{"expectedRevision":NaN}').status_code == 400
        assert client.post(url, headers=headers, json={**body, "body": "x" * 4001}).status_code == 400
        assert client.post(url, headers=headers, json={**body, "extra": True}).status_code == 400
    other.close(); store.close()


def test_paid_manual_api_uses_native_wire_without_model_or_purchase(database, monkeypatch):
    from test_onlyfans_media_pg import shelf
    from test_onlyfans_provider_pg import fake_requests
    from test_onlyfans_provider_adapters import credential
    from onlyfans_runtime.provider_connector import NativeProviderConnector
    from ownership import owned_media_in_session
    store, authority, app, headers, url, body = context(database, monkeypatch)
    item = shelf(database[1])
    authority.configure(TENANT, ACCOUNT, expected_revision=1, enabled=True, media_enabled=True,
        transport_kind="native", limit_units=20, throttle_seconds=1,
        actor="synthetic-operator", reason="Native paid manual API acceptance")
    payload = {**body, "catalogItemId": item}
    calls, requests = fake_requests(database, monkeypatch)
    with TestClient(app) as client:
        queued = client.post(url, headers=headers, json=payload)
        assert queued.status_code == 200
        ack = queued.json()
        assert ack["outcome"] == "queued"
        assert client.post(url, headers=headers, json=payload).json() == ack
        with Session(database[1]) as s:
            intent = s.get(OfTextIntent, (ACCOUNT, ack["intentId"]))
            assert intent.catalog_item_id == item and intent.price_cents == 1000
            assert intent.media_json == "[11,12,13]" and intent.preview_json == "[11]"
            assert s.scalar(select(func.count()).select_from(Message)) == 0
            assert s.scalar(select(func.count()).select_from(ContentOffer)) == 0
        monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
        monkeypatch.setenv("DATABASE_URL", database[0])
        wire = NativeProviderConnector(credential())
        assert authority.dispatch(TENANT, ACCOUNT, ack["intentId"], wire) == "accepted"
        assert calls == [] and sum(method == "POST" for method, _, _ in requests) == 1
        import json
        sent = json.loads(next(raw for method, _, raw in requests if method == "POST"))
        assert sent["mediaFiles"] == [11, 12, 13] and sent["previews"] == [11] and sent["price"] == 10
        monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
        assert client.post(url, headers=headers, json=payload).json() == ack
        assert client.post(url, headers=headers, json={**payload, "catalogItemId": None}).status_code == 409
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 1
        assert s.get(Message, (ACCOUNT, FAN, 9001)).is_paid is False
        assert s.scalar(select(ContentOffer)).status == "open"
        assert s.scalar(select(func.count()).select_from(VaultSend)) == 3
        assert s.scalar(select(func.count()).select_from(VaultSend).where(VaultSend.was_purchased.is_(True))) == 0
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
        assert s.scalar(select(func.count()).select_from(GrokCall)) == 0
        assert owned_media_in_session(s, ACCOUNT, FAN) == set()
    store.close()


def test_manual_paid_api_rejects_foreign_item_and_unapproved_media_without_partial_intent(database, monkeypatch):
    from test_onlyfans_media_pg import shelf, media_policy
    store, authority, app, headers, url, body = context(database, monkeypatch)
    item = shelf(database[1]); media_policy(authority)
    other = seed(database[0], database[1], account="acc-b", tenant="tenant-b")
    foreign = shelf(database[1], account="acc-b")
    with TestClient(app) as client:
        assert client.post(url, headers=headers, json={**body, "catalogItemId": foreign}).status_code == 409
        with Session(database[1]) as s, s.begin(): s.get(CatalogItem, item).is_free_teaser = True
        assert client.post(url, headers=headers, json={**body, "catalogItemId": item}).status_code == 409
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
        assert s.scalar(select(func.count()).select_from(Message)) == 0
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
    other.close(); store.close()


@pytest.mark.parametrize("stage", ["queued", "admitted"])
def test_manual_paid_api_price_change_cannot_refresh_or_consume_frozen_offer(database, monkeypatch, stage):
    from test_onlyfans_media_pg import shelf, media_policy
    store, authority, app, headers, url, body = context(database, monkeypatch)
    item = shelf(database[1]); media_policy(authority)
    payload = {**body, "catalogItemId": item}
    with TestClient(app) as client:
        queued = client.post(url, headers=headers, json=payload)
        assert queued.status_code == 200
        ack = queued.json()
        grant = authority.admit(TENANT, ACCOUNT, ack["intentId"]) if stage == "admitted" else None
        with Session(database[1]) as s, s.begin(): s.get(CatalogItem, item).price_cents = 1100
        if grant is None:
            assert authority.admit(TENANT, ACCOUNT, ack["intentId"]) == "cancelled"
        else:
            assert authority.consume_dispatch(TENANT, grant) is None
        assert client.post(url, headers=headers, json=payload).json() == ack
    with Session(database[1]) as s:
        intent = s.get(OfTextIntent, (ACCOUNT, ack["intentId"]))
        assert intent.price_cents == 1000 and intent.status == "cancelled" and intent.wire_started_at is None
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 1
        assert s.scalar(select(func.count()).select_from(Message)) == 0
        assert s.scalar(select(func.count()).select_from(ContentOffer)) == 0
    store.close()


def test_manual_optional_item_validation_and_legacy_text_shape(database, monkeypatch):
    store, _, app, headers, url, body = context(database, monkeypatch)
    with TestClient(app) as client:
        for item in (True, 0, -1, 2**31, "catalog-1", [], {}):
            assert client.post(url, headers=headers, json={**body, "catalogItemId": item}).status_code == 400
        for extra in ({"priceCents": 1}, {"mediaIds": [11]}, {"previews": [11]}):
            assert client.post(url, headers=headers, json={**body, **extra}).status_code == 400
        assert client.post(url, headers=headers, json={**body, "body": "invalid\x00text"}).status_code == 400
        first = client.post(url, headers=headers, json=body)
        assert first.status_code == 200
        # Optional null does not reinterpret or fork the old text-only intent.
        assert client.post(url, headers=headers, json={**body, "catalogItemId": None}).json() == first.json()
        with Session(database[1]) as s:
            intent = s.get(OfTextIntent, (ACCOUNT, first.json()["intentId"]))
            assert intent.catalog_item_id is None and intent.media_json == "[]" and intent.price_cents == 0
            assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 1
    store.close()


def test_manual_paid_api_lost_ack_keeps_item_bound_to_same_intent(database, monkeypatch):
    from test_onlyfans_media_pg import shelf, media_policy
    store, authority, app, headers, url, body = context(database, monkeypatch)
    item = shelf(database[1]); media_policy(authority)
    payload = {**body, "catalogItemId": item}
    original, called = TextAuthority.prepare, []
    def lose_first_ack(self, *args, **kwargs):
        identity = original(self, *args, **kwargs)
        called.append(identity)
        if len(called) == 1: raise RuntimeError("Synthetic paid acknowledgement lost after commit")
        return identity
    monkeypatch.setattr(TextAuthority, "prepare", lose_first_ack)
    with TestClient(app) as client:
        assert client.post(url, headers=headers, json=payload).status_code == 500
        recovered = client.post(url, headers=headers, json=payload)
        assert recovered.status_code == 200
        assert called == [recovered.json()["intentId"]] * 2
        assert client.post(url, headers=headers, json=body).status_code == 409
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 1
        intent = s.get(OfTextIntent, (ACCOUNT, recovered.json()["intentId"]))
        assert intent.catalog_item_id == item and intent.price_cents == 1000
        assert s.scalar(select(func.count()).select_from(Message)) == 0
    store.close()
