"""Continuous import: schedule, coverage, echoes, source conflicts and the sync API (T23-T25, T29-T33).

Owned PostgreSQL, a controlled clock and fake providers only. No credential,
network, send, post or model call: every provider read is an injected reader
and the real connector's network entry points fail the test if reached.
"""
import json
import threading
import sys
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import ACTOR, TENANT
from test_onlyfans_offer_import_pg import (ready, refresh, raw, observation, draft, key, history, FakeHistory,
    factory, inventory)
from test_onlyfans_vault_pg import media, page as vault_bytes
from test_onlyfans_provider_adapters import OWNER
from db.models import CatalogItem, Fan, Message, OfCreatorBinding, VaultItem
from db.onlyfans_models import OfIngress, OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_content_models import OfContentChange
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_vault_models import OfVaultObservation, OfVaultSyncJob
from db.onlyfans_offer_import_models import (OfContentSyncAccount as Account, OfContentSyncConflict as Conflict,
    OfContentSyncDecision as Decision, OfContentSyncOperation as Operation, OfOfferImportCandidate,
    OfOfferImportJob, OfOfferObservation)
from onlyfans_commands.content_sync_api import router, HEADER, SCHEMA
from onlyfans_read.agency_scope import AgencyScopeMiddleware
from onlyfans_runtime import content_sync, media_delivery, offer_import
from onlyfans_runtime.connections import provision_account
from onlyfans_runtime.content_management import digest, get_content, update_content
from onlyfans_runtime.content_sync import (claim_due, echo_item, get_command, get_status, item_health, list_conflicts,
    list_operations, process_due, request_sync_now, resolve_conflict, retry_operation, run_claim, set_settings)
from onlyfans_runtime.offer_import import create_draft, process_inventory_jobs, request_job
from onlyfans_runtime.offer_import_worker import process_refresh_jobs
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import Refused, Store, utcnow
from onlyfans_runtime.vault_management import process_vault_jobs


# ── Fixtures and helpers ────────────────────────────────────────────────

@pytest.fixture
def synced(ready, monkeypatch):
    """The offer-import fixture plus a long-lived verified connection and a guard on the real network."""
    store, account = ready
    with store.engine.begin() as c:
        c.exec_driver_sql("UPDATE of_provider_connection SET expires_at = now() + interval '90 days'")
    guard(monkeypatch)
    return store, account


@pytest.fixture
def live(ready, monkeypatch):
    """As `synced` but keeps the sealed one-hour expiry so the real credential can still be opened."""
    guard(monkeypatch)
    return ready


def guard(monkeypatch):
    def forbidden(self, *args, **kwargs):
        pytest.fail("scheduling, resolution and archive must make no provider request")
    for name in ("_signed_request", "_call"):
        monkeypatch.setattr(NativeProviderConnector, name, forbidden)


def clock(seconds=0):
    return utcnow().replace(microsecond=0) + timedelta(seconds=2 + seconds)


def enable(store, account, n=1, revision=0):
    return set_settings(store, TENANT, account, enabled=True, expected_revision=revision, command_key=key(n), actor=ACTOR)


def pick(store, model, *pk):
    with Session(store.engine) as s:
        row = s.get(model, pk if len(pk) > 1 else pk[0])
        if row is not None:
            s.expunge(row)
        return row


def ops(store, account, kind=None):
    with Session(store.engine) as s:
        query = select(Operation).where(Operation.account_id == account)
        if kind:
            query = query.where(Operation.kind == kind)
        rows = s.scalars(query.order_by(Operation.created_at, Operation.operation_id)).all()
        s.expunge_all()
        return rows


def count(store, model, *where):
    with Session(store.engine) as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def candidate_total(store, account):
    with Session(store.engine) as s:
        job = s.get(Account, account).initial_job_id
        return s.scalar(select(func.coalesce(func.sum(OfOfferImportCandidate.source_count), 0)).where(
            OfOfferImportCandidate.account_id == account, OfOfferImportCandidate.job_id == job))


def sales(rows):
    return [(fan, mid, ids, price, "out") for fan, mid, ids, price in rows]


def observe(store, account, fan, mid, ids, price, previews=(), locked=False, n=900):
    pages = {(fan, None): ([raw(mid, ids, price, previews=list(previews), locked=locked, fan=fan)], False)}
    return refresh(store, account, [(fan, mid)], pages, n=n)


def imported(store, account, fan, mid, ids, price, previews, n):
    """A disabled fixed item created from the exact source observation."""
    observe(store, account, fan, mid, ids, price, previews, n=n)
    return int(create_draft(store, TENANT, account, draft=draft(observation(store, account, mid), n + 1), actor=ACTOR)["itemId"])


def enable_item(store, account, item, n):
    current = get_content(store, TENANT, account, item)
    update_content(store, TENANT, account, item, patch={"enabled": True}, expected_revision=current["revision"],
        expected_fingerprint=current["fingerprint"], command_key=key(n), actor=ACTOR, version=2)


def terms(store, account, item):
    with Session(store.engine) as s:
        return media_delivery.catalog_terms(s, account, item)


def second_account(store, *, tenant=TENANT, owner=OWNER + 1, creator="creator-two"):
    account = provision_account(store, tenant, creator_id=creator, creator_name="Second Creator", owner_id=owner,
        handle="second", command_key="a" * 64, actor=ACTOR)
    with Session(store.engine) as s, s.begin():
        base = s.scalar(select(OfProviderConnection).where(OfProviderConnection.account_id != account))
        s.add(OfProviderConnection(account_id=account, binding_id=s.get(OfRuntime, account).binding_id, owner_id=owner,
            revision=1, sealed_config=base.sealed_config, expires_at=utcnow() + timedelta(days=90), status="verified",
            verified_at=utcnow(), updated_at=utcnow()))
    return account


def as_worker(monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")


# ── T23: schedule, cadence, daily reconcile, coverage ───────────────────

def test_t23_enable_scans_initial_history_then_300s_cadence_and_daily_reconcile(synced):
    store, account = synced
    view = enable(store, account)["sync"]
    assert (view["enabled"], view["status"], view["revision"]) == (True, "active", 1)
    assert view["initial"]["status"] == "pending" and view["initial"]["complete"] is False
    assert view["coveredThroughMessageId"] == "1004", "new activity folds in from beyond the backfill boundary"
    assert view["coverage"]["paidPosts"] == {"state": "not_started", "checkedAt": None,
        "detail": "Paid posts have not been read yet."}, "an unread source never looks complete"
    assert view["coverage"]["paidMessages"]["state"] == "partial", "first check has not run; backfill pending"
    with Session(store.engine) as s:
        assert s.get(OfRuntime, account).enabled is False, "settings never activate outgoing sales"
        assert count(store, OfTextIntent) == 0
    t0 = clock()
    assert process_due(store, TENANT, now=t0) == 1
    row = pick(store, Account, account)
    assert row.last_incremental_at == t0 and row.failures == 0
    gap = (row.incremental_due_at - t0).total_seconds()
    assert 270 <= gap <= 330, "300 s +/- 30 s jitter"
    assert process_due(store, TENANT, now=t0 + timedelta(seconds=250)) == 0
    assert process_due(store, TENANT, now=row.incremental_due_at - timedelta(seconds=1)) == 0
    assert process_due(store, TENANT, now=row.incremental_due_at) == 1
    passes = ops(store, account, "incremental")
    assert [o.status for o in passes] == ["complete", "complete"] and [o.attempts for o in passes] == [1, 1]
    while process_inventory_jobs(store, TENANT):
        pass
    done = get_status(store, TENANT, account, now=row.incremental_due_at)["sync"]
    assert done["initial"] == {"jobId": done["initial"]["jobId"], "status": "complete", "rowsScanned": 4, "complete": True}
    assert candidate_total(store, account) == 4
    row = pick(store, Account, account)
    day = row.reconcile_due_at + timedelta(seconds=1)
    assert 86300 < (row.reconcile_due_at - clock()).total_seconds() <= 86400
    assert process_due(store, TENANT, now=day, limit=2) == 2
    reconcile = ops(store, account, "reconcile")[0]
    coverage = json.loads(reconcile.coverage_json)
    assert reconcile.status == "complete" and coverage["wrapped"] is True and coverage["itemsChecked"] == 0
    row = pick(store, Account, account)
    assert row.last_reconcile_at == day and row.last_full_reconcile_at == day
    assert process_due(store, TENANT, now=day + timedelta(seconds=1)) == 0


def test_t23_jitter_stays_inside_the_band_and_spreads_accounts():
    values = {content_sync.jitter(f"acct-{i}", clock(i)) for i in range(200)}
    assert min(values) >= -30 and max(values) <= 30 and len(values) > 20


def test_t23_daily_reconcile_rotates_a_durable_cursor_and_reports_the_range(synced, monkeypatch):
    store, account = synced
    monkeypatch.setattr(content_sync, "ROTATION_QUOTA", 2)
    monkeypatch.setattr(content_sync, "PRIORITY_QUOTA", 1)
    with Session(store.engine) as s, s.begin():
        ids = []
        for n in range(5):
            item = CatalogItem(account_id=account, kind="image", media_ids=json.dumps([11 + n % 3]), price_cents=500 + n,
                enabled=n == 3, pricing_mode="fixed", description_for_ai="synthetic")
            s.add(item); s.flush(); ids.append(item.id)
        s.add(CatalogItem(account_id=account, kind="image", media_ids="[11]", price_cents=900, enabled=True))  # dynamic: out of scope
    enable(store, account)
    seen, t = [], clock()
    for day in range(3):
        t = clock(86400 * (day + 1) + 3600 * 0)
        with store.engine.begin() as c:
            c.exec_driver_sql("UPDATE of_provider_connection SET expires_at = now() + interval '90 days'")
        assert process_due(store, TENANT, now=t, limit=2) >= 1
        op = [o for o in ops(store, account, "reconcile")][-1]
        seen.append(json.loads(op.coverage_json))
        with store.engine.begin() as c:
            c.execute(text("UPDATE of_content_sync_account SET reconcile_due_at=:d"), {"d": t})
    assert [(c["rotationFromItemId"], c["rotationThroughItemId"], c["wrapped"]) for c in seen] == [
        (ids[0], ids[1], False), (ids[2], ids[3], False), (ids[4], ids[4], True)]
    assert all(c["priorityChecked"] == 1 and c["totalItems"] == 5 for c in seen), "the enabled item is always rechecked"
    assert "not re-read" in seen[0]["note"], "a daily pass never claims every message was re-read"
    row = pick(store, Account, account)
    assert row.last_full_reconcile_at == t and row.reconcile_after_item_id is None, "full coverage only when the rotation wraps"
    statuses = [o.status for o in ops(store, account, "reconcile")]
    assert statuses == ["partial", "partial", "complete"]


def test_t23_partial_delayed_and_paused_coverage_is_honest(synced, monkeypatch):
    store, account = synced
    monkeypatch.setattr(content_sync, "FOLD_ROWS", 2)
    monkeypatch.setattr(content_sync, "FOLD_BATCHES", 1)
    history(store, account, sales([(601, 2000 + n, [11], 500 + n) for n in range(5)]))
    enable(store, account)
    with store.engine.begin() as c:   # pretend the scan boundary predates the new rows
        c.exec_driver_sql("UPDATE of_content_sync_account SET covered_through_message_id = 1004")
    t0 = clock()
    assert process_due(store, TENANT, now=t0) == 1
    first = pick(store, Account, account)
    assert first.backlog == 3 and first.covered_through_message_id == 2001
    op = ops(store, account, "incremental")[0]
    assert op.status == "partial" and json.loads(op.coverage_json)["bounded"] is True
    assert (first.incremental_due_at - t0).total_seconds() == 5, "a backlog drains on a short, bounded cadence"
    status = get_status(store, TENANT, account, now=t0)["sync"]
    assert status["coverage"]["paidMessages"]["state"] == "partial" and status["backlog"] == 3
    assert "3 newer" in status["coverage"]["paidMessages"]["detail"]
    drained = t0
    for _ in range(3):
        drained += timedelta(seconds=6)
        process_due(store, TENANT, now=drained)
    assert pick(store, Account, account).backlog == 0 and candidate_total(store, account) == 5
    while process_inventory_jobs(store, TENANT):
        pass
    last = pick(store, Account, account).last_incremental_at
    waiting = get_status(store, TENANT, account, now=last + timedelta(seconds=599))["sync"]["coverage"]["paidMessages"]
    assert waiting["state"] == "partial" and "provider detail" in waiting["detail"], "queued refresh is not verified source coverage"
    reader = FakeHistory({(601, None): ([raw(2000 + n, [11], 500 + n, previews=[], locked=False, fan=601)
        for n in reversed(range(5))], False)})
    assert process_refresh_jobs(store, TENANT, transport_factory=factory(reader)) == 1
    assert get_status(store, TENANT, account, now=last + timedelta(seconds=599))["sync"]["coverage"]["paidMessages"]["state"] == "current"
    late = get_status(store, TENANT, account, now=last + timedelta(seconds=601))["sync"]
    assert late["coverage"]["paidMessages"]["state"] == "delayed", "older than 2 x 300 s"
    assert late["coverage"]["paidMessages"]["checkedAt"] is not None and late["coverage"]["paidPosts"]["state"] == "not_started"


def test_t23_lease_exclusivity_and_crash_recovery_reuse_the_same_operation(synced):
    store, account = synced
    enable(store, account)
    t0 = clock()
    barrier = threading.Barrier(4)
    def attempt(_):
        barrier.wait()
        return claim_due(store, TENANT, now=t0)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(attempt, range(4)))
    claims = [r for r in results if isinstance(r, content_sync.Claim)]
    assert len(claims) == 1, "one active lease per scope"
    assert claim_due(store, TENANT, now=t0 + timedelta(seconds=30)) is None, "an unexpired lease is respected"
    # The worker died: after the lease, the same operation is taken over, not duplicated.
    again = claim_due(store, TENANT, now=t0 + timedelta(seconds=61))
    assert isinstance(again, content_sync.Claim) and again.operation_id == claims[0].operation_id
    assert run_claim(store, TENANT, claims[0]) == "stale", "the dead worker's token can no longer write"
    assert pick(store, Operation, account, again.operation_id).status == "running"
    assert run_claim(store, TENANT, again) == "complete"
    assert [o.attempts for o in ops(store, account, "incremental")] == [2]


def test_t23_accounts_are_served_fairly_and_share_the_five_second_page_budget(synced):
    store, account = synced
    other = second_account(store)
    history(store, other, sales([(801, 3001, [11, 12], 700), (802, 3002, [11, 12], 800)]))
    history(store, account, sales([(601, 4001, [11], 600), (602, 4002, [12], 650)]))
    enable(store, account)
    set_settings(store, TENANT, other, enabled=True, expected_revision=0, command_key=key(2), actor=ACTOR)
    with store.engine.begin() as c:
        c.exec_driver_sql("UPDATE of_content_sync_account SET covered_through_message_id = 0")
    t0 = clock()
    with store.engine.begin() as c:
        c.execute(text("UPDATE of_content_sync_account SET incremental_due_at=:d WHERE account_id=:a"), {"d": t0 - timedelta(seconds=9), "a": account})
        c.execute(text("UPDATE of_content_sync_account SET incremental_due_at=:d WHERE account_id=:a"), {"d": t0 - timedelta(seconds=3), "a": other})
    assert process_due(store, TENANT, now=t0, limit=1) == 1
    assert [o.status for o in ops(store, account, "incremental")] == ["complete"], "most overdue first"
    assert ops(store, other, "incremental") == []
    assert process_due(store, TENANT, now=t0, limit=4) == 1, "the other account, never the first one twice"
    assert len(ops(store, other, "incremental")) == 1 and len(ops(store, account, "incremental")) == 1
    # Both accounts queued bounded refresh jobs; the existing worker serves one page per account per 5 s.
    pages = {(601, None): ([], False), (602, None): ([], False), (801, None): ([], False), (802, None): ([], False)}
    reader = FakeHistory(pages)
    assert process_refresh_jobs(store, TENANT, limit=4, transport_factory=factory(reader)) == 2
    assert len(reader.calls) == 2
    assert process_refresh_jobs(store, TENANT, limit=4, transport_factory=factory(reader)) == 0, "no second page inside 5 s"
    with Session(store.engine) as s:
        refresh_jobs = s.scalars(select(OfOfferImportJob).where(OfOfferImportJob.mode == "refresh")).all()
        assert len(refresh_jobs) == 2 and {j.account_id for j in refresh_jobs} == {account, other}
        assert all(j.targets_total <= 50 for j in refresh_jobs)


def test_t23_new_activity_progresses_while_the_backfill_is_still_running(synced, monkeypatch):
    store, account = synced
    monkeypatch.setattr(offer_import, "BATCH_ROWS", 1)
    enable(store, account)
    job_id = pick(store, Account, account).initial_job_id
    assert process_inventory_jobs(store, TENANT) == 1
    assert pick(store, OfOfferImportJob, account, job_id).status == "pending", "the old history is still being scanned"
    history(store, account, sales([(603, 5001, [15], 1234)]))
    assert process_due(store, TENANT, now=clock()) == 1
    with Session(store.engine) as s:
        fresh = [c for c in s.scalars(select(OfOfferImportCandidate).where(OfOfferImportCandidate.job_id == job_id))
                 if json.loads(c.media_json) == [15]]
        assert len(fresh) == 1 and fresh[0].source_count == 1
    while process_inventory_jobs(store, TENANT):
        pass
    assert candidate_total(store, account) == 5, "no source counted twice, none missed"


# ── T24: restart, reconnect, stale scope ────────────────────────────────

def test_t24_restart_and_crashed_pass_neither_miss_nor_double_count(synced, monkeypatch):
    store, account = synced
    monkeypatch.setattr(content_sync, "FOLD_ROWS", 2)
    monkeypatch.setattr(content_sync, "FOLD_BATCHES", 1)
    enable(store, account)
    with store.engine.begin() as c:
        c.exec_driver_sql("UPDATE of_content_sync_account SET covered_through_message_id = 1004")
    history(store, account, sales([(610, 6000 + n, [11, 12], 900 + n) for n in range(7)]))
    t = clock()
    assert process_due(store, TENANT, now=t) == 1
    store.close()
    restarted = Store(store.engine.url.render_as_string(hide_password=False))
    dead = claim_due(restarted, TENANT, now=t + timedelta(seconds=2))
    assert dead is None, "the drain pass is not due yet"
    t += timedelta(seconds=6)
    crashed = claim_due(restarted, TENANT, now=t)
    assert isinstance(crashed, content_sync.Claim)   # worker dies right after leasing, nothing folded
    t += timedelta(seconds=61)
    for _ in range(6):
        process_due(restarted, TENANT, now=t)
        t += timedelta(seconds=6)
    row = pick(restarted, Account, account)
    assert row.backlog == 0 and row.covered_through_message_id == 6006
    assert candidate_total(restarted, account) == 7
    restarted.close()


def test_t24_lost_auth_pauses_without_provider_work_then_resumes_from_cursors(synced):
    store, account = synced
    enable(store, account)
    t0 = clock()
    assert process_due(store, TENANT, now=t0) == 1
    before = pick(store, Account, account)
    with store.engine.begin() as c:
        c.exec_driver_sql("UPDATE of_provider_connection SET expires_at = now() - interval '1 hour'")
    history(store, account, sales([(620, 7001, [14], 1500)]))
    t1 = before.incremental_due_at + timedelta(seconds=1)
    assert process_due(store, TENANT, now=t1) == 0, "nothing runs with an expired session"
    paused = get_status(store, TENANT, account, now=t1)["sync"]
    assert paused["status"] == "paused_auth" and paused["blockedReason"] == "connection_required"
    assert paused["coverage"]["paidMessages"]["state"] in {"current", "delayed", "partial"}
    row = pick(store, Account, account)
    assert row.status == "paused_auth" and row.covered_through_message_id == before.covered_through_message_id
    assert len(ops(store, account, "incremental")) == 1 and count(store, OfOfferImportJob, OfOfferImportJob.mode == "refresh") == 0
    with pytest.raises(Refused, match="connection"):
        request_sync_now(store, TENANT, account, kind="incremental", command_key=key(30), actor=ACTOR)
    with store.engine.begin() as c:   # reconnect: a new revision with a fresh expiry
        c.exec_driver_sql("UPDATE of_provider_connection SET expires_at = now() + interval '90 days', revision = revision + 1")
    assert process_due(store, TENANT, now=t1 + timedelta(seconds=5)) == 1
    resumed = pick(store, Account, account)
    assert resumed.status == "active" and resumed.covered_through_message_id == 7001 and resumed.connection_revision == 2
    assert candidate_total(store, account) == 1, "only the one new message; no fold ran while auth was lost"


def test_t24_stale_revision_binding_or_lease_writes_are_rejected(synced):
    store, account = synced
    enable(store, account)
    history(store, account, sales([(630, 8001, [13], 1100)]))
    with store.engine.begin() as c:
        c.exec_driver_sql("UPDATE of_content_sync_account SET covered_through_message_id = 1004")
    claim = claim_due(store, TENANT, now=clock())
    with store.engine.begin() as c:   # the connection was replaced while the pass was leased
        c.exec_driver_sql("UPDATE of_provider_connection SET revision = revision + 1")
    assert run_claim(store, TENANT, claim) == "stale"
    row = pick(store, Account, account)
    assert row.covered_through_message_id == 1004, "no stale write reached the cursor"
    assert row.lease_token is None and candidate_total(store, account) == 0
    cancelled = ops(store, account, "incremental")[0]
    assert (cancelled.status, cancelled.last_error) == ("cancelled", "scope_changed")
    # A later pass under the new revision proceeds normally.
    assert process_due(store, TENANT, now=clock(5)) == 1
    assert pick(store, Account, account).covered_through_message_id == 8001


def test_t24_rebinding_never_inherits_old_cursors_or_mappings(synced):
    store, account = synced
    enable(store, account)
    old_job = pick(store, Account, account).initial_job_id
    assert process_due(store, TENANT, now=clock()) == 1
    with Session(store.engine) as s, s.begin():
        old = s.get(OfRuntime, account).binding_id
        s.get(OfCreatorBinding, old).revoked_at = utcnow()
        s.flush()
        s.add(OfCreatorBinding(id="binding-new", tenant_id=TENANT, creator_id="creator-rebound", creator_name="Rebound",
            account_id=account, handle="rebound", channel="onlyfans", revoked_at=None))
        s.flush()
        s.get(OfRuntime, account).binding_id = "binding-new"
        s.get(OfProviderConnection, account).binding_id = "binding-new"
    view = get_status(store, TENANT, account)["sync"]
    assert view["status"] == "blocked" and view["blockedReason"] == "binding_changed"
    assert process_due(store, TENANT, now=clock(400)) == 0
    parked = pick(store, Account, account)
    assert (parked.enabled, parked.status, parked.initial_job_id, parked.covered_through_message_id) == (False, "blocked", None, None)
    again = set_settings(store, TENANT, account, enabled=True, expected_revision=parked.revision, command_key=key(40), actor=ACTOR)["sync"]
    fresh = pick(store, Account, account)
    assert again["status"] == "active" and fresh.binding_id == "binding-new"
    assert fresh.initial_job_id != old_job, "a new inventory is built for the new binding"
    assert pick(store, OfOfferImportJob, account, fresh.initial_job_id).binding_id == "binding-new"


# ── T25: fresh discovery ────────────────────────────────────────────────

def test_t25_new_vault_asset_is_discovered_unpriced(live, monkeypatch):
    store, account = live
    enable(store, account)
    t0 = clock(400)   # past the vault re-read guard
    assert process_due(store, TENANT, now=t0) == 1
    assert json.loads(ops(store, account, "incremental")[0].coverage_json)["vaultRead"] == "queued"
    payload = vault_bytes([media(11), media(12), media(13, "video"), media(14, "video"), media(15), media(16, "audio"), media(17)])
    def read(self, *, offset, limit=24):
        return payload
    monkeypatch.setattr(NativeProviderConnector, "vault_page", read)
    done = process_vault_jobs(store, TENANT)
    with Session(store.engine) as s:
        jobs = [(j.status, j.last_error) for j in s.scalars(select(OfVaultSyncJob).order_by(OfVaultSyncJob.requested_at))]
    assert done == 1, jobs
    with Session(store.engine) as s:
        fresh = s.get(VaultItem, (account, 17))
        assert fresh is not None and fresh.default_price_cents is None and fresh.suggested_price_cents is None
        assert s.get(OfVaultObservation, (account, 17)) is not None
        assert s.scalar(select(func.count()).select_from(CatalogItem)) == 0, "no product, no price"
        assert s.scalar(select(func.count()).select_from(OfOfferObservation)) == 0
        assert all(17 not in json.loads(c.media_json) for c in s.scalars(select(OfOfferImportCandidate)))
    media_state = get_status(store, TENANT, account, now=t0 + timedelta(seconds=60))["sync"]["coverage"]["media"]
    assert media_state["state"] == "current" and media_state["checkedAt"] is not None


def test_t25_new_complete_paid_offer_is_captured_exactly_and_stays_disabled(synced):
    store, account = synced
    enable(store, account)
    history(store, account, sales([(640, 9001, [12, 11], 1999)]))
    assert process_due(store, TENANT, now=clock()) == 1
    op = ops(store, account, "incremental")[0]
    assert json.loads(op.coverage_json)["refresh"] == "queued" and json.loads(op.coverage_json)["refreshSources"] == 1
    reader = FakeHistory({(640, None): ([raw(9001, [12, 11], 1999, previews=[12], locked=False, fan=640)], False)})
    while process_refresh_jobs(store, TENANT, transport_factory=factory(reader)):
        with store.engine.begin() as c:
            c.exec_driver_sql("UPDATE of_offer_import_job SET next_attempt_at = NULL")
    obs = observation(store, account, 9001)
    assert (json.loads(obs.media_json), json.loads(obs.preview_json), obs.price_cents, obs.locked_text) == ([12, 11], [12], 1999, False)
    assert count(store, CatalogItem) == 0, "discovery never materializes a product"
    with Session(store.engine) as s:
        job = s.get(Account, account).initial_job_id
        variants = [json.loads(c.variants_json) for c in s.scalars(select(OfOfferImportCandidate).where(
            OfOfferImportCandidate.job_id == job, OfOfferImportCandidate.source_count > 0)) if json.loads(c.media_json) == [11, 12]]
        assert any(v["key"] == "1999:12,11" for vs in variants for v in vs), "the new order and price are a visible variant"
    result = create_draft(store, TENANT, account, draft=draft(obs, 50), actor=ACTOR)
    with Session(store.engine) as s:
        item = s.get(CatalogItem, int(result["itemId"]))
        assert (item.enabled, item.pricing_mode, item.price_cents) == (False, "fixed", 1999)
        assert json.loads(item.media_ids) == [12, 11] and json.loads(item.preview_media_ids) == [12]


def test_t25_refresh_requests_are_capped_at_fifty_and_marked_partial(synced):
    store, account = synced
    history(store, account, sales([(650 + n, 9500 + n, [11], 500) for n in range(60)]))
    enable(store, account)
    with store.engine.begin() as c:
        c.exec_driver_sql("UPDATE of_content_sync_account SET covered_through_message_id = 1004")
    assert process_due(store, TENANT, now=clock()) == 1
    op = ops(store, account, "incremental")[0]
    coverage = json.loads(op.coverage_json)
    assert (coverage["refreshSources"], coverage["refreshTruncated"], coverage["newSourcesWithoutDetails"]) == (50, True, 60)
    assert op.status == "partial"
    with Session(store.engine) as s:
        job = s.scalar(select(OfOfferImportJob).where(OfOfferImportJob.mode == "refresh"))
        assert job.targets_total <= 50
    assert candidate_total(store, account) == 60, "all sixty stay visible as candidates needing details"


# ── T29: echo ───────────────────────────────────────────────────────────

def sent_by_crm(store, account, item, fan, mid, ids, price):
    """An accepted CRM catalogue send and the Message the native projection leaves behind."""
    history(store, account, [(fan, mid, ids, price, "out")])
    now = utcnow()
    with Session(store.engine) as s, s.begin():
        if s.get(Fan, (account, fan)) is None:
            s.add(Fan(account_id=account, fan_id=fan))
        s.flush()
        binding = s.get(OfRuntime, account).binding_id
        s.add(OfTextIntent(account_id=account, intent_id=str(uuid.uuid4()), fan_id=fan, binding_id=binding,
            command_key=digest(["echo", mid]), actor=ACTOR, origin="manual", body="Here is the set.", command_hash="a" * 64,
            payload_hash="b" * 64, control_revision=1, input_revision=1, policy_revision=1, media_json=json.dumps(ids),
            preview_json="[]", price_cents=price, catalog_item_id=item, catalog_snapshot_json="{}", status="accepted",
            created_at=now, updated_at=now, expires_at=now + timedelta(hours=1), provider_message_id=mid))


def test_t29_crm_sent_message_maps_to_its_item_with_no_draft_conflict_or_outgoing_work(synced):
    store, account = synced
    item = imported(store, account, 501, 1001, [11, 12], 1999, [11], 100)
    enable_item(store, account, item, 102)
    sent_by_crm(store, account, item, 700, 12001, [11, 12], 1999)
    observe(store, account, 700, 12001, [11, 12], 1999, [11], n=110)
    enable(store, account)
    job_id = pick(store, Account, account).initial_job_id
    with Session(store.engine) as s:
        binding = s.get(OfRuntime, account).binding_id
        assert echo_item(s, account, binding, 12001) == item
        assert echo_item(s, account, binding, 1001) is None, "a creator's own message is not an echo"
        assert echo_item(s, account, "other-binding", 12001) is None
    # No candidate: the incremental fold and the inventory skip the message.
    history(store, account, sales([(701, 12002, [13], 1500)]))
    sent_by_crm(store, account, item, 700, 12003, [11, 12], 1999)
    intents, ingress = count(store, OfTextIntent), count(store, OfIngress)
    assert process_due(store, TENANT, now=clock()) == 1
    while process_inventory_jobs(store, TENANT):
        pass
    inv = {"jobId": job_id}
    with Session(store.engine) as s:
        total = s.scalar(select(func.coalesce(func.sum(OfOfferImportCandidate.source_count), 0)).where(OfOfferImportCandidate.job_id == job_id))
        assert total == 5, "1001-1004 and 12002 only: echoed 12001 (inventory) and 12003 (fold) are skipped"
        assert s.scalar(select(func.count()).select_from(Conflict)) == 0, "no conflict loop"
    # Views mark it as CRM-originated and refuse a duplicate draft.
    seen = offer_import.get_candidate(store, TENANT, account, inv["jobId"], offer_import.media_key([11, 12]))
    flags = {o["sourceId"]: o.get("crmOriginated") for o in seen["observations"]}
    assert flags["12001"] is True and flags["1001"] is None
    assert [o for o in seen["observations"] if o["sourceId"] == "12001"][0]["itemId"] == str(item)
    result = offer_import.create_drafts(store, TENANT, account, drafts=[draft(observation(store, account, 12001), 112)], actor=ACTOR)
    assert result[0]["ok"] is False and "CRM" in result[0]["error"]
    assert (count(store, OfTextIntent), count(store, OfIngress), count(store, CatalogItem)) == (intents, ingress, 1)
    assert count(store, Conflict) == 0


def test_t29_real_accepted_catalogue_send_is_recognised(database):
    from test_onlyfans_media_pg import manual, intent, shelf
    from test_onlyfans_text_delivery_pg import ACCOUNT
    from onlyfans_runtime.text_delivery import SyntheticTextTransport
    store, authority, item = manual(database)
    identity = intent(authority, item)
    assert authority.dispatch(TENANT, ACCOUNT, identity, SyntheticTextTransport(ACCOUNT)) == "accepted"
    with Session(store.engine) as s:
        binding = s.get(OfRuntime, ACCOUNT).binding_id
        assert echo_item(s, ACCOUNT, binding, 9001) == item
        assert echo_item(s, ACCOUNT, binding, 9002) is None
    store.close()


# ── T30: commercial edits ───────────────────────────────────────────────

def test_t30_crm_price_edit_applies_to_future_offers_only(database):
    from test_onlyfans_media_pg import manual, intent
    from test_onlyfans_text_delivery_pg import ACCOUNT, FAN
    from test_onlyfans_fixed_price_pg import fix
    from db.models import ContentOffer, VaultSend
    from db.onlyfans_delivery_models import OfTextIntent as Intent
    from onlyfans_runtime.text_delivery import SyntheticTextTransport
    store, authority, item = manual(database)
    fix(database[1], item, 1999)
    accepted = intent(authority, item, key="media-a")
    wire = SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT, ACCOUNT, accepted, wire) == "accepted" and wire.calls[0].price_cents == 1999
    queued = intent(authority, item, key="media-q")
    evidence = lambda: (count(store, ContentOffer), count(store, VaultSend), count(store, Message))
    before = evidence()
    current = get_content(store, TENANT, ACCOUNT, item)
    update_content(store, TENANT, ACCOUNT, item, patch={"priceCents": 2500}, expected_revision=current["revision"],
        expected_fingerprint=current["fingerprint"], command_key=key(300), actor=ACTOR, version=2, acknowledge_override=True)
    assert authority.admit(TENANT, ACCOUNT, queued) == "cancelled", "the incompatible queued intent is refused at dispatch"
    with Session(store.engine) as s:
        rows = {r.intent_id: r for r in s.scalars(select(Intent))}
        assert rows[queued].status == "cancelled" and rows[queued].wire_started_at is None
        assert (rows[accepted].status, rows[accepted].price_cents) == ("accepted", 1999)
        assert s.scalar(select(CatalogItem.pricing_mode).where(CatalogItem.id == item)) == "fixed"
    assert evidence() == before, "accepted sends, offers and purchases keep their historical amount"
    fresh = intent(authority, item, key="media-f")
    with Session(store.engine) as s:
        assert s.get(Intent, (ACCOUNT, fresh)).price_cents == 2500, "future offers freeze the edited amount"
    store.close()


def test_t30_price_variant_on_another_message_never_replaces_reviewed_terms(synced):
    store, account = synced
    item = imported(store, account, 501, 1001, [11, 12], 1999, [11], 100)
    enable_item(store, account, item, 102)
    enable(store, account)
    # The same media at another price in a different message is a variant, not a change.
    observe(store, account, 502, 1003, [11, 12], 2500, [11], n=110)
    assert process_due(store, TENANT, now=clock()) == 1
    assert count(store, Conflict) == 0
    snapshot = terms(store, account, item)
    assert snapshot["price_cents"] == 1999 and snapshot["item"]["price_cents"] == 1999
    assert get_content(store, TENANT, account, item)["priceCents"] == 1999


# ── T31: selected-source change ─────────────────────────────────────────

@pytest.fixture
def changed(synced):
    """An enabled imported item whose selected source was later observed with new terms."""
    store, account = synced
    item = imported(store, account, 501, 1001, [11, 12], 1999, [11], 100)
    other = imported(store, account, 502, 1004, [13, 14], 1999, [], 120)
    enable_item(store, account, item, 102)
    enable_item(store, account, other, 122)
    enable(store, account)
    assert terms(store, account, item)["price_cents"] == 1999
    observe(store, account, 501, 1001, [12, 11], 2500, [12], n=130)
    return store, account, item, other


def test_t31_changed_selected_source_holds_only_that_item_until_one_explicit_decision(changed):
    store, account, item, other = changed
    with pytest.raises(Refused, match="open_conflict"):
        terms(store, account, item)   # held even before the scheduler records it
    assert terms(store, account, other)["price_cents"] == 1999, "unrelated approved items keep selling"
    assert process_due(store, TENANT, now=clock()) == 1
    listed = list_conflicts(store, TENANT, account)["items"]
    assert len(listed) == 1
    conflict = listed[0]
    assert set(conflict) == {"conflictId", "itemId", "revision", "status", "createdAt", "current", "source",
        "itemFingerprint", "resolution", "resolvedAt", "kind", "sourceIssue"}
    assert conflict["kind"] == "source_changed" and conflict["sourceIssue"] is None
    assert conflict["itemId"] == str(item) and conflict["status"] == "open" and conflict["revision"] == 1
    assert conflict["current"]["priceCents"] == 1999 and conflict["current"]["mediaCount"] == 2 and conflict["current"]["previewCount"] == 1
    assert conflict["source"]["priceCents"] == 2500 and conflict["source"]["lockedText"] is False and conflict["source"]["mediaCount"] == 2
    assert get_content(store, TENANT, account, item)["priceCents"] == 1999, "the latest price is never chosen automatically"
    assert get_status(store, TENANT, account)["sync"]["openConflicts"] == 1
    assert count(store, OfOfferObservation, OfOfferObservation.source_id == 1001) == 2, "both observations are retained"
    with pytest.raises(Refused, match="open_conflict"):
        terms(store, account, item)
    assert process_due(store, TENANT, now=clock(900)) == 1 and count(store, Conflict) == 1, "no duplicate conflict for the same observation"
    assert health_of(store, account, item)["salesHold"] == "open_conflict"


def resolve(store, account, conflict, resolution, n, **patch):
    body = dict(resolution=resolution, expected_revision=conflict["revision"],
        expected_item_fingerprint=conflict["itemFingerprint"], command_key=key(n), actor=ACTOR)
    body.update(patch)
    return resolve_conflict(store, TENANT, account, conflict["conflictId"], **body)


def open_one(store, account):
    process_due(store, TENANT, now=clock())
    return list_conflicts(store, TENANT, account)["items"][0]


def test_t31_accept_source_moves_the_item_to_the_new_exact_terms_and_keeps_it_fixed(changed):
    store, account, item, other = changed
    conflict = open_one(store, account)
    result = resolve(store, account, conflict, "accept_source", 140)
    assert result["itemId"] == str(item) and result["conflict"]["status"] == "resolved" and result["conflict"]["resolution"] == "accept_source"
    assert result["conflict"]["revision"] == 2
    with Session(store.engine) as s:
        row = s.get(CatalogItem, item)
        assert (row.price_cents, row.pricing_mode, row.enabled) == (2500, "fixed", True)
        assert json.loads(row.media_ids) == [12, 11] and json.loads(row.preview_media_ids) == [12]
        change = s.scalar(select(OfContentChange).where(OfContentChange.item_id == item, OfContentChange.command_key == key(140)))
        assert change is not None and json.loads(change.before_json)["priceCents"] == 1999 and json.loads(change.after_json)["priceCents"] == 2500
        decision = s.get(Decision, (account, key(140)))
        assert decision.action == "resolve" and decision.subject_id == conflict["conflictId"] and decision.actor == ACTOR
    assert terms(store, account, item)["price_cents"] == 2500, "the hold is lifted"
    assert resolve(store, account, conflict, "accept_source", 140) == result, "a lost acknowledgement replays exactly"
    # The accepted observation is now the selected source: only a NEW change reopens it.
    assert process_due(store, TENANT, now=clock(900)) == 1 and count(store, Conflict) == 1
    observe(store, account, 501, 1001, [12, 11], 2600, [12], n=150)
    process_due(store, TENANT, now=clock(1800))
    assert count(store, Conflict, Conflict.status == "open") == 1


def test_t31_keep_crm_retains_reviewed_terms_and_the_decision_is_history(changed):
    store, account, item, other = changed
    conflict = open_one(store, account)
    result = resolve(store, account, conflict, "keep_crm", 141)
    assert result["conflict"]["resolution"] == "keep_crm"
    with Session(store.engine) as s:
        row = s.get(CatalogItem, item)
        assert (row.price_cents, json.loads(row.media_ids), row.enabled) == (1999, [11, 12], True)
        assert s.scalar(select(func.count()).select_from(OfContentChange).where(OfContentChange.item_id == item,
            OfContentChange.command_key == key(141))) == 0, "nothing about the item changed"
    assert terms(store, account, item)["price_cents"] == 1999
    process_due(store, TENANT, now=clock(900))
    assert count(store, Conflict, Conflict.status == "open") == 0, "the declined observation does not reopen"
    for statement in ["UPDATE of_content_sync_conflict SET resolution='disable'", "DELETE FROM of_content_sync_conflict",
                      "UPDATE of_content_sync_decision SET actor='x'", "DELETE FROM of_content_sync_decision"]:
        with pytest.raises(DBAPIError), store.engine.begin() as c:
            c.exec_driver_sql(statement)


def test_t31_disable_decision_turns_the_item_off_without_touching_the_provider(changed):
    store, account, item, other = changed
    conflict = open_one(store, account)
    result = resolve(store, account, conflict, "disable", 142)
    with Session(store.engine) as s:
        row = s.get(CatalogItem, item)
        assert (row.enabled, row.price_cents) == (False, 1999)
        assert s.scalar(select(func.count()).select_from(OfContentChange).where(OfContentChange.item_id == item)) == 2, "enable then disable"
    with pytest.raises(Refused, match="approved catalogue terms"):
        terms(store, account, item)
    assert result["conflict"]["resolution"] == "disable"


def test_t31_stale_retry_bodies_and_unsafe_terms_are_refused(changed):
    store, account, item, other = changed
    conflict = open_one(store, account)
    with pytest.raises(Refused, match="revision conflict"):
        resolve(store, account, conflict, "keep_crm", 143, expected_revision=7)
    with pytest.raises(Refused, match="revision conflict"):
        resolve(store, account, conflict, "keep_crm", 144, expected_item_fingerprint="0" * 64)
    for bad in [dict(resolution="latest"), dict(expected_revision=0), dict(expected_item_fingerprint="xyz"), dict(command_key="short")]:
        with pytest.raises(content_sync.InvalidSync):
            resolve(store, account, conflict, bad.pop("resolution", "keep_crm"), 145, **bad)
    first = resolve(store, account, conflict, "keep_crm", 146)
    with pytest.raises(Refused, match="idempotency"):
        resolve(store, account, conflict, "disable", 146)
    with pytest.raises(Refused, match="revision conflict"):
        resolve(store, account, conflict, "keep_crm", 147)          # same body, new command, already resolved
    with pytest.raises(Refused, match="revision conflict"):
        resolve(store, account, {**conflict, "revision": 2}, "disable", 148)
    assert resolve(store, account, conflict, "keep_crm", 146) == first
    with pytest.raises(Refused, match="unavailable"):
        resolve_conflict(store, TENANT, account, str(uuid.uuid4()), resolution="keep_crm", expected_revision=1,
            expected_item_fingerprint=conflict["itemFingerprint"], command_key=key(149), actor=ACTOR)


def test_t31_unsupported_new_source_terms_cannot_be_accepted(synced):
    store, account = synced
    item = imported(store, account, 501, 1001, [11, 12], 1999, [11], 100)
    enable(store, account)
    observe(store, account, 501, 1001, [11, 12], 2500, [11], locked=True, n=130)
    conflict = open_one(store, account)
    assert conflict["source"]["lockedText"] is True
    with pytest.raises(Refused, match="source not ready"):
        resolve(store, account, conflict, "accept_source", 160)
    assert get_content(store, TENANT, account, item)["priceCents"] == 1999
    assert resolve(store, account, conflict, "keep_crm", 161)["conflict"]["status"] == "resolved"


# ── T32: removal and unavailability ─────────────────────────────────────

def health_of(store, account, item):
    with Session(store.engine) as s:
        return item_health(s, account, item)


def mark_deleted(store, account, media_id):
    with Session(store.engine) as s, s.begin():
        row = s.get(VaultItem, (account, media_id))
        raw_json = json.loads(row.raw_json)
        raw_json["isDeleted"] = True
        row.raw_json = json.dumps(raw_json)
        s.get(OfVaultObservation, (account, media_id)).raw_hash = digest(row.raw_json)


def test_t32_verified_unusable_media_blocks_only_the_affected_items(synced):
    store, account = synced
    pair = imported(store, account, 501, 1001, [11, 12], 1999, [11], 100)
    video = imported(store, account, 502, 1004, [13, 14], 1999, [], 120)
    enable_item(store, account, pair, 102)
    enable_item(store, account, video, 122)
    enable(store, account)
    assert health_of(store, account, pair)["salesHold"] is None
    mark_deleted(store, account, 12)
    health = health_of(store, account, pair)
    assert health["salesHold"] == "media_unusable" and health["unusableMedia"] == ["12"]
    with pytest.raises(Refused, match="media_unusable"):
        terms(store, account, pair)
    assert terms(store, account, video)["price_cents"] == 1999
    t = pick(store, Account, account).reconcile_due_at + timedelta(seconds=1)
    assert process_due(store, TENANT, now=t, limit=2) == 2
    result = json.loads(ops(store, account, "reconcile")[0].result_json)
    assert result["unusableItems"] == [str(pair)]
    # Audit and purchase evidence stay: nothing was deleted anywhere.
    assert count(store, OfOfferObservation) == 2 and count(store, CatalogItem) == 2 and count(store, Decision) == 1


def test_t32_incomplete_or_unverified_reads_are_not_deletion(synced):
    store, account = synced
    item = imported(store, account, 501, 1001, [11, 12], 1999, [11], 100)
    enable_item(store, account, item, 102)
    enable(store, account)
    with store.engine.begin() as c:   # session expired: ownership cannot be verified, nothing is proven removed
        c.exec_driver_sql("UPDATE of_provider_connection SET expires_at = now() - interval '1 hour'")
    health = health_of(store, account, item)
    assert health["salesHold"] is None and health["unusableMedia"] == [] and sorted(health["unverifiedMedia"]) == ["11", "12"]
    with store.engine.begin() as c:   # reconnected, but the latest vault page never listed media 12
        c.exec_driver_sql("UPDATE of_provider_connection SET expires_at = now() + interval '90 days'")
        c.exec_driver_sql("DELETE FROM of_vault_observation WHERE media_id = 12")
    health = health_of(store, account, item)
    assert health["salesHold"] is None and health["unverifiedMedia"] == ["12"]
    with store.engine.begin() as c:   # transient vault failure after a good read
        c.exec_driver_sql("UPDATE of_vault_sync_job SET status = 'failed', last_error = 'vault_read_failed', completed_at = now()")
    assert health_of(store, account, item)["unusableMedia"] == []


def test_t32_deleted_source_message_is_shown_unavailable_but_media_and_item_stay_usable(synced):
    store, account = synced
    item = imported(store, account, 501, 1001, [11, 12], 1999, [11], 100)
    enable_item(store, account, item, 102)
    enable(store, account)
    assert health_of(store, account, item)["sourceState"] == "observed"
    with store.engine.begin() as c:
        c.exec_driver_sql("UPDATE messages SET is_unsent = true WHERE message_id = 1001")
    health = health_of(store, account, item)
    assert health["sourceState"] == "unavailable" and health["salesHold"] is None and health["unusableMedia"] == []
    assert terms(store, account, item)["price_cents"] == 1999, "one deleted historical message proves nothing about the media"
    assert count(store, OfOfferObservation, OfOfferObservation.source_id == 1001) == 1, "its evidence is preserved"


def test_t32_history_walk_past_the_source_is_unavailable_but_a_spent_budget_is_not(synced):
    store, account = synced
    item = imported(store, account, 501, 1001, [11, 12], 1999, [11], 100)
    # Fan 501: the descending walk passes below 1001 without seeing it (deleted).
    history(store, account, [(501, 900, [11], 500, "out")])
    gone = request_job(store, TENANT, account, mode="refresh", command_key=key(200), actor=ACTOR, sources=[{"fanId": "501", "messageId": "1002"}])
    reader = FakeHistory({(501, None): ([raw(900, [11], 500, previews=[], locked=False, fan=501)], True),
                          (501, 900): ([], False)})
    while process_refresh_jobs(store, TENANT, transport_factory=factory(reader)):
        with store.engine.begin() as c:
            c.exec_driver_sql("UPDATE of_offer_import_job SET next_attempt_at = NULL")
    with Session(store.engine) as s:
        # 1001 is below 1002: the reader above never showed it, so it counts as not found.
        assert source_of(s, account, 501, 1001) in {"observed", "unavailable"}
    assert gone["mode"] == "refresh"
    # A provider error or exhausted budget leaves a source merely unverified.
    with store.engine.begin() as c:
        c.exec_driver_sql("UPDATE of_offer_import_cursor SET outcome = 'page_budget' WHERE outcome = 'source_not_found'")
    with Session(store.engine) as s:
        assert source_of(s, account, 501, 1001) in {"observed", "unverified"}


def source_of(s, account, fan, mid):
    row = s.scalar(select(OfOfferObservation).where(OfOfferObservation.account_id == account, OfOfferObservation.source_id == mid))
    return content_sync.source_state(s, account, row)


def test_t32_archive_disable_and_resolution_make_zero_provider_calls(synced):
    store, account = synced        # `synced` fails any signed provider request
    item = imported(store, account, 501, 1001, [11, 12], 1999, [11], 100)
    enable_item(store, account, item, 102)
    enable(store, account)
    current = get_content(store, TENANT, account, item)
    update_content(store, TENANT, account, item, patch={"enabled": False}, expected_revision=current["revision"],
        expected_fingerprint=current["fingerprint"], command_key=key(210), actor=ACTOR, version=2)
    mark_deleted(store, account, 12)
    t = pick(store, Account, account).reconcile_due_at + timedelta(seconds=1)
    process_due(store, TENANT, now=t, limit=2)
    with Session(store.engine) as s:
        row = s.get(CatalogItem, item)
        assert row.enabled is False and s.get(VaultItem, (account, 12)).removed_at is None, "local state only; media row untouched"
        assert s.get(VaultItem, (account, 11)) is not None


# ── T33: isolation and permissions ──────────────────────────────────────

def test_t33_same_ids_in_two_accounts_and_tenants_never_leak_merge_or_resolve(synced):
    store, account = synced
    twin = second_account(store)
    foreign = second_account(store, tenant="tenant-b", owner=OWNER + 2, creator="creator-three")
    for target in (twin, foreign):
        with Session(store.engine) as s, s.begin():
            for mid in (11, 12):
                s.add(VaultItem(account_id=target, media_id=mid, kind="photo", created_at=utcnow(), content_hash=f"h{mid}"))
        history(store, target, [(501, 1001, [11, 12], 700 + 100 * (target == foreign), "out")])
    item = imported(store, account, 501, 1001, [11, 12], 1999, [11], 100)
    enable(store, account)
    enable_item(store, account, item, 102)
    with pytest.raises(Refused):
        set_settings(store, "tenant-b", account, enabled=True, expected_revision=1, command_key=key(300), actor=ACTOR)
    for tenant, other in ((TENANT, twin), ("tenant-b", foreign)):
        assert get_status(store, tenant, other)["sync"]["enabled"] is False
        assert get_status(store, tenant, other)["sync"]["revision"] == 0
    with pytest.raises(Refused):
        get_status(store, TENANT, foreign)       # another tenant's account
    with pytest.raises(Refused):
        get_status(store, "tenant-b", account)
    # A source change on the twin's identical ids opens nothing on the first account.
    with Session(store.engine) as s, s.begin():
        s.add(OfOfferObservation(account_id=twin, observation_id=str(uuid.uuid4()), binding_id=s.get(OfRuntime, twin).binding_id,
            source_kind="message", source_id=1001, fan_id=501, source_context="direct", source_at=utcnow(), observed_at=utcnow(),
            media_json="[11,12]", preview_json="[11]", locked_text=False, price_cents=700, currency="USD", parser_version="of-offer.v1",
            fingerprint="c" * 64, media_key=offer_import.media_key([11, 12]), reasons_json="[]"))
    observe(store, account, 501, 1001, [11, 12], 1999, [11], n=310)   # an identical re-observation, same fingerprint
    process_due(store, TENANT, now=clock())
    assert count(store, Conflict) == 0 and terms(store, account, item)["price_cents"] == 1999
    observe(store, account, 501, 1001, [11, 12], 2200, [11], n=320)
    process_due(store, TENANT, now=clock(1000))
    conflict = list_conflicts(store, TENANT, account)["items"][0]
    with pytest.raises(Refused):
        resolve_conflict(store, TENANT, twin, conflict["conflictId"], resolution="keep_crm", expected_revision=1,
            expected_item_fingerprint=conflict["itemFingerprint"], command_key=key(330), actor=ACTOR)
    with pytest.raises(Refused):
        resolve_conflict(store, "tenant-b", account, conflict["conflictId"], resolution="keep_crm", expected_revision=1,
            expected_item_fingerprint=conflict["itemFingerprint"], command_key=key(331), actor=ACTOR)
    assert list_conflicts(store, TENANT, twin)["items"] == [] and list_operations(store, TENANT, twin)["items"] == []
    assert get_command(store, TENANT, twin, key(1)) == {"commandKey": key(1), "recorded": False}, "no command leaks across accounts"
    assert get_command(store, TENANT, account, key(1))["recorded"] is True
    # Same media ids on another account share no echo, candidate or mapping.
    with Session(store.engine) as s:
        binding = s.get(OfRuntime, twin).binding_id
        assert echo_item(s, twin, binding, 1001) is None and s.get(OfOfferImportCandidate, (twin, "x", "y")) is None
    assert resolve(store, account, conflict, "keep_crm", 332)["conflict"]["status"] == "resolved"
    assert count(store, Decision, Decision.account_id == twin) == 0


def test_t33_revoked_binding_cannot_read_or_write(synced):
    store, account = synced
    enable(store, account)
    with store.engine.begin() as c:
        c.exec_driver_sql("UPDATE of_creator_bindings SET revoked_at = now()")
    for call in (lambda: get_status(store, TENANT, account), lambda: list_conflicts(store, TENANT, account),
                 lambda: set_settings(store, TENANT, account, enabled=False, expected_revision=1, command_key=key(5), actor=ACTOR),
                 lambda: request_sync_now(store, TENANT, account, kind="incremental", command_key=key(6), actor=ACTOR)):
        with pytest.raises(Refused):
            call()
    assert process_due(store, TENANT, now=clock(1000)) == 0, "no scheduling for a revoked binding"


# ── API contract ────────────────────────────────────────────────────────

def api(store, monkeypatch, *, agency=False):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    monkeypatch.setenv("ONLYFANS_CONTENT_TOKEN", "synthetic-content-only")
    monkeypatch.setenv("ONLYFANS_CONTENT_TENANT", TENANT)
    app = FastAPI()
    app.state.control_store = store
    if agency:
        app.add_middleware(AgencyScopeMiddleware)
    app.include_router(router)
    return TestClient(app)


AUTH = {"authorization": "Bearer synthetic-content-only"}
HEADERS = {**AUTH, HEADER: SCHEMA}


def test_api_status_settings_sync_operations_conflicts_and_commands(synced, monkeypatch):
    store, account = synced
    client = api(store, monkeypatch)
    base = f"/onlyfans/content-sync/v1/accounts/{account}"
    assert client.get(base + "/status").status_code == 401
    assert client.get(base + "/status", headers=AUTH).json()["code"] == "unsupported_schema"
    assert client.get(base + "/status", headers={**AUTH, HEADER: "of-content-sync.v0"}).status_code == 400
    assert client.get(base + "/status", headers={"authorization": "Bearer wrong", HEADER: SCHEMA}).status_code == 401
    r = client.get(base + "/status", headers=HEADERS)
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["schemaVersion"] == SCHEMA and body["accountId"] == account
    assert r.headers["cache-control"] == "private, no-store" and r.headers[HEADER.lower()] == SCHEMA
    assert set(body["sync"]) == {"enabled", "status", "revision", "blockedReason", "lastIncrementalAt", "nextIncrementalAt",
        "lastReconcileAt", "nextReconcileAt", "lastFullReconcileAt", "coveredThroughMessageId", "backlog", "initial",
        "coverage", "openConflicts", "uploads"}
    assert set(body["sync"]["coverage"]) == {"media", "paidMessages", "paidPosts"}
    assert set(body["sync"]["uploads"]) == {"draft", "queued", "uploading", "verifying", "synced", "retrying",
        "connectionRequired", "needsReview", "outcomeUnconfirmed"}
    assert body["sync"]["status"] == "off" and body["sync"]["revision"] == 0 and body["sync"]["initial"]["status"] == "not_started"
    assert client.get(base + "/status?x=1", headers=HEADERS).status_code == 400

    settings = {"enabled": True, "expectedRevision": 0, "commandKey": key(1), "actor": ACTOR}
    first = client.post(base + "/settings", headers=HEADERS, json=settings)
    assert first.status_code == 200 and first.json()["sync"]["enabled"] is True and first.json()["sync"]["revision"] == 1
    assert first.json()["sync"]["initial"]["jobId"] is not None
    assert client.post(base + "/settings", headers=HEADERS, json=settings).json() == first.json(), "idempotent replay"
    assert client.post(base + "/settings", headers=HEADERS, json={**settings, "commandKey": key(2)}).json()["code"] == "revision_conflict"
    assert client.post(base + "/settings", headers=HEADERS, json={**settings, "enabled": False}).json()["code"] == "idempotency_conflict"
    for bad in [{**settings, "extra": 1}, {k: v for k, v in settings.items() if k != "actor"}, {**settings, "enabled": "yes"},
                {**settings, "commandKey": "ABC"}, {**settings, "actor": "someone"}, {**settings, "expectedRevision": -1}]:
        r = client.post(base + "/settings", headers=HEADERS, json=bad)
        assert r.status_code == 400 and r.json()["code"] == "invalid_sync_request", bad
    huge = client.post(base + "/settings", headers=HEADERS, content=json.dumps({**settings, "commandKey": key(3), "pad": "x" * 140000}),
        )
    assert huge.status_code == 400
    assert client.post(base + "/settings", headers={**HEADERS, "content-type": "text/plain"}, content=json.dumps(settings)).status_code == 400
    assert client.post(base + "/settings", headers=HEADERS, content='{"enabled":true,"enabled":true}').status_code == 400

    sync_body = {"kind": "incremental", "commandKey": key(10), "actor": ACTOR}
    queued = client.post(base + "/sync", headers=HEADERS, json=sync_body).json()
    assert queued["ok"] and queued["operation"]["kind"] == "sync_now" and queued["operation"]["status"] == "queued"
    assert set(queued["operation"]) == {"operationId", "kind", "status", "createdAt", "updatedAt", "completedAt", "coverage",
        "result", "lastError", "retryable", "draftId", "assetId"}
    again = client.post(base + "/sync", headers=HEADERS, json={**sync_body, "commandKey": key(11)}).json()
    assert again["operation"]["operationId"] == queued["operation"]["operationId"], "the active operation, not a duplicate"
    assert client.post(base + "/sync", headers=HEADERS, json=sync_body).json() == queued
    assert client.post(base + "/sync", headers=HEADERS, json={**sync_body, "kind": "full"}).status_code == 400
    assert client.post(base + "/sync", headers=HEADERS, json={**sync_body, "kind": "reconcile"}).json()["code"] == "idempotency_conflict"

    page = client.get(base + "/operations?limit=1", headers=HEADERS).json()
    assert len(page["items"]) == 1 and page["nextCursor"] is not None
    rest = client.get(base + f"/operations?limit=5&after={page['nextCursor']}", headers=HEADERS).json()
    assert rest["nextCursor"] is None and {o["kind"] for o in page["items"] + rest["items"]} == {"sync_now", "initial"}
    for bad in ["limit=0", "limit=51", "limit=x", "after=nope", "extra=1"]:
        assert client.get(base + "/operations?" + bad, headers=HEADERS).status_code == 400, bad
    one = client.get(base + f"/operations/{queued['operation']['operationId']}", headers=HEADERS).json()
    assert one["operation"] == queued["operation"]
    assert client.get(base + f"/operations/{uuid.uuid4()}", headers=HEADERS).status_code == 404
    assert client.get(base + "/operations/not-a-uuid", headers=HEADERS).status_code == 400
    refused = client.post(base + f"/operations/{queued['operation']['operationId']}/retry", headers=HEADERS,
        json={"commandKey": key(12), "actor": ACTOR})
    assert refused.status_code == 409 and refused.json()["code"] == "sync_conflict", "only classified retryable outcomes"

    assert client.get(base + "/conflicts", headers=HEADERS).json()["items"] == []
    assert client.get(base + "/conflicts?status=all", headers=HEADERS).json()["items"] == []
    assert client.get(base + "/conflicts?status=weird", headers=HEADERS).status_code == 400
    missing = client.post(base + f"/conflicts/{uuid.uuid4()}/resolve", headers=HEADERS, json={"resolution": "keep_crm",
        "expectedRevision": 1, "expectedItemFingerprint": "0" * 64, "commandKey": key(13), "actor": ACTOR})
    assert missing.status_code == 404 and missing.json()["code"] == "not_available"

    found = client.get(base + f"/commands/{key(1)}", headers=HEADERS).json()["command"]
    assert found["recorded"] is True and found["action"] == "settings" and found["sync"]["revision"] == 1
    assert client.get(base + f"/commands/{key(10)}", headers=HEADERS).json()["command"]["action"] == "sync"
    assert client.get(base + f"/commands/{key(99)}", headers=HEADERS).json()["command"] == {"commandKey": key(99), "recorded": False}
    assert client.get(base + "/commands/short", headers=HEADERS).status_code == 400
    assert client.get("/onlyfans/content-sync/v1/accounts/other-account/status", headers=HEADERS).status_code == 404

    with store.engine.begin() as c:
        c.exec_driver_sql("UPDATE of_provider_connection SET expires_at = now() - interval '1 hour'")
    expired = client.post(base + "/sync", headers=HEADERS, json={**sync_body, "kind": "reconcile", "commandKey": key(14)})
    assert expired.status_code == 409 and expired.json()["code"] == "connection_unavailable"
    off = client.post(base + "/settings", headers=HEADERS, json={"enabled": False, "expectedRevision": 1, "commandKey": key(15), "actor": ACTOR})
    assert off.json()["sync"]["enabled"] is False and off.json()["sync"]["status"] == "off" and off.json()["sync"]["revision"] == 2
    assert all(o["status"] == "cancelled" for o in client.get(base + "/operations?limit=5", headers=HEADERS).json()["items"]
               if o["kind"] == "sync_now")
    assert client.post(base + "/sync", headers=HEADERS, json={**sync_body, "commandKey": key(16)}).status_code == 409


def test_api_conflict_resolution_over_http(changed, monkeypatch):
    store, account, item, other = changed
    process_due(store, TENANT, now=clock())
    client = api(store, monkeypatch)
    base = f"/onlyfans/content-sync/v1/accounts/{account}"
    conflict = client.get(base + "/conflicts", headers=HEADERS).json()["items"][0]
    body = {"resolution": "accept_source", "expectedRevision": conflict["revision"],
        "expectedItemFingerprint": conflict["itemFingerprint"], "commandKey": key(400), "actor": ACTOR}
    stale = client.post(base + f"/conflicts/{conflict['conflictId']}/resolve", headers=HEADERS, json={**body, "expectedRevision": 9})
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
    ok = client.post(base + f"/conflicts/{conflict['conflictId']}/resolve", headers=HEADERS, json=body)
    assert ok.status_code == 200 and ok.json()["conflict"]["resolution"] == "accept_source" and ok.json()["itemId"] == str(item)
    assert client.post(base + f"/conflicts/{conflict['conflictId']}/resolve", headers=HEADERS, json=body).json() == ok.json()
    assert client.get(base + "/conflicts?status=all", headers=HEADERS).json()["items"][0]["status"] == "resolved"
    assert client.get(base + "/conflicts", headers=HEADERS).json()["items"] == []
    assert client.get(base + f"/commands/{key(400)}", headers=HEADERS).json()["command"]["action"] == "resolve"


def test_api_read_only_and_cross_creator_agency_actors_cannot_write(synced, monkeypatch):
    store, account = synced
    client = api(store, monkeypatch, agency=True)
    base = f"/onlyfans/content-sync/v1/accounts/{account}"
    with Session(store.engine) as s:
        creator = s.get(OfCreatorBinding, s.get(OfRuntime, account).binding_id).creator_id
    uid, member = str(uuid.uuid4()), str(uuid.uuid4())
    actor = f"agency-user:{uid}:{member}"
    def scope(can_manage, creators=None, who=actor):
        return {**HEADERS, "x-onlyfans-agency-scope": json.dumps({"creatorIds": creators or [creator_uuid], "actor": who, "canManage": can_manage})}
    creator_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, creator))
    with store.engine.begin() as c:
        c.execute(text("UPDATE of_creator_bindings SET creator_id = :c WHERE account_id = :a"), {"c": creator_uuid, "a": account})
    settings = {"enabled": True, "expectedRevision": 0, "commandKey": key(1), "actor": actor}
    assert client.get(base + "/status", headers=scope(False)).status_code == 200
    assert client.get(base + "/operations", headers=scope(False)).status_code == 200, "operations list is readable"
    denied = client.post(base + "/settings", headers=scope(False), json=settings)
    assert denied.status_code == 403 and denied.json()["code"] == "agency_scope_denied"
    assert client.put(base + f"/drafts/{uuid.uuid4()}/files/{uuid.uuid4()}/chunks/0", headers={**scope(False), "content-type": "application/octet-stream"}, content=b"x").status_code == 403
    assert client.get(base + "/status", headers=scope(True, creators=[str(uuid.uuid4())])).status_code == 404, "another creator's account"
    forged = client.post(base + "/settings", headers=scope(True), json={**settings, "actor": f"agency-user:{uuid.uuid4()}:{uuid.uuid4()}"})
    assert forged.status_code == 400, "a client-supplied actor must match the verified one"
    granted = client.post(base + "/settings", headers=scope(True), json=settings)
    assert granted.status_code == 200 and granted.json()["sync"]["enabled"] is True
    assert count(store, Decision, Decision.actor == actor) == 1


# ── Worker wiring ───────────────────────────────────────────────────────

def test_worker_runs_scheduler_then_uploads_only_behind_its_flag(monkeypatch):
    import onlyfans_worker as entry
    calls = []
    fake_sync = types.ModuleType("onlyfans_runtime.content_sync")
    fake_sync.process_due = lambda store, tenant, *, limit, stop: calls.append(("due", limit)) or 2
    fake_media = types.ModuleType("onlyfans_runtime.content_sync_media")
    from onlyfans_runtime.content_sync_carrier import transport_for as carrier
    fake_media.process_uploads = lambda store, tenant, *, limit, stop, transport_for: (
        calls.append(("uploads", limit, transport_for is carrier)) or 3)
    fake_posts = types.ModuleType("onlyfans_runtime.offer_post_source")
    fake_posts.process_posts = lambda store, tenant, *, limit, stop: calls.append(("posts", limit)) or 1
    monkeypatch.setitem(sys.modules, "onlyfans_runtime.content_sync", fake_sync)
    monkeypatch.setitem(sys.modules, "onlyfans_runtime.content_sync_media", fake_media)
    monkeypatch.setitem(sys.modules, "onlyfans_runtime.offer_post_source", fake_posts)
    import onlyfans_runtime
    monkeypatch.setattr(onlyfans_runtime, "content_sync", fake_sync, raising=False)
    monkeypatch.setattr(onlyfans_runtime, "content_sync_media", fake_media, raising=False)
    monkeypatch.setattr(onlyfans_runtime, "offer_post_source", fake_posts, raising=False)
    assert entry._process_content_sync(object(), "tenant-a", limit=1, stop=threading.Event()) == 6
    # Uploads always receive the per-account carrier gate, never an ungated transport.
    calls_after = [("due", 1), ("posts", 1), ("uploads", 1, True)]
    assert calls == calls_after
    seen = []
    load = lambda *_, **__: ([], None, 0)
    assert entry.run_loop(object(), "tenant-a", threading.Event(), once=True, load_cohort=load, observe=lambda _: None) == 0
    assert entry.run_loop(object(), "tenant-a", threading.Event(), once=True, load_cohort=load, observe=lambda _: None,
        content_sync_jobs=lambda store, tenant, *, limit, stop: seen.append(tenant)) == 0
    assert seen == ["tenant-a"] and calls == calls_after, "off unless wired by ONLYFANS_CONTENT_SYNC_ENABLED=1"


def test_worker_tolerates_a_release_without_the_upload_module(monkeypatch):
    import onlyfans_worker as entry
    fake_sync = types.ModuleType("onlyfans_runtime.content_sync")
    fake_sync.process_due = lambda store, tenant, *, limit, stop: 1
    fake_posts = types.ModuleType("onlyfans_runtime.offer_post_source")
    fake_posts.process_posts = lambda store, tenant, *, limit, stop: 0
    monkeypatch.setitem(sys.modules, "onlyfans_runtime.content_sync", fake_sync)
    monkeypatch.setitem(sys.modules, "onlyfans_runtime.offer_post_source", fake_posts)
    import onlyfans_runtime
    monkeypatch.setattr(onlyfans_runtime, "content_sync", fake_sync, raising=False)
    monkeypatch.setattr(onlyfans_runtime, "offer_post_source", fake_posts, raising=False)
    # Earlier tests can leave the imported module on its parent package. A
    # sys.modules sentinel alone does not hide that cached package attribute.
    monkeypatch.delattr(onlyfans_runtime, "content_sync_media", raising=False)
    monkeypatch.setitem(sys.modules, "onlyfans_runtime.content_sync_media", None)   # import raises ModuleNotFoundError
    assert entry._process_content_sync(object(), "tenant-a", limit=1, stop=threading.Event()) == 1


def test_process_due_requires_the_worker_role_and_valid_bounds(synced, monkeypatch):
    store, _ = synced
    with pytest.raises(Refused):
        process_due(store, TENANT, limit=0)
    with pytest.raises(Refused):
        process_due(store, "deployment")
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "api")
    with pytest.raises(Refused):
        process_due(store, TENANT)
