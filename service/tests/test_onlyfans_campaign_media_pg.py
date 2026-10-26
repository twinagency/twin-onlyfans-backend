"""Paid campaign snapshots share canonical media/ownership/delivery owners."""
from datetime import timedelta, timezone
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, seed
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, accepted, configured, unthrottle
from test_onlyfans_campaigns_pg import add_fan, create, key
from test_onlyfans_media_pg import shelf, media_policy
from db.models import CatalogItem, ContentOffer, GrokCall, Message, Transaction, VaultItem, VaultSend
from db.onlyfans_campaign_models import OfCampaign, OfCampaignRecipient
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from onlyfans_runtime.campaigns import CampaignService
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from ownership import owned_media_in_session


from onlyfans_runtime.ledger import _coverage_receipt

def paid(database):
    store, authority = configured(database, mode="automated")
    item = shelf(database[1]); media_policy(authority)
    return store, authority, item, CampaignService(store)


def test_paid_campaign_freezes_exact_terms_and_acceptance_is_not_a_purchase(database):
    _, engine = database
    store, authority, item, service = paid(database)
    created = create(service, catalog_item_id=item)
    original = service.detail(TENANT, ACCOUNT, created["campaignId"])["campaign"]
    assert original["offer"] == {"catalogItemId": item, "label": f"Catalogue item {item}",
        "priceCents": 1000, "currency": "USD", "mediaIds": ["11", "12", "13"], "previewIds": ["11"],
        "snapshotHash": original["offer"]["snapshotHash"]}
    assert len(original["offer"]["snapshotHash"]) == 64
    identity = service.queue_due(TENANT, ACCOUNT)
    with Session(engine) as s:
        campaign = s.get(OfCampaign, (ACCOUNT, created["campaignId"]))
        intent = s.get(OfTextIntent, (ACCOUNT, identity))
        assert campaign.catalog_snapshot_json == intent.catalog_snapshot_json
        assert intent.catalog_item_id == campaign.catalog_item_id == item
    wire = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, identity, wire) == "accepted"
    assert wire.calls[0].media_ids == (11, 12, 13) and wire.calls[0].preview_ids == (11,)
    assert wire.calls[0].price_cents == 1000
    result = service.detail(TENANT, ACCOUNT, created["campaignId"])["campaign"]
    assert result["offer"] == original["offer"] and result["counts"]["accepted"] == 1
    assert result["reporting"]["revenueCents"] is None
    assert result["reporting"]["purchases"] is None
    assert result["reporting"]["revenueStatus"] == "ledger_unavailable"
    with Session(engine) as s:
        assert s.get(Message, (ACCOUNT, FAN, 9001)).is_paid is False
        assert s.scalar(select(ContentOffer)).status == "open"
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
        assert s.scalar(select(func.count()).select_from(VaultSend).where(VaultSend.was_purchased.is_(True))) == 0
        assert owned_media_in_session(s, ACCOUNT, FAN) == set()
    store.close()


@pytest.mark.parametrize("change", ["price", "description", "files", "previews", "file_hash", "removed", "version", "disabled"])
def test_catalogue_edit_after_campaign_creation_blocks_fire_without_repricing(database, change):
    _, engine = database
    store, _, item, service = paid(database)
    created = create(service, catalog_item_id=item)
    old_offer = service.detail(TENANT, ACCOUNT, created["campaignId"])["campaign"]["offer"]
    with Session(engine) as s, s.begin():
        row = s.get(CatalogItem, item)
        if change == "price": row.price_cents = 1100
        elif change == "description": row.description_for_ai = "An edited approved description."
        elif change == "files": row.media_ids = "[11,13]"
        elif change == "previews": row.preview_media_ids = "[13]"
        elif change == "file_hash": s.get(VaultItem, (ACCOUNT, 12)).content_hash = "new-bytes"
        elif change == "removed": s.get(VaultItem, (ACCOUNT, 12)).removed_at = utcnow()
        elif change == "version": row.updated_at += timedelta(seconds=1)
        else: row.enabled = False
    assert service.queue_due(TENANT, ACCOUNT) is None
    details = service.detail(TENANT, ACCOUNT, created["campaignId"])
    assert details["campaign"]["offer"] == old_offer
    assert details["recipients"][0]["reason"] == "catalogue_snapshot_changed"
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 0
    store.close()


@pytest.mark.parametrize("stage", ["queued", "admitted"])
@pytest.mark.parametrize("change", ["price", "file_hash", "owned", "media_permission"])
def test_paid_campaign_terms_and_ownership_are_rechecked_before_wire(database, stage, change):
    _, engine = database
    store, authority, item, service = paid(database)
    create(service, catalog_item_id=item)
    identity = service.queue_due(TENANT, ACCOUNT)
    assert identity
    grant = authority.admit(TENANT, ACCOUNT, identity) if stage == "admitted" else None
    with Session(engine) as s, s.begin():
        if change == "price": s.get(CatalogItem, item).price_cents = 1100
        elif change == "file_hash": s.get(VaultItem, (ACCOUNT, 12)).content_hash = "new-bytes"
        elif change == "owned": s.add(VaultSend(account_id=ACCOUNT, fan_id=FAN, media_id=12, was_purchased=True))
        else: s.get(OfDeliveryPolicy, ACCOUNT).media_enabled = False
    if stage == "queued":
        assert authority.admit(TENANT, ACCOUNT, identity) == "cancelled"
    else:
        assert authority.consume_dispatch(TENANT, grant) is None
    with Session(engine) as s:
        intent = s.get(OfTextIntent, (ACCOUNT, identity))
        assert intent.wire_started_at is None and intent.status == "cancelled"
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 0
        assert s.scalar(select(func.count()).select_from(ContentOffer)) == 0
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
    store.close()


@pytest.mark.parametrize("change", ["free_price", "below_floor", "above_ceiling", "free_teaser", "all_preview", "video_preview"])
def test_paid_campaign_refuses_free_or_invalid_catalogue_terms(database, change):
    _, engine = database
    store, _, item, service = paid(database)
    with Session(engine) as s, s.begin():
        row = s.get(CatalogItem, item)
        if change == "free_price": row.price_cents = 0
        elif change == "below_floor": row.price_cents = 299
        elif change == "above_ceiling": row.price_cents = 20001
        elif change == "free_teaser": row.is_free_teaser = True
        elif change == "all_preview": row.preview_media_ids = "[11,12,13]"
        else: row.preview_media_ids = "[12]"
    with pytest.raises(Refused):
        create(service, catalog_item_id=item)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfCampaign)) == 0
    store.close()


@pytest.mark.parametrize("cents", [300, 20000])
def test_paid_campaign_uses_exact_supported_price_boundaries(database, cents):
    _, engine = database
    store, authority, item, service = paid(database)
    with Session(engine) as s, s.begin(): s.get(CatalogItem, item).price_cents = cents
    created = create(service, catalog_item_id=item)
    identity = service.queue_due(TENANT, ACCOUNT)
    wire = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, identity, wire) == "accepted"
    assert wire.calls[0].price_cents == cents
    assert service.detail(TENANT, ACCOUNT, created["campaignId"])["campaign"]["offer"]["priceCents"] == cents
    store.close()


def test_foreign_catalogue_and_foreign_only_media_never_create_campaign(database):
    _, engine = database
    store, _, item, service = paid(database)
    other = seed(database[0], engine, account="acc-b", tenant="tenant-b")
    foreign_item = shelf(engine, account="acc-b")
    with pytest.raises(Refused): create(service, catalog_item_id=foreign_item)
    with Session(engine) as s, s.begin():
        s.add(VaultItem(account_id="acc-b", media_id=99, kind="photo", created_at=utcnow()))
        s.get(CatalogItem, item).media_ids = "[99]"
        s.get(CatalogItem, item).preview_media_ids = "[]"
    with pytest.raises(Refused): create(service, catalog_item_id=item)
    with Session(engine) as s: assert s.scalar(select(func.count()).select_from(OfCampaign)) == 0
    other.close(); store.close()


def test_owned_recipient_is_skipped_without_blocking_remaining_campaign(database):
    _, engine = database
    store, authority, item, service = paid(database)
    add_fan(engine, 102)
    with Session(engine) as s, s.begin():
        s.add(VaultSend(account_id=ACCOUNT, fan_id=FAN, media_id=12, was_purchased=True))
    created = create(service, catalog_item_id=item)
    identity = service.queue_due(TENANT, ACCOUNT)
    wire = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, identity, wire) == "accepted"
    assert wire.calls[0].fan_id == 102
    details = service.detail(TENANT, ACCOUNT, created["campaignId"])
    assert details["campaign"]["counts"]["skipped"] == details["campaign"]["counts"]["accepted"] == 1
    assert details["recipients"][0]["reason"] == "catalogue_media_already_owned"
    store.close()


def test_unknown_paid_campaign_cancel_keeps_hold_and_late_receipt_uses_frozen_terms(database):
    _, engine = database
    store, authority, item, service = paid(database)
    created = create(service, catalog_item_id=item)
    identity = service.queue_due(TENANT, ACCOUNT)
    wire = SyntheticTextTransport(ACCOUNT, outcome="accepted_lost_response")
    assert authority.dispatch(TENANT, ACCOUNT, identity, wire) == "unknown"
    service.action(TENANT, ACCOUNT, created["campaignId"], command_key=key("paid-cancel"),
        actor="synthetic-operator", expected_revision=1, action="cancel")
    with Session(engine) as s, s.begin():
        s.get(CatalogItem, item).price_cents = 2000
        assert s.get(OfTextIntent, (ACCOUNT, identity)).reservation_held
        assert s.get(OfDeliveryPolicy, ACCOUNT).active_intent_id == identity
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
        assert s.scalar(select(func.count()).select_from(ContentOffer)) == 0
    grant = wire.calls[0]
    assert authority.record_accepted(TENANT, ACCOUNT, identity, grant.token, accepted(grant)) == "accepted"
    assert authority.materialize(TENANT, ACCOUNT, identity)
    with Session(engine) as s:
        assert s.scalar(select(ContentOffer)).price_cents == 1000
        assert s.get(Message, (ACCOUNT, FAN, 9001)).price_cents == 1000
        assert s.get(Message, (ACCOUNT, FAN, 9001)).is_paid is False
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
    assert service.detail(TENANT, ACCOUNT, created["campaignId"])["campaign"]["offer"]["priceCents"] == 1000
    store.close()


def test_native_paid_campaign_and_manual_media_use_same_wire_without_model(database, monkeypatch):
    from test_onlyfans_provider_pg import native_start, fake_requests
    from test_onlyfans_provider_adapters import credential
    from onlyfans_runtime.native_worker import NativeWorker, WorkerAccount
    from onlyfans_runtime.provider_connector import NativeProviderConnector
    store, authority = native_start(database, monkeypatch)
    item = shelf(database[1])
    authority.configure(TENANT, ACCOUNT, expected_revision=2, enabled=True, limit_units=3,
        throttle_seconds=1, media_enabled=True, transport_kind="native", actor="synthetic-operator",
        reason="Mocked native paid campaign and manual media")
    calls, requests = fake_requests(database, monkeypatch)
    service = CampaignService(store)
    # This native-send fixture has a synthetic completed empty ledger scan.
    # A native paid sender must not run before purchase coverage is ready.
    from db.onlyfans_ledger_models import OfLedgerScan
    created = create(service, catalog_item_id=item)
    assert service.queue_due(TENANT, ACCOUNT) is None
    now = utcnow()
    with Session(database[1]) as s, s.begin():
        s.add(OfLedgerScan(account_id=ACCOUNT, binding_id="binding-" + ACCOUNT,
            connection_revision=1, owner_id=7001, window_start=now-timedelta(days=90), window_end=now,
            page_offset=0, status="idle", next_poll_at=now+timedelta(minutes=5),
            last_complete_at=now, last_full_at=now, updated_at=now))
        s.flush()
        scan=s.get(OfLedgerScan,ACCOUNT);scan.coverage_json=_coverage_receipt(scan)
    wire = NativeProviderConnector(credential(policy_revision=3))
    worker = NativeWorker(store, TENANT, (WorkerAccount(ACCOUNT, None, wire, wire),))
    report = worker.step()
    assert report.accepted == 1 and report.claims == report.errors == 0
    assert calls == []
    payload = json.loads(next(body for method, _, body in requests if method == "POST"))
    assert payload["mediaFiles"] == [11,12,13] and payload["previews"] == [11] and payload["price"] == 10
    revision = store.control(TENANT, ACCOUNT, FAN, mode="human_controlled", expected_revision=0,
        command_key="synthetic-manual-takeover", actor="synthetic-operator", reason="Shared native manual paid test")
    unthrottle(database[1])
    manual = authority.prepare(TENANT, ACCOUNT, FAN, command_key="native-manual-media", expected_revision=revision,
        body="Another operator-authored paid offer.", origin="manual", actor="synthetic-operator", catalog_item_id=item)
    assert authority.dispatch(TENANT, ACCOUNT, manual, wire) == "accepted"
    assert calls == [] and sum(method == "POST" for method, _, _ in requests) == 2
    assert service.detail(TENANT, ACCOUNT, created["campaignId"])["campaign"]["counts"]["accepted"] == 1
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(GrokCall)) == 0
        assert s.scalar(select(func.count()).select_from(ContentOffer)) == 2
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
        assert s.scalar(select(func.count()).select_from(VaultSend).where(VaultSend.was_purchased.is_(True))) == 0
    store.close()


def test_campaign_api_accepts_optional_paid_item_and_preserves_text_only_requests(database, monkeypatch):
    from onlyfans_commands.campaign_api import router, SCHEMA, HEADER
    store, _, item, _ = paid(database)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("ONLYFANS_CAMPAIGN_TOKEN", "synthetic-campaign-secret")
    monkeypatch.setenv("ONLYFANS_CAMPAIGN_TENANT", TENANT)
    app = FastAPI(); app.include_router(router); app.state.control_store = store
    endpoint = f"/onlyfans/campaign/v1/accounts/{ACCOUNT}/campaigns"
    headers = {"Authorization": "Bearer synthetic-campaign-secret", HEADER: SCHEMA}
    body = dict(commandKey=key("paid-api"), actor="op-session:" + "b" * 64, name="Paid campaign",
        body="The operator selected this existing paid item.", audience={"kind": "all_known"},
        scheduledAt=None, catalogItemId=item)
    with TestClient(app) as client:
        posted = client.post(endpoint, headers=headers, json=body)
        assert posted.status_code == 200
        assert client.post(endpoint, headers=headers, json=body).json() == posted.json()
        detail = client.get(endpoint + "/" + posted.json()["campaignId"], headers=headers).json()
        assert detail["campaign"]["offer"]["catalogItemId"] == item
        assert detail["campaign"]["offer"]["priceCents"] == 1000
        assert client.post(endpoint, headers=headers, json={**body, "catalogItemId": None}).status_code == 409
        assert client.post(endpoint, headers=headers, json={**body, "catalogItemId": True}).status_code == 400
        text = {name: value for name, value in body.items() if name != "catalogItemId"}
        text["commandKey"] = key("legacy-text-api")
        posted_text = client.post(endpoint, headers=headers, json=text)
        assert posted_text.status_code == 200
        text_detail = client.get(endpoint + "/" + posted_text.json()["campaignId"], headers=headers).json()
        assert text_detail["campaign"]["offer"] is None
    store.close()
