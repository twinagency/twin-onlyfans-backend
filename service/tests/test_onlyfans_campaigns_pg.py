"""Canonical PostgreSQL campaign lifecycle; synthetic wire and accounts only."""
from datetime import timedelta, timezone
import hashlib
import json
import multiprocessing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, seed
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured, unthrottle
from db.models import Fan, Message, OfConversationState
from db.onlyfans_campaign_models import OfCampaign, OfCampaignRecipient, OfCampaignChange
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from onlyfans_read.contract import conversation_id_for
from onlyfans_runtime.campaigns import CampaignService, InvalidCampaign, intent_key
from onlyfans_runtime.store import Refused, Store, utcnow
from onlyfans_runtime.text_delivery import Dispatch, SyntheticTextTransport, TextAuthority


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def create(service, *, command="campaign-1", audience=None, scheduled_at=None, **kw):
    return service.create(TENANT, ACCOUNT, command_key=key(command), actor="synthetic-operator",
        name="Synthetic campaign", body="An operator-authored campaign message.",
        audience=audience or {"kind": "all_known"}, scheduled_at=scheduled_at, **kw)


def add_fan(engine, fan, *, mode="automated"):
    with Session(engine) as s, s.begin():
        s.add(Fan(account_id=ACCOUNT, fan_id=fan)); s.flush()
        s.add(OfConversationState(account_id=ACCOUNT, fan_id=fan, mode=mode,
            revision=0, takeover_state="none", requires_attention=False))


def test_audience_is_frozen_idempotent_and_tenant_scoped(database):
    _, engine = database
    store, _ = configured(database, mode="automated")
    service = CampaignService(store)
    result = create(service)
    add_fan(engine, 102)
    assert create(service) == result
    details = service.detail(TENANT, ACCOUNT, result["campaignId"])
    assert details["campaign"]["recipientCount"] == 1
    assert [r["conversationId"] for r in details["recipients"]] == [conversation_id_for(ACCOUNT, FAN)]
    with pytest.raises(Refused):
        service.detail("tenant-b", ACCOUNT, result["campaignId"])
    with pytest.raises(Refused):
        service.create(TENANT, ACCOUNT, command_key=key("campaign-1"), actor="synthetic-operator",
            name="Changed", body="Changed", audience={"kind": "all_known"})
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfCampaign)) == 1
        assert s.scalar(select(func.count()).select_from(OfCampaignChange)) == 1
    store.close()


@pytest.mark.parametrize("audience", [
    {"kind": "explicit", "conversationIds": []},
    {"kind": "explicit", "conversationIds": [conversation_id_for(ACCOUNT, FAN)] * 2},
    {"kind": "explicit", "conversationIds": [conversation_id_for("acc-b", FAN)]},
    {"kind": "all_known", "extra": True},
    {"kind": "provider_subscribers"},
])
def test_invalid_audiences_never_create_campaign(database, audience):
    store, _ = configured(database, mode="automated")
    with pytest.raises(InvalidCampaign):
        create(CampaignService(store), audience=audience)
    store.close()


def test_explicit_unknown_fan_fails_without_partial_audience(database):
    _, engine = database
    store, _ = configured(database, mode="automated")
    with pytest.raises(Refused):
        create(CampaignService(store), audience={"kind": "explicit", "conversationIds": [
            conversation_id_for(ACCOUNT, FAN), conversation_id_for(ACCOUNT, 999)]})
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfCampaign)) == 0
    store.close()


def test_scheduled_campaign_waits_durably_then_queues_one_shared_intent(database, monkeypatch):
    _, engine = database
    store, authority = configured(database, mode="automated")
    service = CampaignService(store)
    target = utcnow() + timedelta(minutes=2)
    created = create(service, scheduled_at=target.replace(tzinfo=timezone.utc).isoformat())
    assert service.queue_due(TENANT, ACCOUNT) is None
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 0
    monkeypatch.setattr(TextAuthority, "_db_now", staticmethod(lambda s: target + timedelta(seconds=1)))
    identity = service.queue_due(TENANT, ACCOUNT)
    assert identity is not None
    assert service.queue_due(TENANT, ACCOUNT) is None
    with Session(engine) as s:
        row = s.get(OfTextIntent, (ACCOUNT, identity))
        recipient = s.get(OfCampaignRecipient, (ACCOUNT, created["campaignId"], FAN))
        assert row.origin == "campaign" and row.native_plan_id is None and row.work_generation is None
        assert row.command_key == intent_key(created["campaignId"], FAN)
        assert recipient.intent_id == identity and recipient.status == "queued"
        assert s.get(OfDeliveryPolicy, ACCOUNT).queued_count == 1
    store.close()


@pytest.mark.parametrize("scheduled", ["2026-10-18T22:00:00", "invalid", "99999-01-01T00:00:00Z", True])
def test_schedules_require_timezone_and_bounded_valid_date(database, scheduled):
    store, _ = configured(database, mode="automated")
    with pytest.raises(InvalidCampaign):
        create(CampaignService(store), scheduled_at=scheduled)
    store.close()


def test_no_contact_and_human_takeover_are_skipped_before_queue(database):
    _, engine = database
    store, _ = configured(database, mode="automated")
    add_fan(engine, 102, mode="no_contact")
    add_fan(engine, 103, mode="human_controlled")
    service = CampaignService(store)
    created = create(service)
    identity = service.queue_due(TENANT, ACCOUNT)
    assert identity is not None
    # Cancel only the still-queued first recipient; remaining two get evaluated.
    with Session(engine) as s, s.begin():
        s.get(OfTextIntent, (ACCOUNT, identity)).status = "cancelled"
        s.get(OfDeliveryPolicy, ACCOUNT).queued_count = 0
    assert service.queue_due(TENANT, ACCOUNT) is None
    summary = service.detail(TENANT, ACCOUNT, created["campaignId"])["campaign"]
    assert summary["counts"]["skipped"] == 2
    assert summary["status"] == "completed"
    store.close()


def test_campaign_delivery_uses_one_authority_and_human_authorship(database):
    _, engine = database
    store, authority = configured(database, mode="automated")
    service = CampaignService(store)
    created = create(service)
    identity = service.queue_due(TENANT, ACCOUNT)
    transport = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, identity, transport) == "accepted"
    assert len(transport.calls) == 1
    assert authority.dispatch(TENANT, ACCOUNT, identity, transport) == "accepted"
    assert len(transport.calls) == 1
    details = service.detail(TENANT, ACCOUNT, created["campaignId"])
    assert details["campaign"]["counts"]["accepted"] == 1
    assert details["campaign"]["status"] == "completed"
    assert details["campaign"]["reporting"]["replies"] is None
    assert details["campaign"]["reporting"]["revenueCents"] is None
    assert details["campaign"]["reporting"]["revenueStatus"] == "ledger_unavailable"
    with Session(engine) as s:
        assert s.get(Message, (ACCOUNT, FAN, 9001)).automation_kind == "onlyfans_campaign"
    store.close()


def test_worker_delivers_campaign_without_a_model_invocation(database, monkeypatch):
    from onlyfans_runtime.native_planning import SyntheticNativeModel
    from onlyfans_runtime.native_worker import NativeWorker, WorkerAccount
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setenv("DATABASE_URL", database[0])
    store, _ = configured(database, mode="automated")
    create(CampaignService(store))
    model, transport = SyntheticNativeModel(), SyntheticTextTransport(ACCOUNT)
    worker = NativeWorker(store, TENANT, (WorkerAccount(ACCOUNT, model, transport),))
    report = worker.step()
    assert report.accepted == 1 and report.claims == 0 and report.errors == 0
    assert not model.calls and len(transport.calls) == 1
    for _ in range(2):
        worker.step()
    assert not model.calls and len(transport.calls) == 1
    store.close()


@pytest.mark.parametrize("change", ["no_contact", "human_controlled", "paused", "new_input"])
def test_contact_and_input_are_rechecked_after_queue(database, change):
    _, engine = database
    store, authority = configured(database, mode="automated")
    service = CampaignService(store)
    create(service)
    identity = service.queue_due(TENANT, ACCOUNT)
    with Session(engine) as s, s.begin():
        state = s.get(OfConversationState, (ACCOUNT, FAN))
        if change == "new_input":
            state.input_revision += 1
        else:
            state.mode = change
    transport = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, identity, transport) in {"refused", "cancelled"}
    assert not transport.calls
    store.close()


def test_cancel_releases_only_queued_work_and_retains_unknown_hold(database):
    _, engine = database
    store, authority = configured(database, mode="automated")
    add_fan(engine, 102)
    service = CampaignService(store)
    created = create(service)
    identity = service.queue_due(TENANT, ACCOUNT)
    wire = SyntheticTextTransport(ACCOUNT, outcome="timeout")
    assert authority.dispatch(TENANT, ACCOUNT, identity, wire) == "unknown"
    ack = service.action(TENANT, ACCOUNT, created["campaignId"], command_key=key("cancel"),
        actor="synthetic-operator", expected_revision=1, action="cancel")
    assert ack["recordedRevision"] == 2
    assert service.action(TENANT, ACCOUNT, created["campaignId"], command_key=key("cancel"),
        actor="synthetic-operator", expected_revision=1, action="cancel") == ack
    assert service.queue_due(TENANT, ACCOUNT) is None
    details = service.detail(TENANT, ACCOUNT, created["campaignId"])
    assert details["campaign"]["counts"]["unknown"] == 1
    assert details["campaign"]["counts"]["cancelled"] == 1
    with Session(engine) as s:
        assert s.get(OfTextIntent, (ACCOUNT, identity)).reservation_held
        assert s.get(OfDeliveryPolicy, ACCOUNT).active_intent_id == identity
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id
    store.close()


def test_pause_resume_and_cancel_commands_are_revisioned_and_idempotent(database):
    _, engine = database
    store, _ = configured(database, mode="automated")
    service = CampaignService(store)
    created = create(service)
    service.action(TENANT, ACCOUNT, created["campaignId"], command_key=key("pause"),
        actor="synthetic-operator", expected_revision=1, action="pause")
    assert service.queue_due(TENANT, ACCOUNT) is None
    with pytest.raises(Refused):
        service.action(TENANT, ACCOUNT, created["campaignId"], command_key=key("stale"),
            actor="synthetic-operator", expected_revision=1, action="resume")
    service.action(TENANT, ACCOUNT, created["campaignId"], command_key=key("resume"),
        actor="synthetic-operator", expected_revision=2, action="resume")
    identity = service.queue_due(TENANT, ACCOUNT)
    assert identity
    service.action(TENANT, ACCOUNT, created["campaignId"], command_key=key("cancel"),
        actor="synthetic-operator", expected_revision=3, action="cancel")
    with Session(engine) as s:
        assert s.get(OfTextIntent, (ACCOUNT, identity)).status == "cancelled"
        assert s.get(OfDeliveryPolicy, ACCOUNT).queued_count == 0
    store.close()


def test_pause_between_grant_and_consume_prevents_wire(database):
    _, engine = database
    store, authority = configured(database, mode="automated")
    service = CampaignService(store)
    created = create(service)
    identity = service.queue_due(TENANT, ACCOUNT)
    grant = authority.admit(TENANT, ACCOUNT, identity)
    assert isinstance(grant, Dispatch)
    service.action(TENANT, ACCOUNT, created["campaignId"], command_key=key("pause"),
        actor="synthetic-operator", expected_revision=1, action="pause")
    assert authority.consume_dispatch(TENANT, grant) is None
    with Session(engine) as s:
        assert s.get(OfTextIntent, (ACCOUNT, identity)).status == "cancelled"
        assert s.get(OfDeliveryPolicy, ACCOUNT).used_units == 0
        assert s.get(OfDeliveryPolicy, ACCOUNT).active_intent_id is None
    store.close()


def test_mutated_frozen_campaign_is_not_sendable_and_audit_is_append_only(database):
    _, engine = database
    store, authority = configured(database, mode="automated")
    service = CampaignService(store)
    created = create(service)
    identity = service.queue_due(TENANT, ACCOUNT)
    with Session(engine) as s, s.begin():
        s.get(OfCampaign, (ACCOUNT, created["campaignId"])).body = "Changed without a new campaign"
    assert authority.admit(TENANT, ACCOUNT, identity) == "cancelled"
    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(DBAPIError):
            connection.execute(text("DELETE FROM of_campaign_change"))
        transaction.rollback()
    store.close()


def _queue_child(url, gate, result):
    store = Store(url)
    try:
        gate.wait(10)
        result.put(CampaignService(store).queue_due(TENANT, ACCOUNT))
    finally:
        store.close()


def test_competing_workers_create_at_most_one_recipient_intent(database):
    url, engine = database
    store, _ = configured(database, mode="automated")
    created = create(CampaignService(store))
    context = multiprocessing.get_context("spawn")
    gate, result = context.Event(), context.Queue()
    processes = [context.Process(target=_queue_child, args=(url, gate, result)) for _ in range(2)]
    for process in processes:
        process.start()
    gate.set()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    identities = [result.get(timeout=2), result.get(timeout=2)]
    assert len({value for value in identities if value is not None}) == 1
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(OfTextIntent)) == 1
        assert s.get(OfDeliveryPolicy, ACCOUNT).queued_count == 1
        assert s.get(OfCampaignRecipient, (ACCOUNT, created["campaignId"], FAN)).intent_id
    store.close()


def test_api_requires_distinct_purpose_credential_and_rejects_duplicate_json(database, monkeypatch):
    from onlyfans_commands.campaign_api import router, SCHEMA, HEADER
    store, _ = configured(database, mode="automated")
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("ONLYFANS_API_TOKEN", "read-secret")
    monkeypatch.setenv("ONLYFANS_CAMPAIGN_TOKEN", "campaign-secret")
    monkeypatch.setenv("ONLYFANS_CAMPAIGN_TENANT", TENANT)
    app = FastAPI(); app.include_router(router); app.state.control_store = store
    url = f"/onlyfans/campaign/v1/accounts/{ACCOUNT}/campaigns"
    with TestClient(app) as client:
        assert client.get(url).status_code == 401
        assert client.get(url, headers={"Authorization": "Bearer read-secret", HEADER: SCHEMA}).status_code == 401
        headers = {"Authorization": "Bearer campaign-secret", HEADER: SCHEMA, "Content-Type": "application/json"}
        bad = client.post(url, headers=headers, content='{"name":"a","name":"b"}')
        assert bad.status_code == 400
        body = dict(commandKey=key("api-create"), actor="op-session:" + "a" * 64, name="Synthetic campaign",
            body="Synthetic operator text", audience={"kind": "all_known"}, scheduledAt=None)
        response = client.post(url, headers=headers, json=body)
        assert response.status_code == 200
        ack = response.json()
        assert client.post(url, headers=headers, json=body).json() == ack
        detail = client.get(url + "/" + ack["campaignId"], headers=headers)
        assert detail.status_code == 200
        assert detail.json()["campaign"]["counts"]["pending"] == 1
        assert detail.headers["cache-control"] == "private, no-store"
        revision = ack["recordedRevision"]
        for action in ("pause", "resume", "cancel"):
            command = dict(commandKey=key("api-" + action), actor=body["actor"],
                expectedRevision=revision, action=action)
            changed = client.post(url + "/" + ack["campaignId"] + "/actions", headers=headers, json=command)
            assert changed.status_code == 200, changed.text
            result = changed.json()
            assert result["recordedRevision"] == revision + 1
            assert result["action"] == action and result["campaignId"] == ack["campaignId"]
            assert client.post(url + "/" + ack["campaignId"] + "/actions", headers=headers, json=command).json() == result
            revision = result["recordedRevision"]
        assert client.post(url, headers=headers, json={**body, "audience": {"kind": []}}).status_code == 400
        assert client.post(url, headers=headers, content=json.dumps({**body, "body": "\ud800"})).status_code == 400
        assert client.post(url, headers=headers, json={**body, "body": "invalid\x00text"}).status_code == 400
        monkeypatch.setenv("ONLYFANS_CAMPAIGN_TOKEN", "read-secret")
        assert client.get(url, headers=headers).status_code == 503
        monkeypatch.setenv("ONLYFANS_CAMPAIGN_TOKEN", "campaign-secret")
        for purpose in ("ONLYFANS_CONTENT_TOKEN", "ONLYFANS_CONNECTION_TOKEN"):
            monkeypatch.setenv(purpose, "campaign-secret")
            assert client.get(url, headers=headers).status_code == 503
            monkeypatch.delenv(purpose)
    store.close()
