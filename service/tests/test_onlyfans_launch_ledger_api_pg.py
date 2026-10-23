"""Ledger dashboard read (of-ledger.v1) and retry command on disposable PostgreSQL; no provider I/O."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from test_onlyfans_foundation_pg import database, seed
from test_onlyfans_connections_pg import verified, TENANT
from test_onlyfans_launch_ledger_recovery_pg import frozen_scan
from test_onlyfans_agency_pg import CREATOR, OTHER, STAFF, COLLEAGUE, headers, assign
from db.models import OfCreatorBinding, Transaction
from db.onlyfans_ledger_models import OfLedgerEvidence, OfLedgerScan
from onlyfans_read.agency_scope import AgencyScopeMiddleware
from onlyfans_runtime.launch_ledger.models import LedgerRetryCommand
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import utcnow

KEY = "c" * 64
FIELDS = {"schemaVersion", "accountId", "status", "windowStart", "windowEnd", "pageOffset", "lastCompleteAt", "lastFullAt",
          "nextPollAt", "lastError", "leaseActive", "connectionCurrent", "connectionRevision", "conflictCount",
          "unlinkedPaidCount", "requiresInvestigation", "commerceHeld", "scanFingerprint"}
READ = {"Authorization": "Bearer synthetic-read", "X-OnlyFans-Schema-Version": "of-preview.v2"}
CONTROL = {"Authorization": "Bearer synthetic-control", "X-OnlyFans-Control-Version": "of-controls.v1"}
AGENCY = "/onlyfans/agency/v1"


class Harness:
    def __init__(self, database, monkeypatch):
        self.database, self.engine = database, database[1]
        self.store, self.account, _ = verified(database, monkeypatch)
        assign(self.engine, self.account, CREATOR)
        for key, value in {"ONLYFANS_PROCESS_ROLE": "api", "ONLYFANS_API_TOKEN": "synthetic-read", "ONLYFANS_API_TENANT": TENANT,
                "ONLYFANS_CONTROL_TOKEN": "synthetic-control", "ONLYFANS_CONTROL_TENANT": TENANT}.items():
            monkeypatch.setenv(key, value)
        from onlyfans_read import api, repo
        from onlyfans_commands.api import router as controls
        import importlib
        engine_module = importlib.import_module("db.engine")
        self.async_engine = create_async_engine(make_url(database[0]).set(drivername="postgresql+asyncpg"), poolclass=NullPool)
        factory = async_sessionmaker(self.async_engine, expire_on_commit=False)

        @asynccontextmanager
        async def sessions():
            async with factory() as session:
                yield session
        monkeypatch.setattr(repo, "get_session", sessions)
        monkeypatch.setattr(engine_module, "get_session", sessions)
        self.calls = []
        for name in ("transaction_page", "verify_identity"):
            monkeypatch.setattr(NativeProviderConnector, name, lambda *a, _n=name, **k: self.calls.append(_n))
        app = FastAPI()
        app.add_middleware(AgencyScopeMiddleware)
        app.include_router(api.router)
        app.include_router(controls)
        app.state.control_store = self.store
        self.client = TestClient(app)

    def close(self):
        self.store.close()
        asyncio.run(self.async_engine.dispose())

    def read(self, account=None, **kw):
        return self.client.get(f"{AGENCY}/api/v1/accounts/{account or self.account}/ledger", **{"headers": {**headers(), **READ}, **kw})

    def retry(self, body=None, account=None, extra=None, **kw):
        body = {"commandKey": KEY, "expectedConnectionRevision": 1, "expectedScanFingerprint": self.read().json()["scanFingerprint"]} if body is None else body
        return self.client.post(f"{AGENCY}/control/v1/accounts/{account or self.account}/ledger/retry",
            json=body, headers={**(extra or headers()), **CONTROL}, **kw)

    def scan(self):
        with Session(self.engine) as s:
            row = s.get(OfLedgerScan, self.account)
            return {c.name: getattr(row, c.name) for c in OfLedgerScan.__table__.columns}

    def counts(self):
        with Session(self.engine) as s:
            return tuple(s.scalar(select(func.count()).select_from(m)) for m in (LedgerRetryCommand, OfLedgerEvidence, Transaction))

    def push_out(self, **changes):
        with Session(self.engine) as s, s.begin():
            row = s.get(OfLedgerScan, self.account)
            row.next_poll_at = utcnow() + timedelta(minutes=5)
            for key, value in changes.items():
                setattr(row, key, value)


@pytest.fixture
def h(database, monkeypatch):
    harness = Harness(database, monkeypatch)
    yield harness
    harness.close()


def test_read_shape_for_held_and_not_started_scan(h):
    empty = h.read()
    assert empty.status_code == 200 and empty.headers["cache-control"] == "private, no-store"
    body = empty.json()
    assert set(body) == FIELDS and body["schemaVersion"] == "of-ledger.v1" and body["accountId"] == h.account
    assert body["status"] == "not_started" and body["scanFingerprint"] is None and body["lastError"] is None
    assert (body["windowStart"], body["pageOffset"], body["nextPollAt"], body["lastCompleteAt"]) == (None,) * 4
    assert (body["connectionCurrent"], body["connectionRevision"], body["leaseActive"]) == (True, 1, False)
    assert (body["requiresInvestigation"], body["commerceHeld"]) == (False, False)  # existing commerce.held: no scan row, no media policy
    start, end = frozen_scan(h.engine, h.account)
    body = h.read().json()
    assert set(body) == FIELDS and body["status"] == "held" and body["pageOffset"] == 40
    assert body["windowStart"] == start.isoformat(timespec="seconds") + "Z" and body["windowEnd"] == end.isoformat(timespec="seconds") + "Z"
    assert (body["lastCompleteAt"], body["lastFullAt"], body["lastError"]) == (None, None, "unsupported_ledger_response")
    assert body["requiresInvestigation"] is True and body["commerceHeld"] is True and len(body["scanFingerprint"]) == 64
    assert body["conflictCount"] == body["unlinkedPaidCount"] == 0
    assert "percent" not in json.dumps(body).lower() and "sealed" not in json.dumps(body)
    assert h.read().json()["scanFingerprint"] == body["scanFingerprint"]
    with Session(h.engine) as s, s.begin():
        s.get(OfLedgerScan, h.account).last_error = "something_internal"
    assert h.read().json()["lastError"] == "unknown_error"
    assert h.calls == []


def test_read_credential_tenant_and_grant_checks_do_not_disclose(h):
    frozen_scan(h.engine, h.account)
    path = f"/onlyfans/api/v1/accounts/{h.account}/ledger"
    assert h.client.get(path, headers=READ).status_code == 200  # internal service contract: no agency narrowing
    assert h.client.get(path).status_code == 401
    assert h.client.get(path, headers={**READ, "Authorization": "Bearer wrong"}).status_code == 401
    assert h.client.get(path, headers={"Authorization": READ["Authorization"]}).status_code == 400
    assert h.read(headers={**headers(manage=False), **READ}).status_code == 200  # read-only agency user may read
    denied = h.read(headers={**headers(creators=(OTHER,)), **READ})
    unknown = h.read("no-such-account")
    assert denied.status_code == unknown.status_code == 404 and denied.json()["code"] == unknown.json()["code"]
    assert h.account not in denied.text and "no-such-account" not in unknown.text and "scanFingerprint" not in denied.text
    with Session(h.engine) as s, s.begin():
        s.scalar(select(OfCreatorBinding).where(OfCreatorBinding.account_id == h.account)).tenant_id = "tenant-other"
    assert h.read().status_code == 404 and h.client.get(path, headers=READ).status_code == 404


def test_creator_switch_never_mixes_per_account_values(h):
    other = seed(*h.database, account="acc-b", tenant=TENANT)
    assign(h.engine, "acc-b", OTHER)
    frozen_scan(h.engine, h.account)
    now = utcnow()
    with Session(h.engine) as s, s.begin():
        s.add(OfLedgerScan(account_id="acc-b", binding_id="binding-acc-b", connection_revision=9, owner_id=777,
            window_start=now - timedelta(days=7), window_end=now, page_offset=3, status="idle", next_poll_at=now,
            last_complete_at=now, last_full_at=now, updated_at=now))
        s.add(OfLedgerEvidence(account_id="acc-b", evidence_hash="e" * 64, provider_transaction_id="x1", fan_id=101,
            outcome="conflict", observation_json="{}", recorded_at=now))
        s.add(Transaction(account_id=h.account, fan_id=101, kind="ppv_message", amount_cents=1000, currency="USD",
            occurred_at=now, provider_transaction_id="p1", status="cleared", source="ledger"))
    mine = h.read().json()
    assert (mine["conflictCount"], mine["unlinkedPaidCount"], mine["status"], mine["pageOffset"]) == (0, 1, "held", 40)
    assert mine["requiresInvestigation"] is True
    switched = h.client.get(f"{AGENCY}/api/v1/accounts/acc-b/ledger", headers={**headers(creators=(OTHER,)), **READ}).json()
    assert (switched["accountId"], switched["conflictCount"], switched["unlinkedPaidCount"], switched["status"]) == ("acc-b", 1, 0, "idle")
    assert switched["connectionCurrent"] is False and switched["connectionRevision"] is None and switched["pageOffset"] == 3
    assert h.read("acc-b").status_code == 404  # CREATOR's grant does not reach the other creator's account
    assert h.client.get(f"{AGENCY}/api/v1/accounts/{h.account}/ledger", headers={**headers(creators=(OTHER,)), **READ}).status_code == 404
    other.close()


def test_retry_only_makes_the_held_scan_due(h):
    frozen_scan(h.engine, h.account)
    h.push_out()
    before, counts = h.scan(), h.counts()
    response = h.retry()
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    ack = response.json()
    assert ack["ok"] is True and ack["outcome"] == "queued" and ack["accepted"] is True
    assert ack["commandKey"] == KEY and ack["accountId"] == h.account and "reconciled" not in json.dumps(ack)
    after = h.scan()
    assert after["next_poll_at"] <= utcnow() and after["next_poll_at"] < before["next_poll_at"]
    assert {k: v for k, v in after.items() if k != "next_poll_at"} == {k: v for k, v in before.items() if k != "next_poll_at"}
    assert (after["status"], after["last_error"], after["last_complete_at"], after["page_offset"]) == ("held", "unsupported_ledger_response", None, 40)
    assert h.counts() == (counts[0] + 1, counts[1], counts[2])
    with Session(h.engine) as s:
        row = s.get(LedgerRetryCommand, (h.account, KEY))
        assert (row.actor, row.result, row.expected_connection_revision) == (STAFF, "queued", 1)
        assert row.binding_id == s.scalar(select(OfCreatorBinding.id).where(OfCreatorBinding.account_id == h.account))
    assert h.calls == []


def test_retry_is_idempotent_and_changed_payload_conflicts(h):
    frozen_scan(h.engine, h.account)
    h.push_out()
    fingerprint = h.read().json()["scanFingerprint"]
    first = h.retry()
    assert first.status_code == 200 and h.read().json()["scanFingerprint"] != fingerprint  # the scan moved on
    scan = h.scan()
    assert h.retry({"commandKey": KEY, "expectedConnectionRevision": 1, "expectedScanFingerprint": fingerprint}).json() == first.json()
    assert h.scan() == scan and h.counts()[0] == 1
    for body in ({"commandKey": KEY, "expectedConnectionRevision": 2, "expectedScanFingerprint": fingerprint},
                 {"commandKey": KEY, "expectedConnectionRevision": 1, "expectedScanFingerprint": "0" * 64}):
        changed = h.retry(body)
        assert changed.status_code == 409 and changed.json()["code"] == "command_conflict"
    other_actor = h.retry({"commandKey": KEY, "expectedConnectionRevision": 1, "expectedScanFingerprint": fingerprint},
        extra=headers(actor=COLLEAGUE))
    assert other_actor.status_code == 409 and other_actor.json()["code"] == "command_conflict"
    assert h.scan() == scan and h.counts()[0] == 1 and h.calls == []


def test_stale_revision_or_fingerprint_is_refused_without_a_record(h):
    frozen_scan(h.engine, h.account)
    h.push_out()
    fingerprint, before = h.read().json()["scanFingerprint"], h.scan()
    for body in ({"commandKey": KEY, "expectedConnectionRevision": 2, "expectedScanFingerprint": fingerprint},
                 {"commandKey": KEY, "expectedConnectionRevision": 1, "expectedScanFingerprint": "a" * 64}):
        stale = h.retry(body)
        assert stale.status_code == 409 and stale.json()["code"] == "ledger_state_changed"
    with Session(h.engine) as s, s.begin():  # the worker moved the offset after the dashboard read
        s.get(OfLedgerScan, h.account).page_offset = 41
    stale = h.retry({"commandKey": KEY, "expectedConnectionRevision": 1, "expectedScanFingerprint": fingerprint})
    assert stale.status_code == 409 and h.counts()[0] == 0 and h.scan()["next_poll_at"] == before["next_poll_at"]


def test_live_lease_idle_and_missing_scan_are_refused(h):
    assert h.retry({"commandKey": KEY, "expectedConnectionRevision": 1, "expectedScanFingerprint": "a" * 64}).json()["code"] == "ledger_state_changed"
    frozen_scan(h.engine, h.account, status="running")
    h.push_out(lease_token="11111111-1111-4111-8111-111111111111", lease_until=utcnow() + timedelta(seconds=60))
    fingerprint, before = h.read().json()["scanFingerprint"], h.scan()
    assert h.read().json()["leaseActive"] is True
    busy = h.retry()
    assert busy.status_code == 409 and busy.json()["code"] == "ledger_busy"
    h.push_out(lease_token=None, lease_until=None, status="idle", last_error=None)
    idle = h.retry()
    assert idle.status_code == 409 and idle.json()["code"] == "ledger_not_held"
    assert h.counts()[0] == 0 and h.calls == []
    assert h.retry({"commandKey": KEY, "expectedConnectionRevision": 1, "expectedScanFingerprint": fingerprint}).status_code == 409


def test_read_only_wrong_creator_and_malformed_requests_are_refused(h):
    frozen_scan(h.engine, h.account)
    h.push_out()
    fingerprint = h.read().json()["scanFingerprint"]
    good = {"commandKey": KEY, "expectedConnectionRevision": 1, "expectedScanFingerprint": fingerprint}
    assert h.retry(good, extra=headers(manage=False)).status_code == 403
    assert h.retry(good, extra=headers(creators=(OTHER,))).status_code == 404
    assert h.retry(good, account="no-such-account").status_code == 404
    for body in ({**good, "actor": STAFF}, {k: v for k, v in good.items() if k != "commandKey"}, {**good, "commandKey": "short"},
                 {**good, "expectedConnectionRevision": "1"}, {**good, "expectedConnectionRevision": True},
                 {**good, "expectedScanFingerprint": "A" * 64}, [good]):
        assert h.retry(body).status_code == 400, body
    url = f"{AGENCY}/control/v1/accounts/{h.account}/ledger/retry"
    assert h.client.post(url, json=good, headers={**headers(), "Authorization": "Bearer wrong", "X-OnlyFans-Control-Version": "of-controls.v1"}).status_code == 401
    assert h.client.post(url, json=good, headers={**headers(), "Authorization": CONTROL["Authorization"]}).status_code == 400
    assert h.client.post(url, content=b'{"commandKey":"x","commandKey":"y"}', headers={**headers(), **CONTROL, "content-type": "application/json"}).status_code == 400
    assert h.client.post(url, json=good, headers={**headers(), **READ}).status_code == 401  # a read credential is not a control credential
    assert h.counts()[0] == 0 and h.scan()["next_poll_at"] > utcnow()
    # internal service contract: no agency grant, so the trusted gateway must name an allowed actor
    plain = f"/onlyfans/control/v1/accounts/{h.account}/ledger/retry"
    assert h.client.post(plain, json=good, headers=CONTROL).status_code == 400
    assert h.client.post(plain, json=good, headers={**CONTROL, "X-OnlyFans-Actor": "root"}).status_code == 400
    actor = "op-session:" + "a" * 64
    assert h.client.post(plain, json=good, headers={**CONTROL, "X-OnlyFans-Actor": actor}).status_code == 200
    with Session(h.engine) as s:
        assert s.get(LedgerRetryCommand, (h.account, KEY)).actor == actor


def test_retry_command_rows_are_append_only(h):
    frozen_scan(h.engine, h.account)
    assert h.retry().status_code == 200
    for statement in ("UPDATE of_ledger_retry_command SET result='queued'", "DELETE FROM of_ledger_retry_command"):
        with pytest.raises(DBAPIError, match="append-only"), h.engine.begin() as c:
            c.execute(text(statement))
    assert h.counts()[0] == 1
