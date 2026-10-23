"""Paid feed posts as import sources: strict page parsing, bounded worker reads, exact drafts.

Synthetic posts use the key set observed read-only on a real creator session on
23 Oct 2026. The transport is a local fake; no provider request is made.
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_connections_pg import ACTOR, TENANT
from test_onlyfans_vault_pg import media, sync
from test_onlyfans_provider_adapters import OWNER
from db.models import CatalogItem
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_offer_import_models import OfContentSyncAccount, OfOfferObservation
from onlyfans_runtime import offer_post_source as posts
from onlyfans_runtime.native_protocol import NativeProtocolError
from onlyfans_runtime.offer_import import create_draft
from onlyfans_runtime.store import utcnow

KEYS = ("author,canComment,canDelete,canEdit,canToggleFavorite,canViewMedia,id,isMarkdownDisabled,isMediaReady,"
        "isOpened,labelStates,media,mediaCount,mentionedUsers,postedAt,postedAtPrecise,preview,price,rawText,"
        "releaseForms,responseType,text,tipsAmount,tweetSend").split(",")


def post(pid, ids, price=None, preview=..., author=OWNER):
    row = {k: None for k in KEYS}
    row.update(author={"id": author}, id=pid, responseType="post", postedAt="2026-10-20T09:00:00+00:00",
        postedAtPrecise="1760950800", text="<p>synthetic</p>", rawText="synthetic", isOpened=True,
        canViewMedia=True, mediaCount=len(ids), media=[{"id": i, "type": k, "canView": True, "isReady": True}
                                                         for i, k in ids])
    if price is None:
        row.pop("price"); row.pop("preview")
    else:
        row["price"] = price
        if preview is ...:
            row.pop("preview")
        else:
            row["preview"] = preview
    return row


def raw_page(rows, more=False, tail="1760900000.000000"):
    # Prices as JSON numbers, exactly like the provider (parsed as Decimal).
    text = json.dumps({"list": rows, "hasMore": more, "headMarker": "1760999999.000000", "tailMarker": tail})
    for r in rows:
        if "price" in r:
            text = text.replace(f'"price": "{r["price"]}"', f'"price": {r["price"]}')
    return text.encode()


def test_paid_posts_parse_exactly_and_free_posts_are_not_offers():
    rows = [post(901, [(13, "video"), (14, "video")], "19.99", preview=[13]),
            post(902, [(11, "photo")], "10.07", preview=[]),
            post(903, [(12, "photo")]),  # free post
            post(904, [(15, "photo")], "5.00")]  # preview key absent
    found, more, tail = posts.parse_page(raw_page(rows, more=True), owner_id=OWNER)
    assert more is True and tail == "1760900000.000000"
    by = {o.source_id: o for o in found}
    assert set(by) == {901, 902, 904}, "a free post is not an offer"
    a = by[901]
    assert (a.source_kind, a.source_context, a.fan_id) == ("post", "feed", None)
    assert a.media_ids == (13, 14) and a.preview_ids == (13,) and a.price_cents == 1999 and a.locked_text is False
    assert a.reasons == (), "two paid videos with a video preview are classified at review, not here"
    assert by[902].price_cents == 1007 and by[902].preview_ids == () and by[902].reasons == ()
    assert by[904].preview_ids is None and by[904].reasons == ("previews_unknown",)


@pytest.mark.parametrize("bad", [
    lambda r: r[0].update(author={"id": OWNER + 1}),
    lambda r: r[0].update(responseType="message"),
    lambda r: r.append(dict(r[0])),
    lambda r: r[0].update(media="x"),
])
def test_malformed_posts_page_is_refused_whole(bad):
    rows = [post(901, [(13, "video")], "19.99", preview=[])]
    bad(rows)
    with pytest.raises(NativeProtocolError):
        posts.parse_page(raw_page(rows), owner_id=OWNER)


class FakePosts:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def posts_page(self, *, before_marker=None, limit=10):
        self.calls.append(before_marker)
        return self.pages[before_marker]


def enable(store, account):
    with Session(store.engine) as s, s.begin():
        runtime = s.execute(select(OfProviderConnection.binding_id).where(OfProviderConnection.account_id == account)).scalar()
        now = utcnow()
        s.add(OfContentSyncAccount(account_id=account, binding_id=runtime, enabled=True, status="active", revision=1,
            actor=ACTOR, command_key="e" * 64, backlog=0, failures=0, created_at=now, updated_at=now))


@pytest.fixture
def ready(database, monkeypatch):
    store, account, _, _ = sync(database, monkeypatch, rows=[media(11), media(12), media(13, "video"), media(14, "video")])
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    enable(store, account)
    yield store, account
    store.close()


def test_worker_reads_paid_posts_bounded_and_idempotently_then_drafts_exactly(ready):
    store, account = ready
    fake = FakePosts({None: raw_page([post(901, [(12, "photo"), (13, "video")], "19.99", preview=[12]),
                                      post(902, [(11, "photo")])], more=True, tail="1760900000.000000"),
                      "1760900000.000000": raw_page([post(903, [(11, "photo")], "3.00", preview=[])])})
    now = utcnow()
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: fake, now=now) == 1
    assert fake.calls == [None, "1760900000.000000"], "walks older pages until hasMore is false"
    with Session(store.engine) as s:
        rows = s.scalars(select(OfOfferObservation).where(OfOfferObservation.source_kind == "post")
                         .order_by(OfOfferObservation.source_id)).all()
        assert [(r.source_id, r.price_cents, json.loads(r.media_json), json.loads(r.preview_json), r.locked_text, r.fan_id)
                for r in rows] == [(901, 1999, [12, 13], [12], False, None), (903, 300, [11], [], False, None)]
        assert s.get(OfContentSyncAccount, account).posts_checked_at == now
    # Inside the 300 s interval nothing is read; after it, the newest page is
    # read and known sources are revisited so older edits cannot be starved.
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: fake, now=now + timedelta(seconds=60)) == 0
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: fake, now=now + timedelta(seconds=301)) == 1
    assert fake.calls == [None, "1760900000.000000", None, "1760900000.000000"]
    with Session(store.engine) as s:
        assert s.scalar(select(OfOfferObservation.observation_id).where(OfOfferObservation.source_id == 901)) is not None
        assert len(s.scalars(select(OfOfferObservation).where(OfOfferObservation.source_kind == "post")).all()) == 2
        observation = s.scalar(select(OfOfferObservation).where(OfOfferObservation.source_id == 901))
    result = create_draft(store, TENANT, account, actor=ACTOR, draft={"commandKey": "a" * 64,
        "observationId": observation.observation_id, "expectedFingerprint": observation.fingerprint,
        "label": "Synthetic set", "description": "One photo and one video.", "reuseConfirmed": True,
        "descriptionApproved": True})
    with Session(store.engine) as s:
        item = s.get(CatalogItem, int(result["itemId"]))
        assert (item.enabled, item.pricing_mode, item.price_cents, json.loads(item.media_ids)) == (False, "fixed", 1999, [12, 13])
        assert json.loads(item.preview_media_ids) == [12]


def test_expired_connection_or_disabled_sync_reads_nothing(ready):
    store, account = ready
    fake = FakePosts({None: raw_page([post(901, [(11, "photo")], "3.00", preview=[])])})
    with Session(store.engine) as s, s.begin():
        s.get(OfProviderConnection, account).expires_at = utcnow() - timedelta(seconds=1)
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: fake) == 0 and fake.calls == []
    with Session(store.engine) as s, s.begin():
        s.get(OfProviderConnection, account).expires_at = utcnow() + timedelta(hours=1)
        s.get(OfContentSyncAccount, account).enabled = False
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: fake) == 0 and fake.calls == []


def test_unsupported_page_marks_partial_coverage_without_inventing_offers(ready):
    store, account = ready
    fake = FakePosts({None: b'{"list": "nope", "hasMore": false}'})
    now = utcnow()
    assert posts.process_posts(store, TENANT, transport_factory=lambda *a: fake, now=now) == 1
    with Session(store.engine) as s:
        row = s.get(OfContentSyncAccount, account)
        assert row.posts_error == "unsupported_posts_response"
        assert posts.coverage(row, True, now)["state"] == "partial"
        assert s.scalar(select(OfOfferObservation.observation_id).where(OfOfferObservation.source_kind == "post")) is None
