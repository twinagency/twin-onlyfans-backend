"""Ledger recovery regressions on disposable PostgreSQL and synthetic transport."""
import json
import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, seed
from test_onlyfans_connections_pg import verified, TENANT
from test_onlyfans_ledger_pg import parsed, payload, record, transaction
from test_onlyfans_launch_ledger_boundary import boundary_rows, boundary_payload
from test_onlyfans_media_pg import manual, intent
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN
from db.models import CatalogItem, ContentOffer, Fan, Message, Transaction, VaultSend
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_ledger_models import OfLedgerEvidence, OfLedgerScan
from db.onlyfans_models import OfRuntime
import onlyfans_runtime.ledger as ledger
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from ownership import owned_media_in_session


def frozen_scan(engine, account, *, offset=40, status="held"):
    end = utcnow() - timedelta(minutes=2)
    start = end - timedelta(days=90)
    with Session(engine) as s, s.begin():
        connection = s.get(OfProviderConnection, account)
        scan=OfLedgerScan(account_id=account, binding_id=connection.binding_id,
            connection_revision=connection.revision, owner_id=connection.owner_id,
            window_start=start, window_end=end, page_offset=offset, status=status,
            last_error="unsupported_ledger_response" if status == "held" else None,
            next_poll_at=utcnow() - timedelta(seconds=1), updated_at=utcnow())
        cursor=ledger._new_cursor(scan)
        cursor["marker"]="fixture-offset-"+str(offset) if offset else None
        scan.continuation_json=ledger.canonical(cursor)
        s.add(scan)
    return start, end


def purchased_media(database, monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    store, authority, item = manual(database)
    identity = intent(authority, item)
    assert authority.dispatch(TENANT, ACCOUNT, identity, SyntheticTextTransport(ACCOUNT)) == "accepted"
    observation = parsed(transaction())
    observation.update(fan_id=FAN, message_id=9001, occurred_at=utcnow().isoformat())
    assert record(store, ACCOUNT, observation) == "applied"
    return store, observation


def test_held_offset_40_resumes_the_original_frozen_window(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    start, end = frozen_scan(database[1], account)
    calls = []

    def read(self, **kwargs):
        calls.append(kwargs)
        return payload([transaction(description="Tip from Synthetic Fan",
            createdAt=(end - timedelta(minutes=1)).isoformat() + "Z")])

    monkeypatch.setattr(NativeProviderConnector, "transaction_page", read)
    assert ledger.process_ledger_pages(store, TENANT) == 1
    assert calls == [{"offset": 40, "limit": 20, "start": start, "end": end, "marker":"fixture-offset-40"}]
    with Session(database[1]) as s:
        scan = s.get(OfLedgerScan, account)
        assert (scan.window_start, scan.window_end, scan.page_offset) == (start, end, 41)
        assert scan.status == "idle" and scan.last_complete_at == end
        assert s.get(Fan, (account, 101)).lifetime_spend_cents == 1000
        assert s.get(OfRuntime, account).enabled is False
    store.close()


def test_reported_offset_40_boundary_commits_only_fourteen_in_window_rows(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    start, end = frozen_scan(database[1], account)
    source = boundary_rows(start, end)
    calls = []
    def read(self, **kwargs):
        calls.append(kwargs)
        return boundary_payload(source)
    monkeypatch.setattr(NativeProviderConnector, "transaction_page", read)
    assert ledger.process_ledger_pages(store, TENANT) == 1
    assert calls == [{"offset": 40, "limit": 20, "start": start, "end": end, "marker":"fixture-offset-40"}]
    with Session(database[1]) as s:
        scan = s.get(OfLedgerScan, account)
        assert scan.status == "running" and scan.page_offset == 60
        assert scan.last_complete_at is None and scan.last_full_at is None
        assert s.scalar(select(func.count()).select_from(Transaction)) == 14
        assert s.scalar(select(func.count()).select_from(OfLedgerEvidence)) == 14
        assert s.get(Fan, (account, 101)).lifetime_spend_cents == 14014
        assert s.scalar(select(func.count()).select_from(Message).where(Message.is_paid.is_(True))) == 0
        assert all(mid is None for mid in s.scalars(select(Transaction.message_id)))
        assert s.get(OfRuntime, account).enabled is False
        assert ledger.process_ledger_pages(store, TENANT) == 0
    store.close()


def test_invalid_older_tail_keeps_offset_40_atomic_and_unpaid(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    start, end = frozen_scan(database[1], account)
    source = boundary_rows(start, end)
    source[-1]["amount"] = "1.001"
    monkeypatch.setattr(NativeProviderConnector, "transaction_page", lambda *args, **kwargs: boundary_payload(source))
    assert ledger.process_ledger_pages(store, TENANT) == 0
    with Session(database[1]) as s:
        scan = s.get(OfLedgerScan, account)
        assert scan.status == "held" and scan.last_error == "unsupported_ledger_response"
        assert scan.page_offset == 40 and scan.last_complete_at is None
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
        assert s.scalar(select(func.count()).select_from(OfLedgerEvidence)) == 0
    store.close()


def test_final_page_crossing_legacy_offset_uses_verified_marker(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    start, end = frozen_scan(database[1], account, offset=ledger.MAX_OFFSET, status="running")
    calls = []

    def read(self, **kwargs):
        calls.append(kwargs)
        return payload([transaction(description="Tip from Synthetic Fan",
            createdAt=(end - timedelta(minutes=1)).isoformat() + "Z")], more=False)

    monkeypatch.setattr(NativeProviderConnector, "transaction_page", read)
    assert ledger.process_ledger_pages(store, TENANT) == 1
    assert calls == [{"offset": ledger.MAX_OFFSET, "limit": 20, "start": start, "end": end, "marker":"fixture-offset-"+str(ledger.MAX_OFFSET)}]
    with Session(database[1]) as s:
        scan = s.get(OfLedgerScan, account)
        assert scan.status == "idle" and scan.last_error is None
        assert scan.page_offset == ledger.MAX_OFFSET and scan.last_complete_at == end
        assert ledger.scan_position(scan) == ledger.MAX_OFFSET + 1
        assert scan.lease_token is None and scan.lease_until is None
        assert s.scalar(select(func.count()).select_from(Transaction)) == 1
        assert s.scalar(select(func.count()).select_from(OfLedgerEvidence)) == 1
    store.close()


@pytest.mark.parametrize("last_more", [False, True])
def test_repeated_page_with_different_json_format_cannot_complete_scan(database, monkeypatch, last_more):
    store, account, _ = verified(database, monkeypatch)
    row = transaction(description="Tip from Synthetic Fan")
    calls = []

    def read(self, **kwargs):
        calls.append(kwargs["offset"])
        if len(calls) == 1:
            return payload([row], more=True)
        # Provider row identity is unchanged despite whitespace/key order and
        # a changed pagination flag. The repeat is not fresh coverage.
        return json.dumps({"hasMore": last_more,"nextMarker":row["id"], "list": [dict(reversed(list(row.items())))]},
            indent=2).encode()

    monkeypatch.setattr(NativeProviderConnector, "transaction_page", read)
    assert ledger.process_ledger_pages(store, TENANT) == 1
    with Session(database[1]) as s, s.begin():
        s.get(OfLedgerScan, account).next_poll_at = utcnow() - timedelta(seconds=1)
    assert ledger.process_ledger_pages(store, TENANT) == 0
    assert calls == [0, 1]
    with Session(database[1]) as s:
        scan = s.get(OfLedgerScan, account)
        assert scan.status == "held" and scan.last_error == "unsupported_ledger_pagination"
        assert scan.page_offset == 1 and scan.last_complete_at is None
        assert s.scalar(select(func.count()).select_from(Transaction)) == 1
        assert s.scalar(select(func.count()).select_from(OfLedgerEvidence)) == 1
        assert s.get(Fan, (account, 101)).lifetime_spend_cents == 1000
    store.close()


def test_interrupted_page_rolls_back_all_effects_and_replays_same_window(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    rows = [transaction(id="synthetic-tx-1", description="Tip from Synthetic Fan"),
        transaction(id="synthetic-tx-2", description="Tip from Synthetic Fan")]
    calls = []

    def read(self, **kwargs):
        calls.append(kwargs)
        return payload(rows)

    original = ledger.apply_observation
    applied = []

    def interrupt(s, account_id, observation, *, now, backfill=False):
        applied.append(observation["provider_transaction_id"])
        if len(applied) == 2:
            raise RuntimeError("synthetic interrupted page")
        return original(s, account_id, observation, now=now, backfill=backfill)

    monkeypatch.setattr(NativeProviderConnector, "transaction_page", read)
    monkeypatch.setattr(ledger, "apply_observation", interrupt)
    with pytest.raises(RuntimeError, match="synthetic interrupted page"):
        ledger.process_ledger_pages(store, TENANT)
    with Session(database[1]) as s, s.begin():
        scan = s.get(OfLedgerScan, account)
        assert scan.page_offset == 0 and scan.last_complete_at is None
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
        assert s.scalar(select(func.count()).select_from(OfLedgerEvidence)) == 0
        scan.lease_until = utcnow() - timedelta(seconds=1)
    monkeypatch.setattr(ledger, "apply_observation", original)
    assert ledger.process_ledger_pages(store, TENANT) == 1
    assert len(calls) == 2 and calls[0] == calls[1]
    with Session(database[1]) as s:
        scan = s.get(OfLedgerScan, account)
        assert scan.status == "idle" and scan.page_offset == 2
        assert s.scalar(select(func.count()).select_from(Transaction)) == 2
        assert s.scalar(select(func.count()).select_from(OfLedgerEvidence)) == 2
        assert s.get(Fan, (account, 101)).lifetime_spend_cents == 2000
    store.close()


def test_recovery_keeps_duplicate_protection_for_pre_upgrade_raw_page_hash(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    _, end = frozen_scan(database[1], account, status="running")
    raw = payload([transaction(description="Tip from Synthetic Fan",
        createdAt=(end - timedelta(minutes=1)).isoformat() + "Z")], more=True)
    with Session(database[1]) as s, s.begin():
        s.get(OfLedgerScan, account).last_page_hash = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(NativeProviderConnector, "transaction_page", lambda *args, **kwargs: raw)
    assert ledger.process_ledger_pages(store, TENANT) == 0
    with Session(database[1]) as s:
        scan = s.get(OfLedgerScan, account)
        assert scan.status == "held" and scan.last_error == "unsupported_ledger_pagination"
        assert scan.page_offset == 40 and scan.last_complete_at is None
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
    store.close()


@pytest.mark.parametrize("status", ["refunded", "chargedback"])
@pytest.mark.parametrize("change", ["unsent", "message_price", "offer_price"])
def test_reversal_of_bound_purchase_survives_later_local_edits(database, monkeypatch, status, change):
    store, observation = purchased_media(database, monkeypatch)
    with Session(database[1]) as s, s.begin():
        message = s.get(Message, (ACCOUNT, FAN, 9001))
        offer = s.scalar(select(ContentOffer).where(ContentOffer.account_id == ACCOUNT,
            ContentOffer.fan_id == FAN, ContentOffer.offer_message_id == 9001))
        if change == "unsent":
            message.is_unsent = True
        elif change == "message_price":
            message.price_cents = 2000
        else:
            offer.price_cents = 2000
    reversal = {**observation, "status": status}
    assert record(store, ACCOUNT, reversal) == "applied"
    assert record(store, ACCOUNT, reversal) == "duplicate"
    assert record(store, ACCOUNT, {**reversal, "net_cents": 800, "fee_cents": 200}) == "applied"
    assert record(store, ACCOUNT, {**observation, "description": "Payment for message from stale display"}) == "stale"
    with Session(database[1]) as s:
        tx = s.scalar(select(Transaction).where(Transaction.account_id == ACCOUNT))
        assert tx.status == status and tx.amount_cents == 1000
        assert s.get(Fan, (ACCOUNT, FAN)).lifetime_spend_cents == 0
        assert s.get(Message, (ACCOUNT, FAN, 9001)).is_paid is False
        assert not any(s.scalars(select(VaultSend.was_purchased).where(VaultSend.account_id == ACCOUNT,
            VaultSend.fan_id == FAN, VaultSend.message_id == 9001)))
        assert owned_media_in_session(s, ACCOUNT, FAN) == set()
        offer = s.scalar(select(ContentOffer).where(ContentOffer.account_id == ACCOUNT,
            ContentOffer.fan_id == FAN, ContentOffer.offer_message_id == 9001))
        assert offer.status == "cancelled" and offer.resolved_by is None
    store.close()


@pytest.mark.parametrize("field,value", [("fan_id", 102), ("kind", "tip"),
    ("amount_cents", 1001), ("currency", "AUD"), ("message_id", 9002),
    ("occurred_at", "2026-01-01T00:00:00")])
def test_reversal_with_changed_immutable_identity_remains_conflict(database, monkeypatch, field, value):
    store, observation = purchased_media(database, monkeypatch)
    assert record(store, ACCOUNT, {**observation, "status": "refunded", field: value}) == "conflict"
    with Session(database[1]) as s:
        tx = s.scalar(select(Transaction).where(Transaction.account_id == ACCOUNT))
        assert tx.status == "cleared" and tx.amount_cents == 1000
        assert tx.fan_id == FAN and tx.message_id == 9001
        assert s.get(Fan, (ACCOUNT, FAN)).lifetime_spend_cents == 1000
        assert s.get(Message, (ACCOUNT, FAN, 9001)).is_paid is True
        assert owned_media_in_session(s, ACCOUNT, FAN) == {11, 12, 13}
        assert s.scalar(select(func.count()).select_from(Transaction)) == 1
    store.close()


def test_refund_preserves_an_independent_paid_copy_of_same_media(database, monkeypatch):
    store, observation = purchased_media(database, monkeypatch)
    with Session(database[1]) as s, s.begin():
        original = s.get(Message, (ACCOUNT, FAN, 9001))
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=9002, direction="out",
            body="Synthetic independent purchase", media_ids=original.media_ids,
            media_count=original.media_count, price_cents=1000, is_paid=False,
            created_at=original.created_at))
        for media_id in (11, 12, 13):
            s.add(VaultSend(account_id=ACCOUNT, fan_id=FAN, message_id=9002,
                media_id=media_id, price_cents=1000, was_purchased=False))
    second = {**observation, "provider_transaction_id": "synthetic-independent-tx", "message_id": 9002}
    assert record(store, ACCOUNT, second) == "applied"
    assert record(store, ACCOUNT, {**observation, "status": "refunded"}) == "applied"
    with Session(database[1]) as s:
        assert s.get(Fan, (ACCOUNT, FAN)).lifetime_spend_cents == 1000
        assert s.get(Message, (ACCOUNT, FAN, 9001)).is_paid is False
        assert s.get(Message, (ACCOUNT, FAN, 9002)).is_paid is True
        assert not any(s.scalars(select(VaultSend.was_purchased).where(VaultSend.message_id == 9001)))
        assert all(s.scalars(select(VaultSend.was_purchased).where(VaultSend.message_id == 9002)))
        assert owned_media_in_session(s, ACCOUNT, FAN) == {11, 12, 13}
    store.close()


def test_same_provider_and_fan_ids_cannot_transfer_ownership_between_creators(database, monkeypatch):
    store, observation = purchased_media(database, monkeypatch)
    other = seed(*database, account="synthetic-other-creator", tenant=TENANT, fan=FAN)
    # Same provider/fan/message ID is insufficient when the message exists
    # only under the first creator. Financial evidence stays scoped to each
    # account; the missing local message cannot transfer ownership between them.
    assert record(other, "synthetic-other-creator", observation) == "applied"
    unlinked = {**observation, "message_id": None}
    assert record(other, "synthetic-other-creator", unlinked) == "applied"
    with Session(database[1]) as s:
        tx = s.scalar(select(Transaction).where(Transaction.account_id == "synthetic-other-creator"))
        assert tx.amount_cents == 1000 and tx.message_id is None
        assert s.get(Fan, ("synthetic-other-creator", FAN)).lifetime_spend_cents == 1000
        assert owned_media_in_session(s, "synthetic-other-creator", FAN) == set()
    assert record(other, "synthetic-other-creator", unlinked) == "duplicate"
    assert record(other, "synthetic-other-creator", {**unlinked, "status": "refunded"}) == "applied"
    with Session(database[1]) as s:
        assert s.get(Fan, (ACCOUNT, FAN)).lifetime_spend_cents == 1000
        assert s.get(Fan, ("synthetic-other-creator", FAN)).lifetime_spend_cents == 0
        assert s.get(Message, (ACCOUNT, FAN, 9001)).is_paid is True
        assert owned_media_in_session(s, ACCOUNT, FAN) == {11, 12, 13}
        assert owned_media_in_session(s, "synthetic-other-creator", FAN) == set()
        assert s.scalar(select(func.count()).select_from(Transaction)) == 2
    other.close()
    store.close()


def test_missing_optional_settlement_fields_do_not_erase_prior_evidence(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    observation = parsed(transaction(description="Tip from Synthetic Fan",
        clearedAt=utcnow().isoformat() + "Z", net="8.00", fee="2.00", vatAmount="0.00"))
    assert record(store, account, observation) == "applied"
    later = {**observation, "status": "refund_pending", "cleared_at": None,
        "net_cents": None, "fee_cents": None, "vat_cents": None}
    assert record(store, account, later) == "applied"
    with Session(database[1]) as s:
        tx = s.scalar(select(Transaction).where(Transaction.account_id == account))
        assert tx.cleared_at.isoformat() == observation["cleared_at"]
        assert (tx.net_cents, tx.fee_cents, tx.vat_cents) == (800, 200, 0)
        assert tx.status == "refund_pending"
        assert s.get(Fan, (account, 101)).lifetime_spend_cents == 1000
    assert record(store, account, {**later, "status": "refunded"}) == "applied"
    with Session(database[1]) as s:
        assert s.get(Fan, (account, 101)).lifetime_spend_cents == 0
    store.close()


def test_pinned_fixed_price_mixed_bundle_matches_exact_cents_and_rejects_owned_content(database, monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    store, authority, item = manual(database)
    with Session(database[1]) as s, s.begin():
        catalog = s.get(CatalogItem, item)
        catalog.pricing_mode, catalog.price_cents = "fixed", 1007
    identity = intent(authority, item)
    wire = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, identity, wire) == "accepted"
    assert wire.calls[0].price_cents == 1007 and wire.calls[0].media_ids == (11, 12, 13)
    observation = parsed(transaction(amount="10.07"))
    observation.update(fan_id=FAN, message_id=9001, occurred_at=utcnow().isoformat())
    assert record(store, ACCOUNT, observation) == "applied"
    with Session(database[1]) as s:
        assert s.get(Fan, (ACCOUNT, FAN)).lifetime_spend_cents == 1007
        assert s.get(Message, (ACCOUNT, FAN, 9001)).is_paid is True
        assert owned_media_in_session(s, ACCOUNT, FAN) == {11, 12, 13}
    with pytest.raises(Refused):
        intent(authority, item, key="synthetic-already-owned")
    assert record(store, ACCOUNT, {**observation, "status": "refunded", "amount_cents": 1000}) == "conflict"
    with Session(database[1]) as s:
        assert s.get(Fan, (ACCOUNT, FAN)).lifetime_spend_cents == 1007
        assert s.get(Message, (ACCOUNT, FAN, 9001)).is_paid is True
    assert record(store, ACCOUNT, {**observation, "status": "refunded"}) == "applied"
    with Session(database[1]) as s:
        assert s.get(Fan, (ACCOUNT, FAN)).lifetime_spend_cents == 0
        assert owned_media_in_session(s, ACCOUNT, FAN) == set()
    store.close()
