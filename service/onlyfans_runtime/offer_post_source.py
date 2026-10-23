"""Paid feed posts as offer sources: the same observation/review/draft pipeline.

Contract verified read-only on a real creator session on 23 Oct 2026:
GET /users/{owner}/posts (body "{}") -> {list, hasMore, headMarker, tailMarker}.
A paid post has an int id, postedAt, a decimal price, ordered media and
`preview`: a list of media ids shown free. Posts carry no lockedText; their
text is public on the feed, so it is recorded as an explicit False. Free posts
(no price) are not offers. Nothing here sends, publishes or edits a post.
"""
from datetime import timedelta, timezone
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.onlyfans_offer_import_models import OfContentSyncAccount
from .native_protocol import NativeProtocolError, cents, load
from .offer_terms import OfferObservation
from .store import Refused, _id, utcnow

PAGE_LIMIT = 10
PAGES_PER_PASS = 20
INTERVAL_SECONDS = 300
MAX_PAGE_BYTES = 1024 * 1024


def parse_page(raw, *, owner_id):
    """Strict page validation; one malformed row refuses the page (no partial guesses)."""
    _id(owner_id)
    value = load(raw, max_bytes=MAX_PAGE_BYTES)
    try:
        rows, more, tail = value["list"], value["hasMore"], value.get("tailMarker")
        if type(rows) is not list or len(rows) > PAGE_LIMIT or type(more) is not bool or more and not rows:
            raise ValueError()
        if tail is not None and type(tail) is not str:
            raise ValueError()
        observations, seen = [], set()
        for row in rows:
            if type(row) is not dict or row.get("responseType") != "post":
                raise ValueError()
            post = _id(row["id"])
            if post in seen:
                raise ValueError()
            seen.add(post)
            author = row.get("author")
            if author is not None and (type(author) is not dict or author.get("id") != owner_id):
                raise ValueError()
            offer = from_post(row)
            if offer is not None:
                observations.append(offer)
        return observations, more, tail
    except (KeyError, TypeError, ValueError, Refused):
        raise NativeProtocolError("unsupported native posts page") from None


def from_post(row):
    """Observation for one paid post, or None for a free post."""
    price = row.get("price")
    if price is None or price in (0, "0") or cents(price) == 0:
        return None
    media = row.get("media")
    if type(media) is not list or not media or any(type(m) is not dict for m in media):
        raise ValueError()
    ids = tuple(_id(m.get("id")) for m in media)
    reasons = []
    previews = None
    if "preview" in row:
        raw = row["preview"]
        if type(raw) is list and all(type(v) is int for v in raw) and len(set(raw)) == len(raw) and set(raw) <= set(ids):
            previews = tuple(raw)
        else:
            reasons.append("unsupported_previews")
    posted = row.get("postedAt")
    if type(posted) is not str:
        raise ValueError()
    return OfferObservation("post", _id(row["id"]), None, "feed", posted, ids, previews, False,
        cents(price), "USD", tuple(reasons)).normalized()


def record(store, tenant, account, observations, *, connection_revision, now=None):
    """Persist observations under the current binding and connection revision; idempotent."""
    from db.onlyfans_connection_models import OfProviderConnection
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        connection = s.get(OfProviderConnection, account)
        if (connection is None or connection.binding_id != runtime.binding_id
                or connection.revision != connection_revision or connection.status != "verified"):
            raise Refused("connection changed")
        store._observe_offers(s, runtime, [o.normalized() for o in observations])
        row = s.get(OfContentSyncAccount, account, with_for_update=True)
        if row is not None and row.binding_id == runtime.binding_id:
            row.posts_checked_at, row.posts_error = now or utcnow(), None
        return len(observations)


def _transport(connection, tenant, account):
    from .offer_import_worker import _transport as history_transport
    return history_transport(connection, tenant, account)


def process_posts(store, tenant, *, limit=1, stop=None, transport_factory=_transport, now=None):
    """Worker step: for due enabled accounts, read newest paid posts until known ones.

    Bounded to PAGES_PER_PASS pages per account per pass and one pass per
    INTERVAL_SECONDS. A restart re-reads from the newest page; inserts are
    idempotent, so nothing is double counted and no durable cursor is needed.
    """
    from db.onlyfans_connection_models import OfProviderConnection
    from db.onlyfans_offer_import_models import OfOfferObservation
    import os
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise Refused("paid-post reading requires the worker role")
    now = now or utcnow()
    with Session(store.engine) as s:
        due = s.execute(select(OfContentSyncAccount.account_id).where(OfContentSyncAccount.enabled.is_(True),
            OfContentSyncAccount.status == "active",
            (OfContentSyncAccount.posts_checked_at.is_(None))
            | (OfContentSyncAccount.posts_checked_at <= now - timedelta(seconds=INTERVAL_SECONDS)))
            .order_by(OfContentSyncAccount.posts_checked_at.asc().nulls_first()).limit(limit)).scalars().all()
    done = 0
    for account in due:
        if stop is not None and stop.is_set():
            break
        with Session(store.engine) as s:
            runtime = store._runtime(s, tenant, account)
            connection = s.get(OfProviderConnection, account)
            if (connection is None or connection.binding_id != runtime.binding_id or connection.status != "verified"
                    or connection.expires_at <= now):
                continue
            revision, owner, binding = connection.revision, connection.owner_id, runtime.binding_id
            transport = transport_factory(connection, tenant, account)

        def known(ids):
            with Session(store.engine) as s:
                return set(s.scalars(select(OfOfferObservation.source_id).where(
                    OfOfferObservation.account_id == account, OfOfferObservation.binding_id == binding,
                    OfOfferObservation.source_kind == "post", OfOfferObservation.source_id.in_(ids))).all())
        marker, found, error = None, [], None
        # No database session is open across provider I/O.
        for _ in range(PAGES_PER_PASS):
            try:
                observations, more, marker = parse_page(transport.posts_page(before_marker=marker), owner_id=owner)
            except NativeProtocolError:
                error = "unsupported_posts_response"
                break
            except Exception:
                error = "posts_read_failed"
                break
            seen = known([o.source_id for o in observations]) if observations else set()
            fresh = [o for o in observations if o.source_id not in seen]
            found.extend(fresh)
            if not more or not marker or (observations and not fresh):
                break
        try:
            record(store, tenant, account, found, connection_revision=revision, now=now)
        except Refused:
            continue
        if error:
            with Session(store.engine) as s, s.begin():
                row = s.get(OfContentSyncAccount, account, with_for_update=True)
                if row is not None:
                    row.posts_error = error
        done += 1
    return done


def coverage(row, enabled, now):
    if not enabled or row is None:
        return {"state": "not_started", "checkedAt": None, "detail": "Automatic sync is off."}
    if row.posts_checked_at is None:
        return {"state": "not_started", "checkedAt": None, "detail": "Paid posts have not been read yet."}
    stamp = row.posts_checked_at.replace(tzinfo=None).isoformat() + "Z"
    if row.posts_error:
        return {"state": "partial", "checkedAt": stamp, "detail": "The latest paid-post read stopped early."}
    stale = now - row.posts_checked_at > timedelta(seconds=2 * INTERVAL_SECONDS)
    return {"state": "delayed" if stale else "current", "checkedAt": stamp,
        "detail": "The last paid-post read is more than ten minutes old." if stale else "Paid posts are up to date."}
