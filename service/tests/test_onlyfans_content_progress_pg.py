"""Final integration regressions for durable content work and source order.

All provider readers are synthetic. No session, upload, send or purchase is
performed. PostgreSQL proves receipts, restart continuation and lease fencing.
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_offer_import_pg import ready, history, key, raw, FakeHistory, factory, refresh, observation, draft
from test_onlyfans_content_sync_pg import synced, enable, clock, imported
from test_onlyfans_connections_pg import TENANT, ACTOR
from db.onlyfans_offer_import_models import OfContentSyncAccount, OfOfferImportCandidate, OfOfferImportJob, OfContentSyncConflict
from db.onlyfans_content_sync_progress_models import OfContentSyncSource as Source, OfContentSyncOccurrence as Occurrence
from onlyfans_runtime import content_sync
from onlyfans_runtime.offer_import import process_inventory_jobs, request_job, create_draft
from onlyfans_runtime.offer_import_worker import process_refresh_jobs
from onlyfans_runtime.provider_read_budget import acquire, current, release, ReadDeferred
from onlyfans_runtime.store import Refused, utcnow


def source(store, account, mid):
    with Session(store.engine) as s:
        binding = s.get(OfContentSyncAccount, account).binding_id
        row = s.get(Source, (account, binding, "message", mid))
        if row:
            s.expunge(row)
        return row


def candidate_count(store, account):
    with Session(store.engine) as s:
        job = s.get(OfContentSyncAccount, account).initial_job_id
        return s.scalar(select(func.sum(OfOfferImportCandidate.source_count)).where(
            OfOfferImportCandidate.account_id == account, OfOfferImportCandidate.job_id == job))


def test_late_old_message_is_folded_once_after_progress_and_restart(synced):
    store, account = synced
    enable(store, account)
    while process_inventory_jobs(store, TENANT):
        pass
    history(store, account, [(501, 9000, [15], 1007, "out")])
    content_sync.process_due(store, TENANT, now=clock())
    before = candidate_count(store, account)
    history(store, account, [(501, 42, [15], 1007, "out")])
    # A new worker call sees the committed receipt state, not a provider-ID tail.
    with Session(store.engine) as s, s.begin():
        s.get(OfContentSyncAccount, account).incremental_due_at = clock(1)
    content_sync.process_due(store, TENANT, now=clock(2))
    assert source(store, account, 42).folded_at is not None
    assert source(store, account, 42).refresh_requested_at is not None
    assert candidate_count(store, account) == before + 1
    with Session(store.engine) as s, s.begin():
        s.get(OfContentSyncAccount, account).incremental_due_at = clock(3)
    content_sync.process_due(store, TENANT, now=clock(4))
    assert candidate_count(store, account) == before + 1


def test_125_sources_survive_an_occupied_account_and_drain_without_duplicates(synced):
    store, account = synced
    enable(store, account)
    while process_inventory_jobs(store, TENANT):
        pass
    request_job(store, TENANT, account, mode="refresh", sources=[{"fanId": "501", "messageId": "1001"}],
        command_key=key(8000), actor=ACTOR)
    wanted = list(range(2000, 2125))
    history(store, account, [(501, mid, [15], 1007, "out") for mid in wanted])
    content_sync.process_due(store, TENANT, now=clock())
    assert all(source(store, account, mid).refresh_requested_at is not None for mid in wanted)
    assert all(source(store, account, mid).refresh_job_id is None for mid in wanted)
    rows = [raw(mid, [15], 1007, previews=[], locked=False) for mid in reversed(wanted)]
    rows += [raw(1001, [11, 12], 1999, previews=[], locked=False)]
    class Reader(FakeHistory):
        def messages(self, *, fan_id, before_id, limit):
            remaining = [r for r in rows if before_id is None or r["id"] < before_id]
            picked = remaining[:limit]
            self.pages[(fan_id, before_id)] = (picked, len(remaining) > limit)
            return super().messages(fan_id=fan_id, before_id=before_id, limit=limit)
    reader = Reader({})
    for _ in range(20):
        processed = process_refresh_jobs(store, TENANT, transport_factory=factory(reader))
        with store.engine.begin() as c:
            c.execute(text("UPDATE of_offer_import_job SET next_attempt_at=NULL"))
        if not processed:
            with Session(store.engine) as s:
                active = s.scalar(select(func.count()).select_from(OfOfferImportJob).where(
                    OfOfferImportJob.account_id == account, OfOfferImportJob.mode == "refresh",
                    OfOfferImportJob.status.in_(("pending", "running"))))
            if not active:
                break
    assert all(source(store, account, mid).refresh_completed_at is not None for mid in wanted)
    assert all(source(store, account, mid).current_observation_id is not None for mid in wanted)
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(Occurrence).where(
            Occurrence.account_id == account, Occurrence.source_id.in_(wanted))) == 125


def test_a_b_a_reuses_snapshots_but_preserves_three_occurrences_and_new_hold(synced):
    store, account = synced
    enable(store, account)
    item = imported(store, account, 501, 1001, [11, 12], 1999, (), n=8100)
    refresh(store, account, [(501, 1001)], {(501, None): ([raw(1001, [11, 12], 2500,
        previews=[], locked=False)], False)}, n=8110)
    with Session(store.engine) as s, s.begin():
        binding = s.get(OfContentSyncAccount, account).binding_id
        content_sync.detect_conflicts(s, account, binding, utcnow())
        conflict = s.scalar(select(OfContentSyncConflict).where(OfContentSyncConflict.item_id == item,
            OfContentSyncConflict.status == "open"))
        view = content_sync.conflict_view(s, conflict)
    content_sync.resolve_conflict(store, TENANT, account, view["conflictId"], resolution="accept_source",
        expected_revision=view["revision"], expected_item_fingerprint=view["itemFingerprint"], command_key=key(8120), actor=ACTOR)
    refresh(store, account, [(501, 1001)], {(501, None): ([raw(1001, [11, 12], 1999,
        previews=[], locked=False)], False)}, n=8130)
    with Session(store.engine) as s, s.begin():
        binding = s.get(OfContentSyncAccount, account).binding_id
        content_sync.detect_conflicts(s, account, binding, utcnow())
        events = s.scalars(select(Occurrence).where(Occurrence.account_id == account,
            Occurrence.source_id == 1001).order_by(Occurrence.occurrence)).all()
        assert [r.occurrence for r in events] == [1, 2, 3]
        assert events[0].observation_id == events[2].observation_id
        assert content_sync.open_conflict(s, account, item)


def test_shared_budget_fences_two_workers_expiry_session_and_rate_limit(synced):
    store, account = synced
    from db.onlyfans_connection_models import OfProviderConnection
    with Session(store.engine) as s:
        revision = s.get(OfProviderConnection, account).revision
    now = utcnow()
    a = acquire(store, TENANT, account, connection_revision=revision, kind="history", now=now)
    with pytest.raises(ReadDeferred, match="lease occupied"):
        acquire(store, TENANT, account, connection_revision=revision, kind="posts", now=now)
    b = acquire(store, TENANT, account, connection_revision=revision, kind="recent", now=now + timedelta(seconds=46))
    with pytest.raises(Refused, match="stale"):
        current(store, TENANT, a, now=now + timedelta(seconds=46))
    assert release(store, TENANT, a, now=now + timedelta(seconds=46)) is False
    release(store, TENANT, b, retry_after=30, now=now + timedelta(seconds=46))
    with pytest.raises(ReadDeferred, match="backoff"):
        acquire(store, TENANT, account, connection_revision=revision, kind="history", now=now + timedelta(seconds=47))
    c = acquire(store, TENANT, account, connection_revision=revision, kind="history", now=now + timedelta(seconds=77))
    with Session(store.engine) as s, s.begin():
        s.get(OfProviderConnection, account).revision += 1
    with pytest.raises(Refused, match="connection changed"):
        current(store, TENANT, c, now=now + timedelta(seconds=78))


def test_repeated_priority_sources_do_not_starve_older_ordinary_queue(synced):
    store, account = synced
    from db.onlyfans_offer_import_models import OfOfferImportCursor
    from onlyfans_runtime.offer_import import _terminal
    enable(store, account)
    while process_inventory_jobs(store, TENANT):
        pass
    wanted = [(501, mid) for mid in range(2000, 2125)]
    history(store, account, [(fan, mid, [15], 1007, "out") for fan, mid in wanted])
    claim = content_sync.claim_due(store, TENANT, now=clock())
    assert content_sync._queue_refresh(store, TENANT, claim, ACTOR, wanted) == "queued"
    with Session(store.engine) as s, s.begin():
        job = s.scalar(select(OfOfferImportJob).where(OfOfferImportJob.mode == "refresh"))
        assert job.targets_total == 50
        first_job = job.job_id
        # A synthetic completed batch lets this test isolate scheduling order;
        # provider ownership/observation proof is exercised in the drain test.
        _terminal(job, "complete")
        for row in s.scalars(select(Source).where(Source.refresh_job_id == first_job)):
            row.refresh_completed_at, row.refresh_job_id = utcnow(), None
    assert content_sync._queue_refresh(store, TENANT, claim, ACTOR, wanted[:50]) == "queued"
    with Session(store.engine) as s:
        job = s.scalar(select(OfOfferImportJob).where(OfOfferImportJob.mode == "refresh", OfOfferImportJob.status == "pending"))
        cursor = s.scalar(select(OfOfferImportCursor).where(OfOfferImportCursor.job_id == job.job_id))
        assert sorted(json.loads(cursor.targets_json)) == list(range(2050, 2100))
        assert s.scalar(select(func.count()).select_from(Source).where(Source.refresh_completed_at.is_(None),
            Source.refresh_requested_at.is_not(None))) == 125


def test_crash_between_fold_commit_and_refresh_dispatch_preserves_every_request(synced, monkeypatch):
    store, account = synced
    enable(store, account)
    while process_inventory_jobs(store, TENANT):
        pass
    wanted = list(range(2000, 2125))
    history(store, account, [(501, mid, [15], 1007, "out") for mid in wanted])
    original = content_sync._queue_refresh
    monkeypatch.setattr(content_sync, "_queue_refresh", lambda *a, **kw: (_ for _ in ()).throw(SystemExit("synthetic crash")))
    start = clock()
    with pytest.raises(SystemExit):
        content_sync.process_due(store, TENANT, now=start)
    before = candidate_count(store, account)
    assert all(source(store, account, mid).refresh_requested_at is not None for mid in wanted)
    assert all(source(store, account, mid).refresh_job_id is None for mid in wanted)
    monkeypatch.setattr(content_sync, "_queue_refresh", original)
    assert content_sync.process_due(store, TENANT, now=start + timedelta(seconds=61)) == 1
    assert candidate_count(store, account) == before
    with Session(store.engine) as s:
        job = s.scalar(select(OfOfferImportJob).where(OfOfferImportJob.mode == "refresh"))
        assert job.targets_total == 50
        assert s.scalar(select(func.count()).select_from(Source).where(Source.source_id.in_(wanted),
            Source.refresh_requested_at.is_not(None), Source.refresh_completed_at.is_(None))) == 125


def test_crash_after_folding_an_edit_reopens_the_completed_refresh_receipt(synced, monkeypatch):
    from db.models import Message
    store, account = synced
    enable(store, account)
    while process_inventory_jobs(store, TENANT):
        pass
    refresh(store, account, [(501, 1001)], {(501, None): ([raw(1001, [11, 12], 1999,
        previews=[], locked=False)], False)}, n=8300)
    with Session(store.engine) as s, s.begin():
        binding = s.get(OfContentSyncAccount, account).binding_id
        receipt = s.get(Source, (account, binding, "message", 1001))
        receipt.refresh_requested_at = utcnow() - timedelta(minutes=1)
        receipt.refresh_completed_at = utcnow()
        s.get(Message, (account, 501, 1001)).price_cents = 2500
    monkeypatch.setattr(content_sync, "_queue_refresh", lambda *a, **kw: (_ for _ in ()).throw(SystemExit("synthetic crash")))
    with pytest.raises(SystemExit):
        content_sync.process_due(store, TENANT, now=clock())
    receipt = source(store, account, 1001)
    assert receipt.folded_message_price_cents == 2500
    assert receipt.refresh_requested_at is not None and receipt.refresh_completed_at is None
    assert receipt.refresh_job_id is None, "edited terms remain durably queued before dispatch"


def test_dispatch_restart_preserves_terminal_failure_and_drains_the_next_batch(synced):
    from onlyfans_runtime.offer_import import _terminal
    store, account = synced
    enable(store, account)
    while process_inventory_jobs(store, TENANT):
        pass
    wanted = [(501, mid) for mid in range(2000, 2125)]
    history(store, account, [(fan, mid, [15], 1007, "out") for fan, mid in wanted])
    claim = content_sync.claim_due(store, TENANT, now=clock())
    assert content_sync._queue_refresh(store, TENANT, claim, ACTOR, wanted) == "queued"
    with Session(store.engine) as s, s.begin():
        job = s.scalar(select(OfOfferImportJob).where(OfOfferImportJob.mode == "refresh"))
        _terminal(job, "failed", "authentication_rejected")
    assert content_sync.dispatch_refresh_queue(store, TENANT, account, ACTOR) == "queued"
    with Session(store.engine) as s:
        completed = s.scalars(select(Source).where(Source.refresh_completed_at.is_not(None))).all()
        assert len(completed) == 50 and all(r.refresh_error == "authentication_rejected" for r in completed)
        assert s.scalar(select(OfOfferImportJob.targets_total).where(OfOfferImportJob.mode == "refresh",
            OfOfferImportJob.status == "pending")) == 50
