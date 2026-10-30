"""Paid feed posts as offer sources: the same observation/review/draft pipeline.

Contract verified read-only on a real creator session on 23 Oct 2026:
GET /users/{owner}/posts (body "{}") -> {list, hasMore, headMarker, tailMarker}.
A paid post has an int id, postedAt, a decimal price, ordered media and
`preview`: a list of media ids shown free. Posts carry no lockedText; their
text is public on the feed, so it is recorded as an explicit False. Free posts
(no price) are not offers. Nothing here sends, publishes or edits a post.
"""
from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.onlyfans_offer_import_models import OfContentSyncAccount
from .native_protocol import NativeProtocolError, cents, load
from .offer_terms import OfferObservation
from .store import Refused, _id, utcnow

PAGE_LIMIT = 10
PAGES_PER_PASS = 20
DETAILS_PER_PASS = 2
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


def parse_post(raw, *, owner_id, post_id):
    """A known post's detail must name that exact source and the current owner."""
    try:
        row = load(raw, max_bytes=MAX_PAGE_BYTES)
        if (type(row) is not dict or row.get("responseType") != "post" or _id(row.get("id")) != post_id
                or type(row.get("author")) is not dict or row["author"].get("id") != owner_id):
            raise ValueError()
        return from_post(row)
    except (KeyError, TypeError, ValueError, Refused):
        raise NativeProtocolError("unsupported native post detail") from None


def record(store, tenant, account, observations, *, connection_revision, now=None):
    """Persist observations under the current binding and connection revision; idempotent."""
    from db.onlyfans_connection_models import OfProviderConnection
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        connection = s.get(OfProviderConnection, account)
        if (connection is None or connection.binding_id != runtime.binding_id
                or connection.revision != connection_revision or connection.status != "verified"
                or connection.last_error is not None or connection.verified_at is None or connection.expires_at <= utcnow()):
            raise Refused("connection changed")
        store._observe_offers(s, runtime, [o.normalized() for o in observations])
        row = s.get(OfContentSyncAccount, account, with_for_update=True)
        if row is not None and row.binding_id == runtime.binding_id:
            row.posts_checked_at, row.posts_error = now or utcnow(), None
        return len(observations)


def _transport(connection, tenant, account):
    from .offer_import_worker import _transport as history_transport
    return history_transport(connection, tenant, account)


def verify_cursor_contract(store, tenant, account, *, command_key, actor, transport_factory=_transport):
    """Two bounded read-only pages establish the actual account's cursor contract.

    This is an explicit worker/operator command for release validation, never a
    convention inferred from an API parameter name. The evidence is audited
    and bound to the current verified session; a reconnect invalidates it.
    """
    from hashlib import sha256
    import os
    from decimal import Decimal
    from db.onlyfans_connection_models import OfProviderConnection
    from db.onlyfans_content_sync_progress_models import OfContentPostProgress
    from db.onlyfans_offer_import_models import OfContentSyncDecision
    from .content_sync import _identity, _key_taken
    from .native_context import canonical
    from .provider_read_budget import acquire, current_in_session, release, native_reader
    _identity(command_key, actor)
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise Refused("paid-post verification requires the worker role")
    request = sha256(canonical({"postCursor": "beforePublishTime.v1"}).encode()).hexdigest()
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = s.get(OfContentSyncDecision, (account, command_key))
        if prior is not None:
            if (prior.binding_id, prior.actor, prior.request_hash) != (runtime.binding_id, actor, request):
                raise Refused("sync idempotency conflict")
            return json.loads(prior.result_json)
        _key_taken(s, account, command_key, OfContentSyncDecision)
        connection = s.get(OfProviderConnection, account)
        if (connection is None or connection.binding_id != runtime.binding_id or connection.status != "verified"
                or connection.last_error is not None or connection.verified_at is None or connection.expires_at <= utcnow()):
            raise Refused("verified connection unavailable")
        revision, binding, owner = connection.revision, runtime.binding_id, connection.owner_id
        s.expunge(connection)
    transport, pages, marker = transport_factory(connection, tenant, account), [], None
    read_claim, retry_after = None, None
    try:
        for index in range(2):
            read_claim = acquire(store, tenant, account, connection_revision=revision, kind="posts") if native_reader(transport) else None
            raw = transport.posts_page(before_marker=marker)
            _, more, tail = parse_page(raw, owner_id=owner)
            value = load(raw, max_bytes=MAX_PAGE_BYTES)
            rows = value["list"]
            pages.append({"responseHash": sha256(raw).hexdigest(), "requestedMarker": marker,
                "tailMarker": tail, "ids": [r["id"] for r in rows],
                "times": [r["postedAt"] for r in rows], "hasMore": more})
            if index == 0:
                if not more or not tail or not rows:
                    raise Refused("post cursor cannot be verified without a historical boundary")
                marker = tail
                if read_claim is not None:
                    with Session(store.engine) as s, s.begin():
                        current_in_session(store, s, tenant, read_claim)
                    release(store, tenant, read_claim)
                    read_claim = None
            else:
                first = pages[0]
                if not rows or not set(pages[1]["ids"]) - set(first["ids"]):
                    raise Refused("post cursor did not reach older records")
                if more and (tail is None or Decimal(tail) >= Decimal(marker)):
                    raise Refused("post cursor did not advance")
                first_end = min(datetime.fromisoformat(stamp.replace("Z", "+00:00")) for stamp in first["times"])
                if any(datetime.fromisoformat(stamp.replace("Z", "+00:00")) > first_end for stamp in pages[1]["times"]):
                    raise Refused("post cursor historical ordering is unsupported")
        now = utcnow()
        evidence = {"contract": "beforePublishTime.v1", "accountId": account, "bindingId": binding,
            "connectionRevision": revision, "checkedAt": now.isoformat() + "Z", "actor": actor, "pages": pages}
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account)
            current_connection = s.get(OfProviderConnection, account)
            if (runtime.binding_id != binding or current_connection is None or current_connection.binding_id != binding
                    or current_connection.revision != revision or current_connection.status != "verified"
                    or current_connection.last_error is not None or current_connection.verified_at is None
                    or current_connection.expires_at <= now or current_connection.owner_id != owner):
                raise Refused("post read scope changed")
            if read_claim is not None:
                current_in_session(store, s, tenant, read_claim)
            progress = s.get(OfContentPostProgress, account, with_for_update=True)
            if progress is None:
                progress = OfContentPostProgress(account_id=account, binding_id=binding,
                    connection_revision=revision, history_complete=False)
                s.add(progress)
            if progress.lease_token and progress.lease_until > now:
                raise Refused("post read lease occupied")
            before = {"contract": progress.cursor_contract}
            if (progress.binding_id, progress.connection_revision) != (binding, revision):
                progress.binding_id, progress.connection_revision = binding, revision
                progress.history_marker, progress.history_complete = None, False
                progress.history_checked_at = None
            progress.cursor_contract, progress.cursor_evidence_json = "beforePublishTime.v1", canonical(evidence)
            result = {"verified": True, "contract": progress.cursor_contract, "connectionRevision": revision}
            s.add(OfContentSyncDecision(account_id=account, command_key=command_key, binding_id=binding,
                action="settings", request_hash=request, actor=actor, subject_id="post-cursor-contract",
                before_json=canonical(before), after_json=canonical(evidence), result_json=canonical(result), created_at=now))
            return result
    except Exception as exc:
        retry_after = getattr(exc, "retry_after", None)
        raise
    finally:
        if read_claim is not None:
            release(store, tenant, read_claim, retry_after=retry_after)


def _claim(store, tenant, account, now):
    from db.onlyfans_connection_models import OfProviderConnection
    from db.onlyfans_content_sync_progress_models import OfContentPostProgress
    import uuid
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        sync = s.get(OfContentSyncAccount, account, with_for_update=True)
        connection = s.get(OfProviderConnection, account)
        if (sync is None or not sync.enabled or sync.binding_id != runtime.binding_id
                or connection is None or connection.binding_id != runtime.binding_id
                or connection.status != "verified" or connection.last_error is not None
                or connection.verified_at is None or connection.expires_at <= now):
            return None
        progress = s.get(OfContentPostProgress, account, with_for_update=True)
        if progress is None:
            progress = OfContentPostProgress(account_id=account, binding_id=runtime.binding_id,
                connection_revision=connection.revision, history_complete=False)
            s.add(progress)
        if progress.lease_token and progress.lease_until > now:
            return None
        if progress.next_attempt_at and progress.next_attempt_at > now:
            return None
        if (progress.binding_id, progress.connection_revision) != (runtime.binding_id, connection.revision):
            progress.binding_id, progress.connection_revision = runtime.binding_id, connection.revision
            progress.history_marker, progress.history_complete = None, False
            progress.history_checked_at = None
            progress.cursor_contract = progress.cursor_evidence_json = None
        progress.lease_token, progress.lease_until = str(uuid.uuid4()), now + timedelta(seconds=60)
        progress.next_attempt_at = now + timedelta(seconds=INTERVAL_SECONDS)
        result = (progress.lease_token, connection.revision, connection.owner_id,
            runtime.binding_id, progress.history_marker, progress.cursor_contract, progress.cursor_evidence_json)
        s.expunge(connection)
        return result + (connection,)


def _commit_page(store, tenant, account, token, revision, binding, observations, raw, *, marker, more,
                 now, read_claim=None, error=None, advance=True, unavailable_ids=(), unavailable_reason="post_no_longer_paid"):
    from db.onlyfans_connection_models import OfProviderConnection
    from db.onlyfans_content_sync_progress_models import OfContentPostProgress, OfContentSyncSource
    from .provider_read_budget import current_in_session
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        progress = s.get(OfContentPostProgress, account, with_for_update=True)
        sync = s.get(OfContentSyncAccount, account)
        connection = s.get(OfProviderConnection, account)
        if (progress is None or progress.lease_token != token or progress.lease_until <= utcnow()
                or runtime.binding_id != binding or sync is None or not sync.enabled or sync.binding_id != binding
                or connection is None or connection.binding_id != binding or connection.revision != revision
                or connection.status != "verified" or connection.last_error is not None
                or connection.verified_at is None or connection.expires_at <= utcnow()):
            raise Refused("post read scope changed")
        if read_claim is not None:
            current_in_session(store, s, tenant, read_claim)
        if observations:
            store._observe_offers(s, runtime, observations)
        for source_id in unavailable_ids:
            source = s.get(OfContentSyncSource, (account, binding, "post", source_id))
            if source is not None:
                from .content_sync import mark_source_unavailable
                mark_source_unavailable(s, runtime, source, now, unavailable_reason)
        # Free/retracted known posts cannot leave their former paid terms usable.
        if raw is not None:
            parsed = load(raw, max_bytes=MAX_PAGE_BYTES)
            priced = {o.source_id for o in observations}
            for post in parsed["list"]:
                if post["id"] not in priced:
                    source = s.get(OfContentSyncSource, (account, binding, "post", post["id"]))
                    if source is not None:
                        from .content_sync import mark_source_unavailable
                        mark_source_unavailable(s, runtime, source, now, "post_no_longer_paid")
        if advance:
            progress.last_error, sync.posts_error = error, error
        sync.posts_checked_at = now
        if error is None and advance:
            progress.history_marker = marker if more else None
            if not more:
                progress.history_complete, progress.history_checked_at = True, now
        progress.lease_until = utcnow() + timedelta(seconds=60)


def _revisit_posts(store, tenant, account, token, revision, binding, owner, transport, now, exclude=()):
    """Fair, bounded direct reads detect old edits and verified source deletion."""
    from db.onlyfans_content_sync_progress_models import OfContentSyncSource
    from .provider_read_budget import acquire, release, native_reader
    if not callable(getattr(transport, "post_by_id", None)):
        return
    with Session(store.engine) as s:
        wanted = s.scalars(select(OfContentSyncSource.source_id).where(
            OfContentSyncSource.account_id == account, OfContentSyncSource.binding_id == binding,
            OfContentSyncSource.source_kind == "post", OfContentSyncSource.current_observation_id.is_not(None),
            OfContentSyncSource.source_id.notin_(exclude),
            (OfContentSyncSource.last_checked_at.is_(None)) | (OfContentSyncSource.last_checked_at < now))
            .order_by(OfContentSyncSource.last_checked_at.asc().nulls_first(), OfContentSyncSource.source_id)
            .limit(DETAILS_PER_PASS)).all()
    for post_id in wanted:
        read_claim, retry_after = None, None
        try:
            read_claim = acquire(store, tenant, account, connection_revision=revision, kind="posts") if native_reader(transport) else None
            status, raw = transport.post_by_id(post_id)
            if status == 404:
                observed, absent, reason = [], [post_id], "post_deleted"
            elif status == 200:
                offer = parse_post(raw, owner_id=owner, post_id=post_id)
                observed, absent, reason = ([offer] if offer else []), ([] if offer else [post_id]), "post_no_longer_paid"
            else:
                raise NativeProtocolError("unsupported native post status")
            _commit_page(store, tenant, account, token, revision, binding, observed, None,
                marker=None, more=False, now=now, read_claim=read_claim, advance=False,
                unavailable_ids=absent, unavailable_reason=reason)
        except Exception as exc:
            retry_after = getattr(exc, "retry_after", None)
            raise
        finally:
            if read_claim is not None:
                release(store, tenant, read_claim, retry_after=retry_after)


from sqlalchemy import true


def process_posts(store, tenant, *, limit=1, stop=None, transport_factory=_transport, now=None, account_id=None):
    """Durable newest-page intake plus a fair bounded historical/revisit walk.

    Native historical reads require a recorded verified cursor contract. Known
    IDs never stop the walk: they may have changed since their last observation.
    """
    from db.models import OfCreatorBinding
    from db.onlyfans_content_sync_progress_models import OfContentPostProgress
    from .provider_connector import NativeProviderConnector, NativeRateLimitError
    from .provider_read_budget import acquire, current, release, ReadDeferred, native_reader
    import os
    if os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker":
        raise Refused("paid-post reading requires the worker role")
    if type(limit) is not int or not 1 <= limit <= 4:
        raise Refused("invalid paid-post work bound")
    now = now or utcnow()
    with Session(store.engine) as s:
        due = s.scalars(select(OfContentSyncAccount.account_id).join(OfCreatorBinding,
            OfCreatorBinding.id == OfContentSyncAccount.binding_id).where(OfContentSyncAccount.enabled.is_(True),
            OfCreatorBinding.tenant_id == tenant, OfCreatorBinding.revoked_at.is_(None),
            OfContentSyncAccount.status == "active",
                    (OfContentSyncAccount.account_id == account_id) if account_id is not None else true(),
            (OfContentSyncAccount.posts_checked_at.is_(None))
            | (OfContentSyncAccount.posts_checked_at <= now - timedelta(seconds=INTERVAL_SECONDS)))
            .order_by(OfContentSyncAccount.posts_checked_at.asc().nulls_first(), OfContentSyncAccount.account_id)
            .limit(limit)).all()
    done = 0
    for account in due:
        if stop is not None and stop.is_set():
            break
        claimed = _claim(store, tenant, account, now)
        if claimed is None:
            continue
        token, revision, owner, binding, continuation, contract, proof, connection = claimed
        try:
            transport = transport_factory(connection, tenant, account)
            # Injected synthetic readers exercise the continuation contract;
            # native sessions require account-bound evidence from live validation.
            supported = not native_reader(transport) or (
                contract == "beforePublishTime.v1" and proof is not None)
            marker, error = None, None
            # Keep one bounded continuation slot after the mandatory head even
            # at the minimum configured/tested budget. Otherwise a one-page
            # pass would revisit the head forever and never reach its cursor.
            for page_index in range(max(2, PAGES_PER_PASS)):
                if stop is not None and stop.is_set():
                    break
                read_claim, retry_after = None, None
                try:
                    read_claim = acquire(store, tenant, account, connection_revision=revision, kind="posts") if native_reader(transport) else None
                    raw = transport.posts_page(before_marker=marker)
                    observations, more, tail = parse_page(raw, owner_id=owner)
                    if read_claim is not None:
                        current(store, tenant, read_claim)
                    if more and (not tail or tail == marker):
                        raise NativeProtocolError("posts cursor did not advance")
                    # The head is always revisited for prompt edit/new-post intake.
                    # After it, continue the prior archive position rather than
                    # repeatedly re-reading only the first bounded block.
                    next_marker = continuation if page_index == 0 and continuation else tail
                    error = "post_cursor_contract_unverified" if more and not supported else None
                    _commit_page(store, tenant, account, token, revision, binding, observations, raw,
                        marker=next_marker, more=more, now=now, read_claim=read_claim, error=error)
                    if page_index == 0:
                        # Release the head page's shared lease before bounded
                        # detail reads; every detail keeps its own commit fence.
                        if read_claim is not None:
                            release(store, tenant, read_claim)
                            read_claim = None
                        _revisit_posts(store, tenant, account, token, revision, binding, owner, transport, now,
                            [row["id"] for row in load(raw, max_bytes=MAX_PAGE_BYTES)["list"]])
                    marker = next_marker
                    if not more or error:
                        break
                except ReadDeferred:
                    error = "shared_provider_read_budget"
                    break
                except NativeRateLimitError as exc:
                    error, retry_after = "provider_rate_limit", exc.retry_after
                    break
                except NativeProtocolError:
                    error = "unsupported_posts_response"
                    break
                except Refused:
                    error = "post_read_scope_changed"
                    break
                except Exception:
                    error = "posts_read_failed"
                    break
                finally:
                    if read_claim is not None:
                        release(store, tenant, read_claim, retry_after=retry_after)
            with Session(store.engine) as s, s.begin():
                runtime = store._runtime(s, tenant, account)
                progress = s.get(OfContentPostProgress, account, with_for_update=True)
                sync = s.get(OfContentSyncAccount, account)
                if progress is not None and progress.lease_token == token and runtime.binding_id == binding:
                    progress.lease_token = progress.lease_until = None
                    progress.last_error, sync.posts_error = error, error
                    sync.posts_checked_at = now
                    # A bounded unfinished archive is honest partial coverage.
                    if progress.history_marker is not None and not progress.history_complete and error is None:
                        progress.last_error = sync.posts_error = "historical_posts_in_progress"
            done += 1
        except Refused:
            continue
    return done


def coverage(row, enabled, now, progress=None):
    if not enabled or row is None:
        return {"state": "not_started", "checkedAt": None, "detail": "Automatic sync is off."}
    if row.posts_checked_at is None:
        return {"state": "not_started", "checkedAt": None, "detail": "Paid posts have not been read yet."}
    stamp = row.posts_checked_at.replace(tzinfo=None).isoformat() + "Z"
    if row.posts_error:
        detail = {"post_cursor_contract_unverified": "Only the newest page is checked; the historical cursor still requires provider verification.",
            "historical_posts_in_progress": "The newest page is checked; the historical paid-post walk is still running.",
            "shared_provider_read_budget": "Paid-post recovery is waiting for the shared provider read budget.",
            "provider_rate_limit": "Paid-post recovery is waiting for provider rate-limit backoff."}.get(
                row.posts_error, "The latest paid-post read stopped early.")
        return {"state": "partial", "checkedAt": stamp, "detail": detail}
    if progress is not None and progress.history_marker is not None:
        return {"state": "partial", "checkedAt": stamp,
            "detail": "The newest page is checked; bounded historical revisits are still running."}
    stale = now - row.posts_checked_at > timedelta(seconds=2 * INTERVAL_SECONDS)
    return {"state": "delayed" if stale else "current", "checkedAt": stamp,
        "detail": "The last paid-post read is more than ten minutes old." if stale else "Paid posts are up to date."}
