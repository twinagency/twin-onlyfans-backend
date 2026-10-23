"""Preserved concurrent Claude findings scenarios from e7eae43485a053bb74b9efef3f13b577cee41f48.

The native adapter boundary replaces the upstream global pace-off fixture.
A/B/A preserves two immutable snapshots plus three ordered occurrences.
Shared rate-limit backoff/reserved intake replaces the alternate fixed-interval
read gate; no second provider authority or historical schema rewrite is used.

Owned PostgreSQL, a controlled clock and fake providers only. No credential,
network, send, post or model call (the real connector entry points fail the
test if reached, via the `synced` fixture).
"""
import json
import threading
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from test_onlyfans_content_sync_pg import (synced, enable, clock, pick, ops, count, sales, observe, imported, enable_item,
    terms, as_worker, candidate_total)
from test_onlyfans_content_sync_pg import Account, Conflict, history, raw, FakeHistory, factory, process_due
from test_onlyfans_connections_pg import ACTOR, TENANT
from test_onlyfans_offer_import_pg import key, ready  # noqa: F401 (fixture)
from test_onlyfans_foundation_pg import database  # noqa: F401 (fixture)
from test_onlyfans_offer_post_source_pg import FakePosts, post, raw_page
from db.onlyfans_offer_import_models import OfOfferImportCursor, OfOfferImportJob, OfOfferObservation
from onlyfans_runtime import content_sync, offer_import, offer_post_source as posts
from onlyfans_runtime.content_sync import list_conflicts, resolve_conflict
from onlyfans_runtime.offer_import import get_candidate, list_candidates, create_draft
from onlyfans_runtime.offer_import_worker import process_refresh_jobs
from onlyfans_runtime.offer_terms import media_key
from onlyfans_runtime.store import Refused, utcnow


def drain_refresh(store, reader):
    while process_refresh_jobs(store, TENANT, transport_factory=factory(reader)):
        with store.engine.begin() as c:
            c.exec_driver_sql("UPDATE of_offer_import_job SET next_attempt_at = NULL")


def refresh_jobs(store, account):
    with Session(store.engine) as s:
        rows = s.scalars(select(OfOfferImportJob).where(OfOfferImportJob.account_id == account,
            OfOfferImportJob.mode == "refresh").order_by(OfOfferImportJob.requested_at, OfOfferImportJob.job_id)).all()
        return [(j.job_id, sorted(c.fan_id for c in s.scalars(select(OfOfferImportCursor).where(
            OfOfferImportCursor.account_id == account, OfOfferImportCursor.job_id == j.job_id)))) for j in rows]


# ── 1: refresh requests are never lost ──────────────────────────────────

def test_f1_sources_beyond_fifty_are_requested_by_a_later_refresh(synced, monkeypatch):
    as_worker(monkeypatch)
    store, account = synced
    history(store, account, sales([(650 + n, 9500 + n, [11], 500) for n in range(60)]))
    enable(store, account)
    with store.engine.begin() as c:
        c.exec_driver_sql("UPDATE of_content_sync_account SET covered_through_message_id = 1004")
    assert process_due(store, TENANT, now=clock()) == 1
    pages = {(650 + n, None): ([raw(9500 + n, [11], 500, previews=[], locked=False, fan=650 + n)], False) for n in range(60)}
    drain_refresh(store, FakeHistory(pages))
    assert count(store, OfOfferObservation) == 60, "durable dispatch immediately drains both bounded batches"
    assert process_due(store, TENANT, now=clock(900)) == 1
    jobs = refresh_jobs(store, account)
    assert len(jobs) == 2 and [len(fans) for _, fans in jobs] == [50, 10]
    assert sorted(fan for _, fans in jobs for fan in fans) == list(range(650, 710)), "oldest-first batches preserve all sources exactly once"
    drain_refresh(store, FakeHistory(pages))
    assert count(store, OfOfferObservation) == 60


def test_f1_sources_are_kept_when_another_refresh_job_already_occupies_the_account(synced, monkeypatch):
    as_worker(monkeypatch)
    store, account = synced
    enable(store, account)
    history(store, account, sales([(640, 9001, [12, 11], 1999)]))
    offer_import.request_job(store, TENANT, account, mode="refresh", command_key=key(70), actor=ACTOR,
        sources=[{"fanId": "501", "messageId": "1001"}])
    assert process_due(store, TENANT, now=clock()) == 1
    assert json.loads(ops(store, account, "incremental")[0].coverage_json)["refresh"] == "active"
    drain_refresh(store, FakeHistory({
        (501, None): ([raw(1001, [11, 12], 1999, previews=[], locked=False, fan=501)], False),
        (640, None): ([raw(9001, [12, 11], 1999, previews=[], locked=False, fan=640)], False)}))
    assert process_due(store, TENANT, now=clock(900)) == 1
    assert [fans for _, fans in refresh_jobs(store, account)] == [[501], [640]], "the deferred source is requested once the account is free"


# ── 2: late-imported older messages ─────────────────────────────────────

def test_f2_a_late_imported_older_message_is_folded_exactly_once(synced):
    store, account = synced
    enable(store, account)
    with store.engine.begin() as c:
        c.exec_driver_sql("UPDATE of_content_sync_account SET covered_through_message_id = 1004")
    history(store, account, sales([(610, 7000, [11, 12], 900)]))
    assert process_due(store, TENANT, now=clock()) == 1
    assert pick(store, Account, account).covered_through_message_id == 7000
    base = candidate_total(store, account)
    history(store, account, sales([(611, 6500, [13, 14], 1200)]))   # imported late, id below the cursor
    assert process_due(store, TENANT, now=clock(900)) == 1
    assert candidate_total(store, account) == base + 1, "the late older message becomes a candidate"
    assert process_due(store, TENANT, now=clock(1800)) == 1
    assert candidate_total(store, account) == base + 1, "and is never counted twice"
    assert pick(store, Account, account).covered_through_message_id == 7000


# ── 3: reconciliation does not starve other priority items ──────────────

def test_f3_reconcile_rotates_through_priority_items(synced, monkeypatch):
    store, account = synced
    monkeypatch.setattr(content_sync, "ROTATION_QUOTA", 1)
    monkeypatch.setattr(content_sync, "PRIORITY_QUOTA", 1)
    from db.models import CatalogItem
    with Session(store.engine) as s, s.begin():
        ids = []
        for n in range(6):
            item = CatalogItem(account_id=account, kind="image", media_ids=json.dumps([100 + n]), price_cents=500 + n,
                enabled=n % 2 == 1, pricing_mode="fixed", description_for_ai="synthetic")
            s.add(item); s.flush(); ids.append(item.id)
    enabled = {ids[1], ids[3], ids[5]}
    enable(store, account)
    checked = []
    real = content_sync._media_states
    monkeypatch.setattr(content_sync, "_media_states", lambda s, a, rt, c, media: checked.append(tuple(media)) or real(s, a, rt, c, media))
    for day in range(3):
        t = clock(86400 * (day + 1))
        with store.engine.begin() as c:
            c.exec_driver_sql("UPDATE of_provider_connection SET expires_at = now() + interval '90 days'")
        assert process_due(store, TENANT, now=t, limit=2) >= 1
        with store.engine.begin() as c:
            c.execute(text("UPDATE of_content_sync_account SET reconcile_due_at=:d"), {"d": t})
    enabled_media = {(100 + n,) for n in (1, 3, 5)}
    assert enabled_media <= set(checked), "every enabled item is rechecked within three daily passes"


# ── 4: A -> B -> A ──────────────────────────────────────────────────────

def test_f4_a_source_changing_back_to_an_earlier_state_is_still_a_change(synced):
    store, account = synced
    item = imported(store, account, 501, 1001, [11, 12], 1999, [11], 100)
    enable_item(store, account, item, 102)
    enable(store, account)
    observe(store, account, 501, 1001, [12, 11], 2500, [12], n=130)          # A -> B
    assert process_due(store, TENANT, now=clock()) == 1
    first = list_conflicts(store, TENANT, account)["items"][0]
    resolve_conflict(store, TENANT, account, first["conflictId"], resolution="accept_source",
        expected_revision=first["revision"], expected_item_fingerprint=first["itemFingerprint"], command_key=key(131), actor=ACTOR)
    assert terms(store, account, item)["price_cents"] == 2500
    observe(store, account, 501, 1001, [11, 12], 1999, [11], n=140)          # B -> A (same terms as the first sighting)
    with pytest.raises(Refused, match="open_conflict"):
        terms(store, account, item)
    assert process_due(store, TENANT, now=clock(900)) == 1
    open_now = list_conflicts(store, TENANT, account)["items"]
    assert len(open_now) == 1 and open_now[0]["source"]["priceCents"] == 1999
    assert count(store, OfOfferObservation, OfOfferObservation.source_id == 1001) == 2, "immutable snapshot A is reused"
    from db.onlyfans_content_sync_progress_models import OfContentSyncOccurrence
    assert count(store, OfContentSyncOccurrence, OfContentSyncOccurrence.source_id == 1001) == 3, "every ordered occurrence is retained"


# ── 5: paid posts reach the catalogue through a candidate ───────────────

def test_f5_a_paid_post_is_a_reviewable_candidate_and_becomes_an_exact_draft(synced, monkeypatch):
    as_worker(monkeypatch)
    store, account = synced
    enable(store, account)
    fake = FakePosts({None: raw_page([post(901, [(12, "photo"), (13, "video")], "19.99", preview=[12])])})
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: fake, now=utcnow()) == 1
    job = pick(store, Account, account).initial_job_id
    mkey = media_key([12, 13])
    listed = list_candidates(store, TENANT, account, job)["items"]
    assert [c["candidateId"] for c in listed if c["candidateId"] == mkey] == [mkey]
    detail = get_candidate(store, TENANT, account, job, mkey)
    assert detail["status"] == "ready_for_review" and detail["sourceCount"] == 1
    assert [(o["sourceKind"], o["priceCents"], o["ready"]) for o in detail["observations"]] == [("post", 1999, True)]
    obs = detail["observations"][0]
    result = create_draft(store, TENANT, account, actor=ACTOR, draft={"commandKey": key(80), "observationId": obs["observationId"],
        "expectedFingerprint": obs["fingerprint"], "label": "Post set", "description": "One photo and one video.",
        "reuseConfirmed": True, "descriptionApproved": True})
    assert result["priceCents"] == 1999
    from db.models import CatalogItem
    with Session(store.engine) as s:
        item = s.get(CatalogItem, int(result["itemId"]))
        assert item.price_cents == 1999 and item.pricing_mode == "fixed" and item.enabled is False
        assert json.loads(item.media_ids) == [12, 13] and json.loads(item.preview_media_ids) == [12]
    # Reading the same post again never adds a second source to the candidate.
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: fake, now=utcnow() + timedelta(seconds=400)) == 1
    assert get_candidate(store, TENANT, account, job, mkey)["sourceCount"] == 1


# ── 6: paid-post history continuation and edits ─────────────────────────

def test_f6_the_walk_continues_into_older_pages_across_passes(synced, monkeypatch):
    as_worker(monkeypatch)
    monkeypatch.setattr(posts, "PAGES_PER_PASS", 1)
    store, account = synced
    enable(store, account)
    older = "1760800000.000000"
    fake = FakePosts({None: raw_page([post(901, [(11, "photo")], "3.00", preview=[])], more=True, tail="1760900000.000000"),
                      "1760900000.000000": raw_page([post(902, [(12, "photo")], "4.00", preview=[])], more=True, tail=older),
                      older: raw_page([post(903, [(13, "video")], "5.00", preview=[])])})
    now = utcnow()
    for n in range(3):
        before = len(fake.calls)
        assert posts.process_posts(store, TENANT, transport_factory=lambda *a: fake, now=now + timedelta(seconds=301 * n)) == 1
        assert len(fake.calls) - before <= 2, "one head plus one continuation remains bounded"
    with Session(store.engine) as s:
        seen = sorted(s.scalars(select(OfOfferObservation.source_id).where(OfOfferObservation.source_kind == "post")))
        assert seen == [901, 902, 903], "a bounded pass resumes from its durable cursor instead of rereading the newest page"
        row = s.get(Account, account)
        from db.onlyfans_content_sync_progress_models import OfContentPostProgress
        progress = s.get(OfContentPostProgress, account)
        assert progress.history_checked_at is not None, "the initial historical walk reached its end"
        if progress.history_marker is not None:
            assert posts.coverage(row, True, now + timedelta(seconds=602), progress)["state"] == "partial", "a later revisit stays honest"


def test_f6_an_edited_known_post_is_a_new_observation(synced, monkeypatch):
    as_worker(monkeypatch)
    store, account = synced
    enable(store, account)
    now = utcnow()
    first = FakePosts({None: raw_page([post(901, [(11, "photo")], "3.00", preview=[])])})
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: first, now=now) == 1
    edited = FakePosts({None: raw_page([post(901, [(11, "photo")], "9.00", preview=[])])})
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: edited, now=now + timedelta(seconds=301)) == 1
    with Session(store.engine) as s:
        prices = sorted(s.scalars(select(OfOfferObservation.price_cents).where(OfOfferObservation.source_id == 901)))
    assert prices == [300, 900], "the edit of a known post is recorded, never skipped as already known"


# ── 7: coordinated provider-read leases and throttling ──────────────────

def _native_reader(account):
    from onlyfans_runtime.provider_connector import NativeProviderConnector
    from test_onlyfans_provider_adapters import credential
    return NativeProviderConnector(credential(account_id=account))


def test_f7_two_readers_never_read_one_account_at_the_same_time(synced, monkeypatch):
    """The upstream concurrent refresh/posts invariant uses real native adapters."""
    from concurrent.futures import ThreadPoolExecutor
    from onlyfans_runtime.offer_import import process_inventory_jobs
    store, account = synced
    enable(store, account)
    while process_inventory_jobs(store, TENANT):
        pass
    history(store, account, sales([(640, 9001, [12, 11], 1999)]))
    assert process_due(store, TENANT, now=clock()) == 1
    reader, entered, release = _native_reader(account), threading.Event(), threading.Event()
    calls, active, peak, lock = [], [0], [0], threading.Lock()
    parsed = FakeHistory({(640, None): ([raw(9001, [12, 11], 1999, previews=[], locked=False, fan=640)], False)})
    def messages(**kw):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        entered.set()
        try:
            assert release.wait(5), "concurrent test did not release the bounded read"
            return parsed.messages(**kw)
        finally:
            with lock:
                active[0] -= 1
    def post_page(**kw):
        with lock:
            peak[0] = max(peak[0], active[0] + 1)
        calls.append(kw)
        return raw_page([post(901, [(11, "photo")], "3.00", preview=[])])
    monkeypatch.setattr(reader, "messages", messages)
    monkeypatch.setattr(reader, "posts_page", post_page)
    # Detail reads are separately covered; this scenario isolates overlapping
    # native refresh/head ownership, without opening any HTTP connection.
    monkeypatch.setattr(reader, "post_by_id", None)
    now = utcnow()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(process_refresh_jobs, store, TENANT, transport_factory=factory(reader))
        assert entered.wait(5)
        try:
            b = pool.submit(posts.process_posts, store, TENANT, transport_factory=lambda *a: reader, now=now)
            assert b.result(timeout=5) == 1
            assert calls == [], "the second native lane must defer before provider I/O"
        finally:
            release.set()
        assert a.result(timeout=5) == 1
    assert peak[0] == 1
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: reader, now=now + timedelta(seconds=301)) == 1
    assert len(calls) == 1 and peak[0] == 1, "the deferred post read resumes after release"


def test_f7_two_post_workers_do_not_read_the_same_account_together(synced, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    store, account = synced
    enable(store, account)
    reader, entered, release = _native_reader(account), threading.Event(), threading.Event()
    calls = []
    def page(**kw):
        calls.append(kw)
        entered.set()
        assert release.wait(5)
        return raw_page([post(901, [(11, "photo")], "3.00", preview=[])])
    monkeypatch.setattr(reader, "posts_page", page)
    monkeypatch.setattr(reader, "post_by_id", None)
    now = utcnow()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(posts.process_posts, store, TENANT, transport_factory=lambda *a: reader, now=now)
        assert entered.wait(5)
        try:
            b = pool.submit(posts.process_posts, store, TENANT, transport_factory=lambda *a: reader, now=now)
            assert b.result(timeout=5) == 0
        finally:
            release.set()
        assert a.result(timeout=5) == 1
    assert len(calls) == 1


def test_f7_a_rate_limited_read_throttles_the_next_reader_of_the_same_account(synced, monkeypatch):
    """Preserve shared pacing via current backoff rather than a second fixed gate."""
    from onlyfans_runtime.provider_read_budget import acquire, release
    from db.onlyfans_connection_models import OfProviderConnection
    store, account = synced
    enable(store, account)
    with Session(store.engine) as s:
        revision = s.get(OfProviderConnection, account).revision
    claim = acquire(store, TENANT, account, connection_revision=revision, kind="recent")
    release(store, TENANT, claim, retry_after=5)
    reader = _native_reader(account)
    monkeypatch.setattr(reader, "posts_page", lambda **kw: pytest.fail("native post read ignored the shared backoff"))
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: reader, now=utcnow()) == 1
    with Session(store.engine) as s:
        assert s.get(Account, account).posts_error == "shared_provider_read_budget"


# ── 8: the carrier's authorization is a real scope/permission check ─────

from datetime import datetime
from types import SimpleNamespace
from onlyfans_runtime import content_sync_carrier as carrier, content_sync_media as csm
from test_onlyfans_content_sync_delivery_pg import world, make, saved, commit  # noqa: F401 (fixture)
from db.onlyfans_offer_import_models import OfContentSyncAsset

CARRIER_CONNECTION = SimpleNamespace(revision=2, binding_id="binding-a", owner_id=7001, expires_at=datetime(2030, 1, 1))
UPLOAD_TOKEN = "0f0e0d0c-0b0a-4908-8706-050403020100"


@pytest.fixture
def spied_client(monkeypatch):
    monkeypatch.setattr("onlyfans_runtime.connections._open", lambda row, tenant: {"capture": {}, "xHash": "x", "proxyUrl": None})
    captured = {}

    class Spy:
        def __init__(self, session, *, authorize, job_id, **kw):
            captured["authorize"] = authorize
    monkeypatch.setattr("onlyfans_runtime.vault_upload_transport.NativeVaultUpload", Spy)
    monkeypatch.setattr("onlyfans_runtime.provider_connector.session_from_capture", lambda **kw: object())
    return captured


def test_f8_a_carrier_without_authority_refuses_every_write(spied_client, monkeypatch):
    monkeypatch.setenv(carrier.ENV, "acc-a")
    transport = carrier.CarrierTransport(CARRIER_CONNECTION, "tenant-a", "acc-a")
    with pytest.raises(csm.PermissionRequired):
        transport._client(UPLOAD_TOKEN)
    assert spied_client == {}, "authority is required even before opening the native client"


def test_f8_the_carrier_consults_its_authority_and_the_account_approval_at_every_write(spied_client, monkeypatch):
    monkeypatch.setenv(carrier.ENV, "acc-a")
    calls = []
    transport = carrier.CarrierTransport(CARRIER_CONNECTION, "tenant-a", "acc-a")
    transport.set_authority(lambda: calls.append(1), upload_token=UPLOAD_TOKEN)
    transport._client(UPLOAD_TOKEN)
    spied_client["authorize"]()
    spied_client["authorize"]()
    assert len(calls) == 3, "client creation and each adapter boundary require current authority"
    monkeypatch.setenv(carrier.ENV, "acc-b")
    with pytest.raises(csm.PermissionRequired):
        spied_client["authorize"]()
    assert len(calls) == 3, "removed account approval refuses before consulting the callback"
    monkeypatch.setenv(carrier.ENV, "acc-a")
    spied_client["authorize"]()
    assert len(calls) == 4
    transport.set_authority(lambda: (_ for _ in ()).throw(Refused("lease lost")), upload_token=UPLOAD_TOKEN)
    with pytest.raises(csm.PermissionRequired):
        spied_client["authorize"]()


def _leased_asset(w):
    files, blobs = make(1)
    draft = saved(w, files, blobs)
    commit(w, draft)
    with Session(w.store.engine) as s:
        asset_id = s.scalar(select(OfContentSyncAsset.asset_id).where(OfContentSyncAsset.account_id == w.account))
    lease = csm._lease(w.store, TENANT, w.account, asset_id, utcnow())
    assert lease not in (None, 1)
    return lease


def test_f8_lease_authority_follows_the_asset_lease_and_the_connection(world):
    lease = _leased_asset(world)
    world.store.content_upload_permission_reader = lambda **kw: True
    authorize = lambda: csm._authorize_upload_boundary(world.store, TENANT, lease)
    authorize()                                                       # the leased, unchanged scope is allowed
    with world.engine.begin() as c:
        c.execute(text("UPDATE of_provider_connection SET revision = revision + 1"))
    with pytest.raises(Refused):
        authorize()                                                    # a reconnected/changed connection is not
    with world.engine.begin() as c:
        c.execute(text("UPDATE of_provider_connection SET revision = revision - 1"))
    authorize()
    with world.engine.begin() as c:
        c.execute(text("UPDATE of_content_sync_asset SET lease_token = '00000000-0000-4000-8000-000000000000'"))
    with pytest.raises(Refused):
        authorize()                                                    # another worker took the lease
    with world.engine.begin() as c:
        c.execute(text("UPDATE of_content_sync_asset SET lease_token = :t"), {"t": lease.token})
        c.execute(text("UPDATE of_content_sync_asset SET lease_until = now() - interval '1 second'"))
    with pytest.raises(Refused):
        authorize()                                                    # the lease expired


def test_f8_process_uploads_hands_the_carrier_a_live_authority(world):
    files, blobs = make(1)
    draft = saved(world, files, blobs)
    commit(world, draft)
    seen = []
    world.store.content_upload_permission_reader = lambda **kw: True

    class Quiet:
        def set_authority(self, authority, **scope):
            seen.append((authority, scope))
        def lookup(self, *, upload_token):
            return None
        def upload(self, *, upload_token, path, kind):
            assert seen[0][1]["upload_token"] == upload_token
            seen[0][0]()  # a real worker lease and current staff callback at the write boundary
            return 101
        def verify(self, *, provider_media_id):
            return "ready"
    csm.process_uploads(world.store, TENANT, limit=1, transport_for=lambda *a: Quiet())
    assert len(seen) == 1 and callable(seen[0][0])


@pytest.mark.parametrize("terminal_status", ["partial", "failed"])
def test_terminal_source_detail_failure_stays_partial_until_authoritative_success(synced, terminal_status, monkeypatch):
    """Scheduling completion and unresolved provider detail coverage are distinct."""
    from db.models import OfCreatorBinding
    from db.onlyfans_content_sync_progress_models import OfContentSyncSource as Source
    from test_onlyfans_offer_import_pg import refresh
    from onlyfans_runtime.provider_connector import NativeAuthenticationError
    store, account = synced
    enable(store, account)
    while offer_import.process_inventory_jobs(store, TENANT):
        pass
    if terminal_status == "partial":
        monkeypatch.setattr(offer_import, "REFRESH_PAGES_PER_CHAT", 1)
    history(store, account, sales([(640, 2000, [15], 1007)]))
    at = clock()
    assert process_due(store, TENANT, now=at) == 1
    view = content_sync.get_status(store, TENANT, account, now=at + timedelta(seconds=1))["sync"]
    assert view["initial"]["status"] == "complete" and view["backlog"] == 0
    assert view["coverage"]["paidMessages"]["state"] == "partial"
    assert "waiting for provider detail checks" in view["coverage"]["paidMessages"]["detail"]
    assert json.loads(ops(store, account, "incremental")[-1].coverage_json)["pendingSourceDetails"] == 1
    assert ops(store, account, "incremental")[-1].status == "complete", "the scheduling pass completed durably"
    with Session(store.engine) as s, s.begin():
        job = s.scalar(select(OfOfferImportJob).where(OfOfferImportJob.account_id == account,
            OfOfferImportJob.mode == "refresh", OfOfferImportJob.status == "pending"))
        identity, binding = job.job_id, job.binding_id
        # Historical errors from an ended binding, even for the same source
        # identity, must not hold the current creator's coverage.
        s.add(OfCreatorBinding(id="ended-source-binding", tenant_id=TENANT, creator_id="prior-creator",
            account_id=account, channel="onlyfans", revoked_at=utcnow()))
        s.flush()
        s.add(Source(account_id=account, binding_id="ended-source-binding", source_kind="message", source_id=2000,
            fan_id=640, occurrence=0, refresh_requested_at=at, refresh_completed_at=at,
            refresh_error="prior_binding_detail_failure"))
    if terminal_status == "partial":
        reader = FakeHistory({(640, None): ([raw(3000, [15], 1007, previews=[], locked=False, fan=640)], True)})
    else:
        class Rejected:
            def messages(self, **kw):
                raise NativeAuthenticationError("synthetic authentication refusal")
        reader = Rejected()
    assert process_refresh_jobs(store, TENANT, transport_factory=factory(reader)) == 1
    with Session(store.engine) as s:
        job = s.get(OfOfferImportJob, (account, identity))
        audit = (job.status, job.last_error, job.completed_at)
        source = s.get(Source, (account, binding, "message", 2000))
        assert job.status == terminal_status and source.refresh_completed_at is not None and source.refresh_error
        assert source.refresh_job_id is None
    detail = content_sync.get_status(store, TENANT, account, now=at + timedelta(seconds=1))["sync"]["coverage"]["paidMessages"]
    assert detail["state"] == "partial", "terminal unresolved detail is not current coverage"
    assert "unresolved" in detail["detail"] and "waiting" not in detail["detail"]
    assert "page_budget" not in detail["detail"] and "authentication_rejected" not in detail["detail"]
    assert count(store, OfOfferImportJob, OfOfferImportJob.mode == "refresh") == 1, "reading unresolved coverage never retries provider work"
    refresh(store, account, [(640, 2000)], {(640, None): ([raw(2000, [15], 1007,
        previews=[], locked=False, fan=640)], False)}, n=9400)
    detail = content_sync.get_status(store, TENANT, account, now=at + timedelta(seconds=1))["sync"]["coverage"]["paidMessages"]
    assert detail["state"] == "current", "fresh successful evidence clears only the current binding's failed receipt"
    with Session(store.engine) as s:
        assert s.get(Source, (account, binding, "message", 2000)).refresh_error is None
        assert s.get(Source, (account, "ended-source-binding", "message", 2000)).refresh_error == "prior_binding_detail_failure"
        job = s.get(OfOfferImportJob, (account, identity))
        assert (job.status, job.last_error, job.completed_at) == audit, "prior terminal job evidence is retained"
