"""Paid inbox and marker recovery on disposable PostgreSQL and synthetic wires."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_manual_vault_composer_pg import prepared, prepare, spec, FAN
from test_onlyfans_connections_pg import verified, TENANT
from test_onlyfans_ledger_pg import transaction, payload, parsed, record
from test_onlyfans_launch_ledger_recovery_pg import frozen_scan
from db.models import Transaction, VaultSend
from db.onlyfans_ledger_models import OfLedgerScan, OfLedgerEvidence
from onlyfans_runtime import ledger
from onlyfans_runtime.commerce import held, manual_held
from onlyfans_runtime.media_delivery import assert_unowned
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import Refused, Store, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport


def payment(store, account, fan=FAN):
    observation = parsed(transaction(user={"id": fan}, createdAt=utcnow().isoformat()+"Z"))
    assert record(store, account, observation) == "applied"


@pytest.mark.parametrize("status", ["running", "held"])
def test_manual_paid_send_during_incomplete_archive_keeps_ai_hold(database, monkeypatch, status):
    store, authority, account = prepared(database, monkeypatch)
    frozen_scan(database[1], account, status=status)
    with Session(database[1]) as s:
        assert held(s, account, FAN)
        assert not manual_held(s, account, FAN)
        with pytest.raises(Refused, match="commerce evidence"):
            assert_unowned(s, account, FAN, {"media_ids": [11,13], "preview_ids": [11]})
    identity = prepare(authority, account, spec())
    wire = SyntheticTextTransport(account)
    assert authority.dispatch(TENANT, account, identity, wire) == "accepted"
    assert len(wire.calls) == 1 and wire.calls[0].price_cents == 1234
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
        assert s.get(OfLedgerScan, account).last_complete_at is None
    store.close()


def test_other_fans_unlinked_purchase_does_not_block_manual_recipient(database, monkeypatch):
    store, authority, account = prepared(database, monkeypatch)
    payment(store, account, fan=202)
    with Session(database[1]) as s:
        assert held(s, account) and not manual_held(s, account, FAN)
        before = s.scalar(select(Transaction)).raw_json
    identity = prepare(authority, account, spec())
    wire = SyntheticTextTransport(account)
    assert authority.dispatch(TENANT, account, identity, wire) == "accepted"
    with Session(database[1]) as s:
        tx = s.scalar(select(Transaction))
        assert tx.message_id is None and tx.raw_json == before
    store.close()


def test_own_unlinked_purchase_blocks_prepare(database, monkeypatch):
    store, authority, account = prepared(database, monkeypatch)
    payment(store, account)
    with pytest.raises(Refused, match="commerce evidence"):
        prepare(authority, account, spec())
    store.close()


def test_payment_arriving_after_queue_blocks_dispatch(database, monkeypatch):
    store, authority, account = prepared(database, monkeypatch)
    identity = prepare(authority, account, spec())
    payment(store, account)
    wire = SyntheticTextTransport(account)
    assert authority.dispatch(TENANT, account, identity, wire) == "cancelled"
    assert wire.calls == []
    store.close()


@pytest.mark.parametrize("fan,blocked", [(FAN, True), (202, False)])
def test_manual_conflict_scope_is_the_recipient(database, monkeypatch, fan, blocked):
    store, authority, account = prepared(database, monkeypatch)
    with Session(database[1]) as s, s.begin():
        s.add(OfLedgerEvidence(account_id=account, evidence_hash="a"*64,
            provider_transaction_id="synthetic-conflict", fan_id=fan, outcome="conflict",
            observation_json="{}", recorded_at=utcnow()))
    if blocked:
        with pytest.raises(Refused, match="commerce evidence"):
            prepare(authority, account, spec())
    else:
        identity = prepare(authority, account, spec())
        assert authority.dispatch(TENANT, account, identity, SyntheticTextTransport(account)) == "accepted"
    store.close()


def test_manual_send_still_blocks_known_owned_media(database, monkeypatch):
    store, authority, account = prepared(database, monkeypatch)
    with Session(database[1]) as s, s.begin():
        s.add(VaultSend(account_id=account, fan_id=FAN, media_id=13, message_id=777,
            price_cents=1234, was_purchased=True, sent_at=utcnow()))
    with pytest.raises(Refused, match="already owned"):
        prepare(authority, account, spec())
    store.close()


def test_recent_marker_crosses_10000_resumes_and_keeps_exact_money(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    start, end = frozen_scan(database[1], account, offset=10000)
    calls = []
    def page(self, **kw):
        calls.append(kw)
        n = len(calls)
        assert kw["marker"] == ("fixture-offset-10000" if n == 1 else "continued")
        assert kw["offset"] == 10000 + n - 1
        row = transaction(id=f"synthetic-{n}", description="Tip from Synthetic Fan",
            createdAt=(end-timedelta(minutes=1)).isoformat()+"Z")
        result = json.loads(payload([row], more=n == 1))
        if n == 1:
            result["nextMarker"] = "continued"
        return json.dumps(result).encode()
    monkeypatch.setattr(NativeProviderConnector, "transaction_page", page)
    assert ledger.process_ledger_pages(store, TENANT) == 1
    with Session(database[1]) as s, s.begin():
        scan = s.get(OfLedgerScan, account)
        assert scan.page_offset == 10000 and ledger.scan_position(scan) == 10001
        assert scan.last_complete_at is None and not ledger.coverage_proven(scan)
        scan.next_poll_at = utcnow()-timedelta(seconds=1)
    store.close()
    store = Store(database[0])
    assert ledger.process_ledger_pages(store, TENANT) == 1
    with Session(database[1]) as s:
        scan = s.get(OfLedgerScan, account)
        assert ledger.scan_position(scan) == 10002 and ledger.coverage_proven(scan)
        assert scan.last_complete_at == end and scan.status == "idle"
        assert s.scalar(select(func.count()).select_from(Transaction)) == 2
        assert s.scalar(select(func.sum(Transaction.amount_cents))) == 2000
    store.close()


def test_cycle_after_10000_is_still_held(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    start, end = frozen_scan(database[1], account, offset=10000)
    result = json.loads(payload([transaction(description="Tip from Synthetic Fan")], more=True))
    result["nextMarker"] = "fixture-offset-10000"
    monkeypatch.setattr(NativeProviderConnector, "transaction_page", lambda *a, **kw: json.dumps(result).encode())
    assert ledger.process_ledger_pages(store, TENANT) == 0
    with Session(database[1]) as s:
        assert s.get(OfLedgerScan, account).last_error == "unsupported_ledger_pagination"
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
    store.close()


def test_marker_wire_has_no_offset_ceiling_or_offset_query(monkeypatch):
    from test_onlyfans_provider_adapters import credential
    from urllib.parse import urlsplit, parse_qs
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    wire = NativeProviderConnector(credential())
    calls = []
    monkeypatch.setattr(wire, "verify_identity", lambda: calls.append("identity"))
    def call(method, path, body=b""):
        assert calls == ["identity"] and method == "GET" and body == b""
        query = parse_qs(urlsplit(path).query)
        assert query["marker"] == ["synthetic-next"] and "offset" not in query
        return payload([])
    monkeypatch.setattr(wire, "_call", call)
    end = utcnow()
    assert wire.transaction_page(offset=10001, start=end-timedelta(days=7), end=end, marker="synthetic-next")
    with pytest.raises(Refused):
        wire.transaction_page(offset=10001, start=end-timedelta(days=7), end=end)
