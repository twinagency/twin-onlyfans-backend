"""Native transaction fixtures and owned PostgreSQL; no real account or network."""
import json
import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import verified, TENANT
from test_onlyfans_provider_adapters import OWNER
from test_onlyfans_media_pg import manual, intent
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN
from db.models import Fan, Message, Transaction, VaultSend
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_ledger_models import OfLedgerEvidence, OfLedgerScan
from onlyfans_runtime.commerce import held
from onlyfans_runtime.ledger import parse_page, process_ledger_pages, apply_observation
from onlyfans_runtime.native_protocol import NativeProtocolError
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import utcnow, Refused
from onlyfans_runtime.text_delivery import SyntheticTextTransport


def transaction(**changes):
    result = dict(id="native-tx-1", user={"id": 101}, amount="10.00", currency="USD", status="done",
        createdAt=(utcnow() - timedelta(minutes=1)).isoformat() + "Z",
        description="Payment for message from Synthetic Fan")
    result.update(changes)
    return result


def payload(rows, more=False):
    return json.dumps({"list": rows, "hasMore": more}).encode()


def parsed(row):
    now = utcnow()
    return parse_page(payload([row]), owner_id=OWNER, start=now-timedelta(days=90), end=now)[0][0]


@pytest.mark.parametrize("key,value", [("status", None), ("status", "invented"), ("amount", "1.005"),
    ("amount", True), ("currency", None), ("currency", "AUD"), ("createdAt", "2026-10-18T10:00:00"),
    ("user", {"id": True}), ("ownerId", OWNER+1), ("messageId", "123"),
    ("description", "Unrecognised event"), ("payoutPendingDays", -1)])
def test_parser_requires_explicit_money_status_identity_and_timestamp(key, value):
    row = transaction(); row[key] = value
    with pytest.raises((NativeProtocolError, Refused)):
        parsed(row)


def test_parser_rejects_duplicate_keys_provider_ids_and_partial_page():
    now = utcnow()
    for raw in [b'{"list":[],"list":[],"hasMore":false}', payload([transaction(), transaction()]),
                payload([transaction(), {"id": "broken"}]), payload([], True), b" "*65537]:
        with pytest.raises(NativeProtocolError):
            parse_page(raw, owner_id=OWNER, start=now-timedelta(days=90), end=now)
    row = transaction(); row["amount"] = "10.01"
    assert parsed(row)["amount_cents"] == 1001


def record(store, account, observation):
    with Session(store.engine) as s, s.begin():
        store._runtime(s, TENANT, account)
        return apply_observation(s, account, observation, now=utcnow())


def test_duplicate_pending_cleared_refund_and_stale_page_change_money_once(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    row = transaction(); row["status"] = "loading"
    first = parsed(row)
    assert record(store, account, first) == "applied"
    assert record(store, account, first) == "duplicate"
    paid = {**first, "status": "cleared"}
    assert record(store, account, paid) == "applied"
    refund = {**paid, "status": "refunded"}
    assert record(store, account, refund) == "applied"
    assert record(store, account, refund) == "duplicate"
    stale = {**paid, "description": "Payment for message from corrected display name"}
    assert record(store, account, stale) == "stale"
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 1
        assert s.get(Fan, (account, 101)).lifetime_spend_cents == 0
        assert s.scalar(select(Transaction.status)) == "refunded"
        assert s.scalar(select(func.count()).select_from(OfLedgerEvidence)) == 4
    with pytest.raises(DBAPIError), database[1].begin() as c:
        c.exec_driver_sql("DELETE FROM of_ledger_evidence")
    store.close()


def test_unlinked_paid_media_holds_sales_and_changed_amount_is_a_conflict(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    observation = parsed(transaction())
    assert record(store, account, observation) == "applied"
    with Session(database[1]) as s:
        assert held(s, account)
        assert s.scalar(select(Transaction.message_id)) is None
        assert s.scalar(select(func.count()).select_from(Message)) == 0
    assert record(store, account, {**observation, "amount_cents": 1100}) == "conflict"
    with Session(database[1]) as s:
        assert s.scalar(select(Transaction.amount_cents)) == 1000
        assert s.get(Fan, (account, 101)).lifetime_spend_cents == 1000
    store.close()


def test_exact_message_evidence_settles_existing_media_and_refund(database, monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    store, authority, item = manual(database)
    identity = intent(authority, item)
    assert authority.dispatch(TENANT, ACCOUNT, identity, SyntheticTextTransport(ACCOUNT)) == "accepted"
    observation = parsed(transaction())
    observation.update(fan_id=FAN, message_id=9001, occurred_at=utcnow().isoformat())
    assert record(store, ACCOUNT, observation) == "applied"
    with Session(database[1]) as s:
        assert s.get(Message, (ACCOUNT, FAN, 9001)).is_paid is True
        assert all(s.scalars(select(VaultSend.was_purchased)))
        assert not held(s, ACCOUNT)
    assert record(store, ACCOUNT, {**observation, "status": "chargedback"}) == "applied"
    with Session(database[1]) as s:
        assert s.get(Message, (ACCOUNT, FAN, 9001)).is_paid is False
        assert not any(s.scalars(select(VaultSend.was_purchased)))
        assert s.get(Fan, (ACCOUNT, FAN)).lifetime_spend_cents == 0
    store.close()


def test_worker_reconciles_paused_accounts_without_outgoing_or_model_and_http_outside_transaction(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    with Session(database[1]) as s, s.begin():
        s.get(OfProviderConnection, account).status = "paused"
    calls = []
    row = transaction(); row["description"] = "Tip from Synthetic Fan"
    def read(self, **kwargs):
        assert store.engine.pool.checkedout() == 0
        assert kwargs["offset"] == 0 and kwargs["limit"] == 20
        calls.append(kwargs)
        return payload([row])
    monkeypatch.setattr(NativeProviderConnector, "transaction_page", read)
    assert process_ledger_pages(store, TENANT) == 1
    assert process_ledger_pages(store, TENANT) == 0
    with Session(database[1]) as s:
        scan = s.get(OfLedgerScan, account)
        assert scan.status == "idle" and scan.last_complete_at
        assert s.scalar(select(Transaction.kind)) == "tip"
        assert s.get(Fan, (account, 101)).lifetime_spend_cents == 1000
    assert len(calls) == 1
    store.close()


def test_replaced_credentials_invalid_page_stop_and_wrong_tenant_never_apply_money(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    calls = []
    def read(self, **kwargs):
        calls.append(1)
        with Session(database[1]) as s, s.begin():
            s.get(OfProviderConnection, account).revision += 1
        return payload([transaction()])
    monkeypatch.setattr(NativeProviderConnector, "transaction_page", read)
    stop = threading.Event(); stop.set()
    assert process_ledger_pages(store, TENANT, stop=stop) == 0
    assert process_ledger_pages(store, "other-tenant") == 0
    assert process_ledger_pages(store, TENANT) == 0 and calls == [1]
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
        assert s.get(OfLedgerScan, account).last_error == "connection_changed"
    store.close()


def test_complete_page_is_atomic_and_invalid_read_errors_are_redacted(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    monkeypatch.setattr(NativeProviderConnector, "transaction_page", lambda self, **kwargs: payload([transaction(), {"id": "bad"}]))
    assert process_ledger_pages(store, TENANT) == 0
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
        assert s.get(OfLedgerScan, account).last_error == "unsupported_ledger_response"
        assert held(s, account)
    store.close()


def test_competing_workers_lease_one_page(database, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    store, account, _ = verified(database, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def read(self, **kwargs):
        calls.append(kwargs["offset"])
        entered.set()
        assert release.wait(10)
        return payload([transaction(description="Tip from Synthetic Fan")])
    monkeypatch.setattr(NativeProviderConnector, "transaction_page", read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(process_ledger_pages, store, TENANT)
        assert entered.wait(10)
        assert process_ledger_pages(store, TENANT) == 0
        release.set()
        assert first.result(timeout=15) == 1
    assert calls == [0]
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 1
    store.close()


def test_expired_lease_is_recovered_and_old_reader_cannot_commit(database, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    store, account, _ = verified(database, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls = []
    row = transaction(description="Tip from Synthetic Fan")
    def read(self, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set(); assert release.wait(10)
        return payload([row])
    monkeypatch.setattr(NativeProviderConnector, "transaction_page", read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(process_ledger_pages, store, TENANT)
        assert entered.wait(10)
        with database[1].begin() as c:
            c.exec_driver_sql("UPDATE of_ledger_scan SET lease_until=CURRENT_TIMESTAMP-INTERVAL '1 second'")
        assert process_ledger_pages(store, TENANT) == 1
        release.set()
        assert old.result(timeout=15) == 0
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 1
        assert s.scalar(select(func.count()).select_from(OfLedgerEvidence)) == 1
        assert s.get(Fan, (account, 101)).lifetime_spend_cents == 1000
    store.close()


def test_unavailable_encrypted_connection_is_held_without_starving_later_work(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    with Session(database[1]) as s, s.begin():
        s.get(OfProviderConnection, account).sealed_config = "invalid"
    monkeypatch.setattr(NativeProviderConnector, "transaction_page", lambda *_ , **__: pytest.fail("must not reach provider"))
    assert process_ledger_pages(store, TENANT) == 0
    with Session(database[1]) as s:
        scan = s.get(OfLedgerScan, account)
        assert scan.status == "held" and scan.last_error == "connection_unavailable"
        assert scan.next_poll_at > utcnow() and scan.lease_token is None
    assert process_ledger_pages(store, TENANT) == 0
    store.close()


def test_native_media_requires_a_recent_completed_ledger_scan(database, monkeypatch):
    from db.onlyfans_delivery_models import OfDeliveryPolicy
    store, account, _ = verified(database, monkeypatch)
    with Session(database[1]) as s, s.begin():
        s.add(OfDeliveryPolicy(account_id=account, transport_kind="native", media_enabled=True))
    with Session(database[1]) as s:
        assert held(s, account)
    monkeypatch.setattr(NativeProviderConnector, "transaction_page", lambda self, **kwargs: payload([]))
    assert process_ledger_pages(store, TENANT) == 1
    with Session(database[1]) as s:
        assert not held(s, account)
    with Session(database[1]) as s, s.begin():
        s.get(OfLedgerScan, account).last_complete_at = utcnow()-timedelta(minutes=16)
    with Session(database[1]) as s:
        assert held(s, account)
    store.close()


def test_long_scan_records_its_provider_cutoff_instead_of_claiming_newer_coverage(database, monkeypatch):
    import onlyfans_runtime.ledger as ledger
    import onlyfans_runtime.store as store_module
    store, account, _ = verified(database, monkeypatch)
    cutoff = utcnow()
    monkeypatch.setattr(ledger, "utcnow", lambda: cutoff)
    def read(self, **kwargs):
        assert kwargs["end"] == cutoff
        monkeypatch.setattr(ledger, "utcnow", lambda: cutoff+timedelta(minutes=20))
        return payload([])
    monkeypatch.setattr(NativeProviderConnector, "transaction_page", read)
    assert process_ledger_pages(store, TENANT) == 1
    with Session(database[1]) as s:
        scan = s.get(OfLedgerScan, account)
        assert scan.last_complete_at == cutoff and scan.last_full_at == cutoff
        assert scan.next_poll_at <= cutoff+timedelta(minutes=20, seconds=2)
    store.close()


def test_populated_0083_upgrade_preserves_disabled_state_and_adds_checked_evidence(database, monkeypatch):
    from db.schema import check_schema, upgrade
    store, account, _ = verified(database, monkeypatch)
    with database[1].begin() as c:
        # Recreate the published 0083 shape, including removal of newer media admission.
        c.exec_driver_sql("DROP TABLE of_vault_media_job; DROP FUNCTION of_vault_media_guard()")
        c.exec_driver_sql("DROP TABLE of_connection_enrollment; DROP TABLE of_vault_edit; DROP FUNCTION of_vault_edit_immutable(); DROP TABLE of_creator_facts_source; DROP TABLE of_ledger_evidence; DROP TABLE of_ledger_scan; DROP FUNCTION of_ledger_immutable()")
        c.exec_driver_sql("UPDATE alembic_version SET version_num='0083_vault_management'")
        upgrade(c)
        check_schema(c)
    from db.onlyfans_models import OfRuntime
    with Session(database[1]) as s:
        assert s.get(OfRuntime, account).enabled is False
        assert s.get(OfProviderConnection, account).status == "verified"
        assert s.scalar(select(func.count()).select_from(OfLedgerEvidence)) == 0
    store.close()
