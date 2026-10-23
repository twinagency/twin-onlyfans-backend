"""Resumable paid-post archive and revisits use source identities, not known-ID stopping."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_offer_post_source_pg import ready as post_ready, post, raw_page, FakePosts
from test_onlyfans_connections_pg import TENANT
from db.onlyfans_offer_import_models import OfContentSyncAccount, OfOfferImportCandidate, OfOfferObservation
from db.onlyfans_content_sync_progress_models import OfContentPostProgress, OfContentSyncOccurrence, OfContentSyncSource
from onlyfans_runtime import offer_post_source as posts
from onlyfans_runtime.offer_import import get_candidate
from onlyfans_runtime.store import utcnow
from test_onlyfans_connections_pg import ACTOR


def test_pending_connection_recheck_pauses_sync_and_post_reads(post_ready):
    from db.onlyfans_connection_models import OfProviderConnection
    from onlyfans_runtime import content_sync
    store, account = post_ready
    with Session(store.engine) as s, s.begin():
        s.get(OfProviderConnection, account).last_error = "provider_connection_check_retry"
    assert content_sync.get_status(store, TENANT, account)["sync"]["status"] == "paused_auth"
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: (_ for _ in ()).throw(
        AssertionError("held connection opened a provider read"))) == 0


def test_multiple_pages_resume_across_restart_and_revisit_old_edits(post_ready, monkeypatch):
    store, account = post_ready
    monkeypatch.setattr(posts, "PAGES_PER_PASS", 2)
    pages = {
        None: raw_page([post(i, [(11, "photo")], "10.07", preview=[]) for i in range(1000, 990, -1)],
            more=True, tail="900.000000"),
        "900.000000": raw_page([post(i, [(11, "photo")], "10.07", preview=[]) for i in range(990, 980, -1)],
            more=True, tail="800.000000"),
        "800.000000": raw_page([post(i, [(11, "photo")], "10.07", preview=[]) for i in range(980, 975, -1)])}
    fake = FakePosts(pages)
    now = utcnow()
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: fake, now=now) == 1
    with Session(store.engine) as s:
        progress = s.get(OfContentPostProgress, account)
        assert progress.history_marker == "800.000000" and progress.history_complete is False
        assert posts.coverage(s.get(OfContentSyncAccount, account), True, now, progress)["state"] == "partial"
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: fake, now=now + timedelta(seconds=301)) == 1
    assert fake.calls == [None, "900.000000", None, "800.000000"]
    with Session(store.engine) as s:
        progress = s.get(OfContentPostProgress, account)
        assert progress.history_complete and progress.history_marker is None
        assert s.scalar(select(func.count()).select_from(OfOfferObservation)) == 25
        job = s.get(OfContentSyncAccount, account).initial_job_id
        candidate = s.scalar(select(OfOfferImportCandidate).where(OfOfferImportCandidate.job_id == job))
        assert candidate.source_count == 25
        candidate_id = candidate.media_key
    detail = get_candidate(store, TENANT, account, job, candidate_id)
    assert all(o["sourceKind"] == "post" and o["fanId"] is None for o in detail["observations"])
    # Later edit behind every newest-page boundary is found on fair revisits.
    pages["800.000000"] = raw_page([post(i, [(11, "photo")], "19.99" if i == 976 else "10.07", preview=[])
        for i in range(980, 975, -1)])
    posts.process_posts(store, TENANT, transport_factory=lambda *a: fake, now=now + timedelta(seconds=602))
    posts.process_posts(store, TENANT, transport_factory=lambda *a: fake, now=now + timedelta(seconds=903))
    with Session(store.engine) as s:
        events = s.scalars(select(OfContentSyncOccurrence).where(OfContentSyncOccurrence.account_id == account,
            OfContentSyncOccurrence.source_kind == "post", OfContentSyncOccurrence.source_id == 976)).all()
        assert len(events) == 2
        current = s.get(OfContentSyncSource, (account, s.get(OfContentSyncAccount, account).binding_id, "post", 976))
        assert s.get(OfOfferObservation, (account, current.current_observation_id)).price_cents == 1999
        assert s.scalar(select(OfOfferImportCandidate.source_count).where(OfOfferImportCandidate.job_id == job)) == 25


def test_duplicate_boundary_rows_are_idempotent_but_stalled_markers_stay_partial(post_ready, monkeypatch):
    store, account = post_ready
    monkeypatch.setattr(posts, "PAGES_PER_PASS", 3)
    fake = FakePosts({None: raw_page([post(901, [(11, "photo")], "10.07", preview=[])], more=True, tail="1.000000"),
        "1.000000": raw_page([post(901, [(11, "photo")], "10.07", preview=[])], more=True, tail="1.000000")})
    posts.process_posts(store, TENANT, transport_factory=lambda *a: fake)
    with Session(store.engine) as s:
        sync = s.get(OfContentSyncAccount, account)
        assert sync.posts_error == "unsupported_posts_response"
        progress = s.get(OfContentPostProgress, account)
        assert progress.history_marker == "1.000000" and progress.history_complete is False
        assert s.scalar(select(func.count()).select_from(OfOfferObservation)) == 1
        assert posts.coverage(sync, True, utcnow(), progress)["state"] == "partial"


def test_known_paid_post_becoming_free_marks_source_unavailable(post_ready):
    store, account = post_ready
    fake = FakePosts({None: raw_page([post(901, [(11, "photo")], "10.07", preview=[])])})
    now = utcnow()
    posts.process_posts(store, TENANT, transport_factory=lambda *a: fake, now=now)
    fake.pages[None] = raw_page([post(901, [(11, "photo")])])
    posts.process_posts(store, TENANT, transport_factory=lambda *a: fake, now=now + timedelta(seconds=301))
    with Session(store.engine) as s:
        binding = s.get(OfContentSyncAccount, account).binding_id
        source = s.get(OfContentSyncSource, (account, binding, "post", 901))
        assert source.source_unavailable and source.unavailable_reason == "post_no_longer_paid"
        assert s.scalar(select(func.count()).select_from(OfOfferObservation)) == 1, "old evidence remains immutable"


def test_native_history_waits_for_audited_account_bound_cursor_validation(post_ready, monkeypatch):
    store, account = post_ready
    from onlyfans_runtime.provider_connector import NativeProviderConnector
    pages = {None: raw_page([post(901, [(11, "photo")], "10.07", preview=[])], more=True, tail="1.000000"),
        "1.000000": raw_page([post(900, [(11, "photo")], "10.07", preview=[])])}
    calls = []
    def read(self, *, before_marker=None, limit=10):
        calls.append(before_marker)
        return pages[before_marker]
    monkeypatch.setattr(NativeProviderConnector, "posts_page", read)
    now = utcnow()
    posts.process_posts(store, TENANT, now=now)
    assert calls == [None], "a parameter convention cannot authorize a historical walk"
    with Session(store.engine) as s:
        assert s.get(OfContentSyncAccount, account).posts_error == "post_cursor_contract_unverified"
    result = posts.verify_cursor_contract(store, TENANT, account, command_key="f" * 64, actor=ACTOR)
    assert result["verified"] is True and calls == [None, None, "1.000000"]
    assert posts.verify_cursor_contract(store, TENANT, account, command_key="f" * 64, actor=ACTOR) == result
    assert calls == [None, None, "1.000000"], "lost acknowledgement replays the audit receipt"
    posts.process_posts(store, TENANT, now=now + timedelta(seconds=301))
    assert calls[-2:] == [None, "1.000000"]
    with Session(store.engine) as s:
        progress = s.get(OfContentPostProgress, account)
        assert progress.history_complete and progress.cursor_evidence_json is not None


def test_direct_old_post_revisit_finds_edit_and_explicit_deletion(post_ready):
    store, account = post_ready
    now = utcnow()
    class Reader(FakePosts):
        def post_by_id(self, post_id):
            assert post_id == 901
            return self.detail
    reader = Reader({None: raw_page([post(901, [(11, "photo")], "10.07", preview=[])])})
    posts.process_posts(store, TENANT, transport_factory=lambda *a: reader, now=now)
    reader.pages[None] = raw_page([post(902, [(12, "photo")], "3.00", preview=[])])
    import json
    reader.detail = (200, json.dumps(post(901, [(11, "photo")], "19.99", preview=[])).encode())
    posts.process_posts(store, TENANT, transport_factory=lambda *a: reader, now=now + timedelta(seconds=301))
    with Session(store.engine) as s:
        binding = s.get(OfContentSyncAccount, account).binding_id
        source = s.get(OfContentSyncSource, (account, binding, "post", 901))
        assert source.occurrence == 2 and s.get(OfOfferObservation, (account, source.current_observation_id)).price_cents == 1999
    reader.detail = (404, b'{}')
    posts.process_posts(store, TENANT, transport_factory=lambda *a: reader, now=now + timedelta(seconds=602))
    with Session(store.engine) as s:
        source = s.get(OfContentSyncSource, (account, binding, "post", 901))
        assert source.source_unavailable and source.unavailable_reason == "post_deleted"
        assert source.occurrence == 3, "404 is a distinct source-state event"
        assert s.scalar(select(func.count()).select_from(OfOfferObservation).where(OfOfferObservation.source_id == 901)) == 2, "404 never rewrites a commercial snapshot"


def test_deleted_source_reappearance_keeps_hold_until_audited_resolution(post_ready):
    import json
    from db.models import CatalogItem
    from db.onlyfans_offer_import_models import OfContentSyncConflict
    from onlyfans_runtime.offer_import import create_draft
    from onlyfans_runtime import content_sync
    store, account = post_ready
    now = utcnow()
    class Reader(FakePosts):
        def post_by_id(self, post_id):
            return (404, b'{}') if post_id == 901 else (
                200, json.dumps(post(post_id, [(12, "photo")], "3.00", preview=[])).encode())
    reader = Reader({None: raw_page([post(901, [(11, "photo")], "10.07", preview=[])])})
    posts.process_posts(store, TENANT, transport_factory=lambda *a: reader, now=now)
    with Session(store.engine) as s:
        observed = s.scalar(select(OfOfferObservation).where(OfOfferObservation.source_id == 901))
        oid, fingerprint = observed.observation_id, observed.fingerprint
    item_id = int(create_draft(store, TENANT, account, actor=ACTOR, draft={"commandKey": "a" * 64,
        "observationId": oid, "expectedFingerprint": fingerprint, "label": "Synthetic photograph",
        "description": "One approved photograph.", "reuseConfirmed": True, "descriptionApproved": True})["itemId"])
    reader.pages[None] = raw_page([post(902, [(12, "photo")], "3.00", preview=[])])
    posts.process_posts(store, TENANT, transport_factory=lambda *a: reader, now=now + timedelta(seconds=301))
    with Session(store.engine) as s:
        conflict = s.scalar(select(OfContentSyncConflict).where(OfContentSyncConflict.item_id == item_id))
        assert conflict.kind == "post_deleted" and conflict.status == "open"
        assert content_sync.sale_hold(s, account, s.get(CatalogItem, item_id)) == "open_conflict"
    reader.pages[None] = raw_page([post(901, [(11, "photo")], "10.07", preview=[])])
    posts.process_posts(store, TENANT, transport_factory=lambda *a: reader, now=now + timedelta(seconds=602))
    with Session(store.engine) as s:
        conflict = s.scalar(select(OfContentSyncConflict).where(OfContentSyncConflict.item_id == item_id))
        view = content_sync.conflict_view(s, conflict)
        assert content_sync.sale_hold(s, account, s.get(CatalogItem, item_id)) == "open_conflict", "reappearance is not operator approval"
        assert view["sourceIssue"] is None
    content_sync.resolve_conflict(store, TENANT, account, view["conflictId"], resolution="keep_crm",
        expected_revision=view["revision"], expected_item_fingerprint=view["itemFingerprint"], command_key="b" * 64, actor=ACTOR)
    with Session(store.engine) as s:
        assert content_sync.sale_hold(s, account, s.get(CatalogItem, item_id)) is None
