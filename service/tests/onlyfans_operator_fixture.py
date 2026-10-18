"""Actual operator UI acceptance against owned native HTTP, never real accounts."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import threading
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_models import OfRuntime
from db.models import CatalogItem
from onlyfans_native_protocol_fixture import OWNER, native_peer
from onlyfans_runtime.native_connector import NativeLoopbackConnector, from_native_server
from onlyfans_runtime.text_delivery import TextAuthority
from test_onlyfans_foundation_pg import seed, event

ACCOUNT = "acc-operator"
MANUAL = "Actual operator reply through PostgreSQL and the owned native HTTP peer."


def check_campaign_http(client, origin, cookie, engine):
    """Exercise the production Next action routes, not just backend services."""
    headers = {"Cookie": cookie, "Origin": origin, "Content-Type": "application/json",
        "Sec-Fetch-Site": "same-origin"}
    endpoint = origin + f"/api/onlyfans-campaigns/accounts/{ACCOUNT}/campaigns"
    assert client.get(endpoint).status_code == 401
    with Session(engine) as s:
        item_id = s.scalar(select(CatalogItem.id).where(CatalogItem.account_id == ACCOUNT))
    command = {"requestKey": str(uuid.uuid4()), "name": "HTTP operator campaign",
        "body": "Scheduled message through the actual dashboard and PostgreSQL.",
        "audience": {"kind": "explicit", "conversationIds": [f"{ACCOUNT}:202"]},
        "scheduledAt": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "catalogItemId": item_id}
    created = client.post(endpoint, headers=headers, json=command)
    assert created.status_code == 200, created.text
    ack = created.json()
    assert client.post(endpoint, headers=headers, json=command).json() == ack
    detail_url = endpoint + "/" + ack["campaignId"]
    revision = ack["recordedRevision"]
    for action, status in (("pause", "paused"), ("resume", "scheduled"), ("cancel", "cancelled")):
        change = {"action": action, "expectedRevision": revision}
        response = client.post(detail_url, headers=headers, json=change)
        assert response.status_code == 200, (action, response.text)
        recorded = response.json()
        assert recorded["recordedRevision"] == revision + 1
        assert client.post(detail_url, headers=headers, json=change).json() == recorded
        revision = recorded["recordedRevision"]
        detail = client.get(detail_url, headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["campaign"]["status"] == status
        assert detail.json()["campaign"]["revision"] == revision
    assert client.post(detail_url, headers={**headers, "Origin": "https://foreign.example"}, json=change).status_code == 403
    assert client.get(detail_url.replace(ACCOUNT, "other-account"), headers=headers).status_code == 404
    print("HTTP campaign PASS: authenticated production routes, paid content, persisted scheduling, exact retries, pause/resume/cancel and account isolation")


def seed_operator(database):
    url, engine = database
    store = seed(url, engine, account=ACCOUNT)
    for fan in (101, 202):
        store.record("tenant-a", ACCOUNT, event(mid=fan, fan=fan, body=f"Saved operator conversation {fan}"))
    store.drain("tenant-a", ACCOUNT)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(engine) as s, s.begin():
        runtime = s.get(OfRuntime, ACCOUNT)
        runtime.recovery_needed = False
        runtime.last_sync_at = now
        # Explicit disposable fixture evidence; this value cannot be opened as
        # credentials and cannot create a production connector.
        s.add(OfProviderConnection(account_id=ACCOUNT, binding_id="binding-" + ACCOUNT,
            owner_id=OWNER, revision=1, sealed_config="fixture-not-a-secret",
            expires_at=now + timedelta(hours=1), status="verified", verified_at=now,
            last_error=None, updated_at=now))
    TextAuthority(store).configure("tenant-a", ACCOUNT, expected_revision=0, enabled=True,
        limit_units=20, throttle_seconds=1, actor="synthetic-operator-integration",
        reason="Owned operator UI acceptance only", media_enabled=True)
    from test_onlyfans_media_pg import shelf
    item_id = shelf(engine, account=ACCOUNT)
    with Session(engine) as s, s.begin():
        s.get(CatalogItem, item_id).label = "Operator scenic collection"
    return store


@contextmanager
def operator_dispatcher(store):
    stop = threading.Event()
    failures = []
    with native_peer(history={101: [], 202: []}) as peer:
        transport = NativeLoopbackConnector(from_native_server(peer.server, account_id=ACCOUNT,
            owner_id=OWNER, policy_revision=1))
        def drain():
            try:
                authority = TextAuthority(store)
                while not stop.wait(.05):
                    with Session(store.engine) as s:
                        identity = s.scalar(select(OfTextIntent.intent_id).where(
                            OfTextIntent.account_id == ACCOUNT, OfTextIntent.status == "queued"
                        ).order_by(OfTextIntent.created_at, OfTextIntent.intent_id).limit(1))
                    if identity is not None:
                        authority.dispatch("tenant-a", ACCOUNT, identity, transport)
            except Exception as exc:
                failures.append(exc)
        thread = threading.Thread(target=drain, daemon=True)
        thread.start()
        try:
            yield peer
        finally:
            stop.set(); thread.join(10)
            assert not thread.is_alive(), "owned operator dispatcher did not stop"
            assert not failures, failures
