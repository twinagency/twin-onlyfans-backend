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


AUDIT_SECONDS = 24 * 3600
_KEEP = object()


def _deeper(marker, current):
    """Whether `marker` reaches further back in time than `current` (markers are decreasing publish times)."""
    if current is None:
        return True
    try:
        return float(marker) < float(current)
    except (TypeError, ValueError):
        return False


def _candidate(s, account, job_id, row, now):
    """One review candidate per attachment set, counted once per post and attachment set."""
    from db.onlyfans_offer_import_models import OfOfferImportCandidate
    from .native_context import canonical
    from .offer_import import _date, _merge
    media = json.loads(row["media_json"])
    candidate = s.get(OfOfferImportCandidate, (account, job_id, row["media_key"]), with_for_update=True)
    if candidate is None:
        candidate = OfOfferImportCandidate(account_id=account, job_id=job_id, media_key=row["media_key"],
            media_json=canonical(sorted(media)), source_count=0, variants_json="[]", samples_json="[]",
            revision=0, updated_at=now)
        s.add(candidate)
    _merge(candidate, media, row["price_cents"], {"fanId": None, "messageId": row["source_id"], "sourceKind": "post",
        "priceCents": row["price_cents"], "sentAt": _date(row["source_at"])})
    candidate.updated_at = now


def record(store, tenant, account, observations, *, connection_revision, now=None, state=None):
    """Persist observations under the current binding and connection revision; idempotent.

    A post seen for the first time (or with a new attachment set) also joins the account's candidate list, so
    it can be reviewed and turned into a draft like any message offer. `state` updates the durable walk
    position atomically with the observations it was read from.
    """
    from db.onlyfans_connection_models import OfProviderConnection
    from db.onlyfans_offer_import_models import OfOfferObservation, OfOfferImportJob
    now = now or utcnow()
    state = state or {}
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        connection = s.get(OfProviderConnection, account)
        if (connection is None or connection.binding_id != runtime.binding_id
                or connection.revision != connection_revision or connection.status != "verified"):
            raise Refused("connection changed")
        offers = [o.normalized() for o in observations]
        row = s.get(OfContentSyncAccount, account, with_for_update=True)
        known = set()
        if offers:
            known = set(s.execute(select(OfOfferObservation.source_id, OfOfferObservation.media_key).where(
                OfOfferObservation.account_id == account, OfOfferObservation.binding_id == runtime.binding_id,
                OfOfferObservation.source_kind == "post", OfOfferObservation.source_id.in_([o.source_id for o in offers]))).all())
        inserted = store._observe_offers(s, runtime, offers)
        if row is not None and row.binding_id == runtime.binding_id:
            job = s.get(OfOfferImportJob, (account, row.initial_job_id)) if row.initial_job_id else None
            for fresh in inserted:
                if (fresh["source_id"], fresh["media_key"]) in known:
                    continue
                known.add((fresh["source_id"], fresh["media_key"]))
                if job is not None and job.binding_id == runtime.binding_id:
                    _candidate(s, account, job.job_id, fresh, now)
            if state.get("checked"):
                row.posts_checked_at = now
            if "error" in state:
                row.posts_error = state["error"]
            elif observations or state.get("checked") or "cursor" in state:
                row.posts_error = None
            for column in ("cursor", "complete", "audit_after"):
                if column in state:
                    setattr(row, {"cursor": "posts_cursor", "complete": "posts_history_complete",
                                  "audit_after": "posts_audit_after"}[column], state[column])
        return len(observations)


def _transport(connection, tenant, account):
    from .offer_import_worker import _transport as history_transport
    return history_transport(connection, tenant, account)


def process_posts(store, tenant, *, limit=1, stop=None, transport_factory=_transport, now=None):
    """Worker step: read paid posts for due enabled accounts, one gated page at a time.

    * Head: the newest pages, until a page shows nothing new or changed (an edited known post counts as
      changed), at most PAGES_PER_PASS pages.
    * History: a durable cursor continues into older pages across passes until the end of the feed; a full
      re-read starts every AUDIT_SECONDS so edits to old posts are noticed too. Each pass advances history by
      at least one page even when the head used its whole budget.
    * Every page needs the account's provider-read lease and pace (provider_read_gate), so concurrent
      workers and other readers of the same account never overlap or exceed the intended rate. When the
      account is busy the pass simply ends; the saved position resumes later.
    """
    from sqlalchemy import and_, or_
    from db.onlyfans_connection_models import OfProviderConnection
    from db.onlyfans_offer_import_models import OfOfferObservation
    from . import provider_read_gate as gate
    import os
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise Refused("paid-post reading requires the worker role")
    now = now or utcnow()
    Account = OfContentSyncAccount
    with Session(store.engine) as s:
        due = s.execute(select(Account.account_id).where(Account.enabled.is_(True), Account.status == "active",
            or_(Account.posts_checked_at.is_(None), Account.posts_checked_at <= now - timedelta(seconds=INTERVAL_SECONDS),
                and_(Account.posts_history_complete.is_(False), Account.posts_cursor.is_not(None), Account.posts_error.is_(None)),
                and_(Account.posts_history_complete.is_(True), Account.posts_audit_after <= now)),
            ~gate.blocked(Account.account_id, utcnow()))
            .order_by(Account.posts_checked_at.asc().nulls_first()).limit(limit)).scalars().all()
    done = 0
    for account in due:
        if stop is not None and stop.is_set():
            break
        with Session(store.engine) as s:
            runtime = store._runtime(s, tenant, account)
            connection = s.get(OfProviderConnection, account)
            row = s.get(Account, account)
            if (row is None or connection is None or connection.binding_id != runtime.binding_id or connection.status != "verified"
                    or connection.expires_at <= now or row.binding_id != runtime.binding_id):
                continue
            revision, owner, binding = connection.revision, connection.owner_id, runtime.binding_id
            cursor, complete, audit = row.posts_cursor, row.posts_history_complete, row.posts_audit_after
            head_due = row.posts_checked_at is None or row.posts_checked_at <= now - timedelta(seconds=INTERVAL_SECONDS)
            transport = transport_factory(connection, tenant, account)
        if complete and audit is not None and audit <= now:
            complete, cursor = False, None          # periodic full re-read: catches edits to old posts
            audit_state = {"complete": False, "cursor": None}
        else:
            audit_state = {}

        def latest(ids):
            with Session(store.engine) as s:
                found = {}
                for source_id, fingerprint in s.execute(select(OfOfferObservation.source_id, OfOfferObservation.fingerprint).where(
                        OfOfferObservation.account_id == account, OfOfferObservation.binding_id == binding,
                        OfOfferObservation.source_kind == "post", OfOfferObservation.source_id.in_(ids))
                        .order_by(OfOfferObservation.observed_at.desc(), OfOfferObservation.occurrence.desc())):
                    found.setdefault(source_id, fingerprint)
                return found

        def read(marker):
            """One gated page: (observations, more, tail) | 'paced' | error string. No database session is open across I/O."""
            lease = gate.acquire(store, account, "posts")
            if lease is None:
                return "paced"
            try:
                return parse_page(transport.posts_page(before_marker=marker), owner_id=owner)
            except NativeProtocolError:
                return "unsupported_posts_response"
            except Exception:
                return "posts_read_failed"
            finally:
                gate.release(store, account, lease)

        attempted, aborted = False, False
        pages_used, reached_end = 0, False

        def save(found, **state):
            record(store, tenant, account, found, connection_revision=revision, now=now, state={**audit_state, **state})
            audit_state.clear()

        # Head: newest first, until nothing is new or changed.
        if head_due:
            marker, head_state = None, {}
            while pages_used < PAGES_PER_PASS:
                page = read(marker)
                if page == "paced":
                    aborted = True
                    break
                attempted = True
                pages_used += 1
                if type(page) is str:
                    head_state = {"error": page, "checked": True}
                    break
                found_obs, more, tail = page
                seen = latest([o.source_id for o in found_obs]) if found_obs else {}
                fresh = [o for o in found_obs if seen.get(o.source_id) != o.fingerprint()]
                if not more or not tail:
                    head_state = {"checked": True, "cursor": None, "complete": True, "audit_after": now + timedelta(seconds=AUDIT_SECONDS)}
                    reached_end = True
                    save(fresh, **head_state)
                    break
                if found_obs and not fresh:
                    head_state = {"checked": True}
                    save([], **head_state)
                    break
                marker = tail
                step = {"cursor": tail} if not complete and _deeper(tail, cursor) else {}
                if step:
                    cursor = tail
                save(fresh, **step)
            else:
                # Budget spent while pages were still new: history continues from where the head stopped.
                head_state = {"checked": True, "complete": False}
                if marker is not None and _deeper(marker, cursor):
                    head_state["cursor"] = cursor = marker
                save([], **head_state)
                complete = False
            if "error" in head_state:
                save([], **head_state)
                aborted = True
        # History: continue into older pages from the durable cursor.
        if not aborted and not reached_end and not complete:
            budget, marker = max(1, PAGES_PER_PASS - pages_used), cursor
            while budget > 0:
                page = read(marker)
                if page == "paced":
                    break
                attempted = True
                budget -= 1
                if type(page) is str:
                    save([], error=page)
                    break
                found_obs, more, tail = page
                seen = latest([o.source_id for o in found_obs]) if found_obs else {}
                fresh = [o for o in found_obs if seen.get(o.source_id) != o.fingerprint()]
                if not more or not tail:
                    save(fresh, cursor=None, complete=True, audit_after=now + timedelta(seconds=AUDIT_SECONDS))
                    break
                marker = tail
                save(fresh, cursor=tail)
        if audit_state and attempted:
            save([])
        done += 1 if attempted else 0
    return done


def coverage(row, enabled, now):
    if not enabled or row is None:
        return {"state": "not_started", "checkedAt": None, "detail": "Automatic sync is off."}
    if row.posts_checked_at is None:
        return {"state": "not_started", "checkedAt": None, "detail": "Paid posts have not been read yet."}
    stamp = row.posts_checked_at.replace(tzinfo=None).isoformat() + "Z"
    if row.posts_error:
        return {"state": "partial", "checkedAt": stamp, "detail": "The latest paid-post read stopped early."}
    if not row.posts_history_complete:
        return {"state": "partial", "checkedAt": stamp, "detail": "Older paid posts are still being read."}
    stale = now - row.posts_checked_at > timedelta(seconds=2 * INTERVAL_SECONDS)
    return {"state": "delayed" if stale else "current", "checkedAt": stamp,
        "detail": "The last paid-post read is more than ten minutes old." if stale else "Paid posts are up to date."}
