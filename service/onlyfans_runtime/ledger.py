"""Bounded native ledger reads into the existing Transaction/commerce owners.

No legacy reader/executor is imported. Observations have no invented provider
revision. Exact identities are required for media linkage; amount/time matching
never grants ownership. The worker can reconcile while messaging is paused.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from db.models import ContentOffer, Fan, Message, OfCreatorBinding, Transaction
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_ledger_models import OfLedgerEvidence, OfLedgerScan, OfLedgerHistory
from transaction_contract import STATUS_MAP, PAID, TERMINAL, STATUSES, classify_kind, strip_html, transaction_marker
from .native_context import canonical
from .native_protocol import MAX_BYTES, NativeProtocolError, cents, load
from .launch_ledger.validation import LedgerValidationError
from .store import Refused, _id, utcnow

PAGE_SIZE = 20
MAX_OFFSET = 10000
MAX_MARKER_ROWS = 1000000000
# Recent coverage is refreshed on provider time, not worker completion time.
# A normal page gets one provider lease of headroom before the old cutoff expires.
RECENT_REFRESH_INTERVAL = timedelta(seconds=300)
RECENT_READ_ALLOWANCE = timedelta(seconds=45)
RECENT_COVERAGE_FRESHNESS = RECENT_REFRESH_INTERVAL + RECENT_READ_ALLOWANCE
RECENT_OVERLAP = RECENT_REFRESH_INTERVAL
MAX_RECENT_WINDOW = timedelta(days=90)


def recent_refresh_at(completed_at, cutoff_at):
    """Start the next refresh early enough for this scan's observed duration.

    The provider query cutoff is the truth. If a scan took most of its refresh
    interval, the next request is already due and starts on the next worker
    tick rather than another 300 seconds after completion.
    """
    if (type(completed_at) is not datetime or type(cutoff_at) is not datetime
            or completed_at.tzinfo is not None or cutoff_at.tzinfo is not None
            or completed_at < cutoff_at):
        raise Refused("invalid ledger refresh timing")
    observed_duration = completed_at - cutoff_at
    target = cutoff_at + RECENT_REFRESH_INTERVAL - observed_duration
    return max(completed_at + timedelta(seconds=1), target)


def recent_window_start(now, last_complete_at):
    """Start a recent refresh from the last proven cutoff, not seven days again.

    The full-history lane remains responsible for old transactions and late
    refunds. Re-reading the whole seven-day window on every recent refresh made
    pagination consume the freshness window under normal provider latency.
    """
    if type(now) is not datetime or now.tzinfo is not None:
        raise Refused("invalid ledger refresh timing")
    floor = now - MAX_RECENT_WINDOW
    if (type(last_complete_at) is not datetime or last_complete_at.tzinfo is not None
            or last_complete_at > now):
        return max(floor, now - timedelta(days=7))
    return max(floor, last_complete_at - RECENT_OVERLAP)


def deferred_reason_code(reason):
    """Stable public cause for a provider-read deferral; never provider text."""
    if str(reason or "").startswith("provider read lease occupied"):
        return "lease-occupied"
    return {
        "provider read lease occupied": "lease-occupied",
        "provider read backoff": "provider-backoff",
        "provider read budget exhausted": "budget-exhausted",
        "provider ledger turn due": "ledger-turn-due",
    }.get(str(reason or ""), "deferred")


def timestamp(value):
    if type(value) is not str or len(value) > 64:
        raise NativeProtocolError("explicit transaction timestamp required")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise NativeProtocolError("invalid transaction timestamp") from None


def parse_page(raw, *, owner_id, start, end):
    rows, more, _, _ = _provider_page(raw, owner_id=owner_id, start=start, end=end)
    return rows, more


def _provider_page(raw, *, owner_id, start, end):
    """Validate a complete page; diagnostics expose fixed codes, never values."""
    _id(owner_id)
    if (type(start) is not datetime or type(end) is not datetime
            or start.tzinfo is not None or end.tzinfo is not None or start > end):
        raise Refused("invalid ledger validation window")
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise LedgerValidationError("invalid_response_size", "$")
    try:
        value = load(raw)
    except NativeProtocolError:
        raise LedgerValidationError("invalid_json", "$") from None
    rows, more = value.get("list"), value.get("hasMore")
    if type(rows) is not list or len(rows) > PAGE_SIZE:
        raise LedgerValidationError("invalid_page_list", "list")
    if type(more) is not bool:
        raise LedgerValidationError("invalid_has_more", "hasMore")
    if more and not rows:
        raise LedgerValidationError("empty_nonterminal_page", "list")
    marker = None
    if more:
        try:
            marker = transaction_marker(value.get("nextMarker"))
        except (ValueError, UnicodeError):
            raise LedgerValidationError("invalid_next_marker", "nextMarker") from None
    result, seen = [], set()
    for index, row in enumerate(rows):
        def require(condition, code, path):
            if not condition:
                raise LedgerValidationError(code, path, index)

        def checked(convert, value, code, path):
            try:
                return convert(value)
            except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
                raise LedgerValidationError(code, path, index) from None

        require(type(row) is dict, "invalid_row", "list")
        provider = row.get("id")
        if type(provider) is int and provider > 0:
            # Bound before conversion, including Python's integer string limit.
            require(provider < 10**64, "invalid_provider_id", "id")
            provider = str(provider)
        require(type(provider) is str and re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", provider),
                "invalid_provider_id", "id")
        require(provider not in seen, "duplicate_provider_id", "id")
        seen.add(provider)
        if "ownerId" in row:
            owner = checked(_id, row["ownerId"], "invalid_owner_id", "ownerId")
            require(owner == owner_id, "owner_mismatch", "ownerId")
        user = row.get("user")
        require(user is None or type(user) is dict, "invalid_user", "user")
        fan = checked(_id, user.get("id"), "invalid_fan_id", "user.id") if user is not None else None
        require(fan != owner_id, "fan_is_owner", "user.id")
        description = row.get("description")
        require(type(description) is str, "invalid_description", "description")
        encoded_description = checked(lambda v: v.encode(), description, "invalid_description", "description")
        require(len(encoded_description) <= 4000, "invalid_description", "description")
        kind = classify_kind(description)
        require(kind != "unknown", "unknown_kind", "description")
        status = row.get("status")
        require(type(status) is str, "invalid_status", "status")
        status = STATUS_MAP.get(status, status)
        require(status in STATUSES, "invalid_status", "status")
        require(row.get("currency") == "USD", "invalid_currency", "currency")
        amount = checked(cents, row.get("amount"), "invalid_amount", "amount")
        require(amount > 0, "nonpositive_amount", "amount")
        occurred = checked(timestamp, row.get("createdAt"), "invalid_created_at", "createdAt")
        message = row.get("messageId")
        if message is not None:
            message = checked(_id, message, "invalid_message_id", "messageId")
            require(kind == "ppv_message" and fan is not None, "unexpected_message_id", "messageId")
        require(kind != "ppv_message" or fan is not None, "missing_fan", "user.id")
        cleared = checked(timestamp, row["clearedAt"], "invalid_cleared_at", "clearedAt") if row.get("clearedAt") is not None else None
        require(cleared is None or cleared >= occurred, "cleared_before_created", "clearedAt")
        days = row.get("payoutPendingDays")
        require(days is None or type(days) is int and 0 <= days <= 366, "invalid_pending_days", "payoutPendingDays")
        optional = {field + "_cents": checked(cents, row[key], "invalid_" + field, key) if row.get(key) is not None else None
                    for field, key in [("net", "net"), ("fee", "fee"), ("vat", "vatAmount")]}
        # The provider's descending page can include an older tail despite the
        # requested start bound. Validate every field even in that tail; only
        # in-window observations may reach the transaction/ownership writer.
        if start <= occurred <= end:
            result.append(dict(provider_transaction_id=provider, fan_id=fan, kind=kind,
                message_id=message, amount_cents=amount, currency="USD", status=status,
                occurred_at=occurred.isoformat(), cleared_at=cleared.isoformat() if cleared else None,
                payout_pending_days=days, description=strip_html(description), **optional))
    # Date parameters are not a verified ordering/termination contract. Validate
    # every row, filter locally, and follow the provider's continuation to its end.
    return result, more, marker, sorted(seen)


def _generation(scan):
    return hashlib.sha256(canonical([scan.account_id, scan.binding_id,
        scan.connection_revision, scan.owner_id]).encode()).hexdigest()


def _cursor_scope(scan):
    return hashlib.sha256(canonical(["transaction-date-filters-v2", _generation(scan), scan.window_start, scan.window_end]).encode()).hexdigest()


def _new_cursor(scan):
    return dict(version=1, scope=_cursor_scope(scan), marker=None, anchor=None, power=1, distance=0)


def _cursor(scan):
    try:
        if not scan.continuation_json or len(scan.continuation_json.encode()) > 8192:
            return None
        value = load(scan.continuation_json.encode())
        version = value.get("version")
        keys = {"version", "scope", "marker", "anchor", "power", "distance"}
        if version == 2:
            keys.add("rows")
        if (set(value) != keys
                or type(version) is not int or version not in (1, 2)
                or value["scope"] != _cursor_scope(scan)
                or type(value["power"]) is not int or not 1 <= value["power"] <= 2**31
                or type(value["distance"]) is not int or not 0 <= value["distance"] < value["power"]):
            return None
        for key in ("marker", "anchor"):
            if value[key] is not None and transaction_marker(value[key]) != value[key]:
                return None
        if version == 2 and (type(value["rows"]) is not int
                or not MAX_OFFSET < value["rows"] <= MAX_MARKER_ROWS
                or scan.page_offset != MAX_OFFSET):
            return None
        return value
    except (NativeProtocolError, ValueError, TypeError, UnicodeError):
        return None


def scan_position(scan):
    """Actual marker traversal count; the legacy offset column stays in range."""
    cursor = _cursor(scan)
    return cursor.get("rows", scan.page_offset) if cursor else scan.page_offset


def _advance_cursor(cursor, marker):
    """Brent cycle detection: constant durable space, no extra provider reads."""
    result = dict(cursor)
    if marker is not None:
        if marker in {cursor["marker"], cursor["anchor"]}:
            raise NativeProtocolError("unsupported ledger pagination")
        if cursor["anchor"] is None:
            result.update(anchor=marker, distance=0)
        else:
            result["distance"] += 1
            if result["distance"] == result["power"]:
                result.update(anchor=marker, distance=0, power=result["power"] * 2)
    result["marker"] = marker
    return result


def _coverage_receipt(scan):
    return canonical(dict(version=1, generation=_generation(scan),
        recent=scan.last_complete_at.isoformat() if scan.last_complete_at else None,
        full=scan.last_full_at.isoformat() if scan.last_full_at else None))


def coverage_proven(scan):
    try:
        if scan is None or not scan.last_complete_at or not scan.coverage_json or len(scan.coverage_json.encode()) > 2048:
            return False
        value = load(scan.coverage_json.encode())
        return type(value.get("version")) is int and value == load(_coverage_receipt(scan).encode())
    except (NativeProtocolError, ValueError, TypeError, UnicodeError):
        return False


def _status(previous, incoming):
    if previous == incoming:
        return "applied"
    # A stale GET may arrive after a terminal observation. Never resurrect it.
    if previous in TERMINAL:
        return "stale" if incoming in PAID else "conflict"
    if previous == "refund_pending" and incoming in {"pending", "cleared"}:
        return "stale"
    if previous == "cleared" and incoming == "pending":
        return "stale"
    return "applied" if previous in PAID and incoming in STATUSES else "conflict"


def _project_receipt(s, account, observation, *, now, backfill=False):
    """Project validated evidence under the caller's runtime lock and audit owner."""
    encoded = canonical(observation)
    provider = observation["provider_transaction_id"]
    tx = s.scalar(select(Transaction).where(Transaction.account_id == account,
        Transaction.provider_transaction_id == provider).with_for_update())
    outcome = "applied"
    core = ("fan_id", "kind", "amount_cents", "currency")
    occurred = datetime.fromisoformat(observation["occurred_at"])
    prior_mid = tx.message_id if tx is not None else None
    if tx is not None:
        # Retain the provider's intended message even before history can prove
        # its ownership link. A later observation cannot change that identity.
        try:
            retained_mid = json.loads(tx.raw_json or "{}").get("message_id")
        except (ValueError, AttributeError):
            retained_mid = None
            outcome = "conflict"
        if retained_mid is not None:
            if prior_mid is not None and prior_mid != retained_mid:
                outcome = "conflict"
            prior_mid = retained_mid
        if (tx.source != "ledger" or any(getattr(tx, key) != observation[key] for key in core)
                or tx.occurred_at != occurred
                or prior_mid is not None and observation["message_id"] not in {None, prior_mid}):
            outcome = "conflict"
        elif outcome != "conflict":
            outcome = _status(tx.status, observation["status"])
    financial_outcome = outcome
    message, offer = None, None
    mid = observation["message_id"] if observation["message_id"] is not None else prior_mid
    fan_id = observation["fan_id"]
    if mid is not None and outcome == "applied":
        message = s.get(Message, (account, fan_id, mid), with_for_update=True)
        other = s.scalar(select(Transaction.id).where(Transaction.account_id == account,
            Transaction.fan_id == fan_id, Transaction.message_id == mid,
            or_(Transaction.provider_transaction_id != provider,
                Transaction.provider_transaction_id.is_(None))).limit(1))
        offers = s.scalars(select(ContentOffer).where(ContentOffer.account_id == account,
            ContentOffer.fan_id == fan_id, ContentOffer.offer_message_id == mid).with_for_update()).all()
        # A terminal update to an already bound transaction revokes that exact
        # purchase. Later unsends or price edits cannot veto its reversal.
        # Immutable transaction identity was checked above; this never grants
        # a new ownership link or permits a changed fan/amount/message.
        bound_reversal = (tx is not None and tx.message_id == mid
            and observation["status"] in TERMINAL)
        if (message is None or other is not None or len(offers) > 1
                or not bound_reversal and (message.direction != "out" or message.is_unsent
                    or message.price_cents != observation["amount_cents"] or occurred < message.created_at
                    or offers and offers[0].price_cents != observation["amount_cents"])):
            message = None
        else:
            offer = offers[0] if offers else None
    if financial_outcome == "applied":
        # Verified money is independent of imported message availability. Keep
        # unresolved linkage in immutable evidence and retain the commerce hold;
        # only exact canonical history may project media ownership.
        linked_mid = mid if message is not None else tx.message_id if tx is not None else None
        was_paid = tx is not None and tx.status in PAID
        moved = tx is None or tx.status != observation["status"] or tx.message_id != linked_mid
        if tx is None:
            tx = Transaction(account_id=account, provider_transaction_id=provider,
                occurred_at=occurred, source="ledger", **{key: observation[key] for key in core})
            s.add(tx)
            s.flush()
        tx.status, tx.message_id = observation["status"], linked_mid
        tx.raw_json = canonical({**observation, "message_id": mid})
        if observation["cleared_at"] is not None:
            tx.cleared_at = datetime.fromisoformat(observation["cleared_at"])
        for field in ("net_cents", "fee_cents", "vat_cents", "payout_pending_days", "description"):
            # A later observation missing optional fields cannot erase evidence.
            if observation[field] is not None:
                setattr(tx, field, observation[field])
        paid = tx.status in PAID
        if fan_id is not None:
            fan = s.get(Fan, (account, fan_id), with_for_update=True)
            if fan is None:
                fan = Fan(account_id=account, fan_id=fan_id, lifetime_spend_cents=0)
                s.add(fan)
                s.flush()
            if paid != was_paid:
                fan.lifetime_spend_cents = max(0, fan.lifetime_spend_cents + (tx.amount_cents if paid else -tx.amount_cents))
        if message is not None:
            from .commerce import settle_media_purchase
            settle_media_purchase(s, account_id=account, fan_id=fan_id, message=message,
                offer=offer, paid=paid, was_paid=was_paid, stamp=occurred, now=now)
        if moved and fan_id is not None and not (backfill and occurred < now - timedelta(minutes=5)):
            s.flush()
            from .launch_engagement.events import cancel_for_fan
            cancel_for_fan(s, account, fan_id, "purchase", now=now)
    s.flush()
    return tx, financial_outcome


def apply_observation(s, account, observation, *, now, backfill=False):
    """Caller holds the runtime lock; all canonical effects are one transaction."""
    encoded = canonical(observation)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    if s.get(OfLedgerEvidence, (account, fingerprint)) is not None:
        return "duplicate"
    tx, outcome = _project_receipt(s, account, observation, now=now, backfill=backfill)
    s.add(OfLedgerEvidence(account_id=account, evidence_hash=fingerprint,
        provider_transaction_id=observation["provider_transaction_id"], fan_id=observation["fan_id"], transaction_id=tx.id if tx else None,
        outcome=outcome, observation_json=encoded, recorded_at=now))
    s.flush()
    return outcome


def _current(runtime, connection, scan, now):
    return (connection is not None and connection.binding_id == runtime.binding_id
        and connection.status in {"verified", "paused"} and connection.verified_at is not None
        and connection.expires_at > now and (scan is None or
            (scan.binding_id, scan.connection_revision, scan.owner_id) ==
            (runtime.binding_id, connection.revision, connection.owner_id)))


def process_ledger_pages(store, tenant, *, limit=1, stop=None, history=False):
    """At most four leased GET pages, independent of outgoing activation."""
    from onlyfans_read.contract import ID_PATTERN
    if (os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker" or type(tenant) is not str
            or tenant == "deployment" or not ID_PATTERN.fullmatch(tenant)
            or type(limit) is not int or not 1 <= limit <= 4 or type(history) is not bool):
        raise Refused("invalid ledger worker scope or bound")
    Scan = OfLedgerHistory if history else OfLedgerScan
    processed = 0
    for _ in range(limit):
        if stop is not None and stop.is_set():
            break
        now = utcnow()
        with Session(store.engine) as s:
            candidate = s.scalar(select(OfProviderConnection.account_id).join(OfRuntime,
                OfRuntime.account_id == OfProviderConnection.account_id).join(OfCreatorBinding,
                OfCreatorBinding.id == OfRuntime.binding_id).outerjoin(Scan,
                Scan.account_id == OfRuntime.account_id).where(
                OfCreatorBinding.tenant_id == tenant, OfCreatorBinding.channel == "onlyfans",
                OfCreatorBinding.revoked_at.is_(None), OfProviderConnection.binding_id == OfRuntime.binding_id,
                OfProviderConnection.status.in_(("verified", "paused")), OfProviderConnection.verified_at.is_not(None),
                OfProviderConnection.expires_at > now,
                or_(Scan.account_id.is_(None), Scan.binding_id != OfRuntime.binding_id,
                    Scan.connection_revision != OfProviderConnection.revision,
                    Scan.owner_id != OfProviderConnection.owner_id, and_(Scan.next_poll_at <= now,
                    or_(Scan.lease_until.is_(None), Scan.lease_until <= now))))
                .order_by(Scan.next_poll_at.asc().nullsfirst(), OfRuntime.account_id).limit(1))
        if candidate is None:
            break
        account = candidate
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account)
            connection, scan = s.get(OfProviderConnection, account), s.get(Scan, account)
            now = utcnow()
            if not _current(runtime, connection, None, now):
                continue
            if (scan is not None and _current(runtime, connection, scan, now)
                    and (scan.next_poll_at > now or scan.lease_until and scan.lease_until > now)):
                continue
            if scan is None:
                scan = Scan(account_id=account, last_complete_at=None)
                s.add(scan)
            if (not history and scan.status == "running" and scan.window_end is not None
                    and now - scan.window_end >= RECENT_COVERAGE_FRESHNESS):
                # A partial page that outlived the freshness contract can never
                # certify current purchases, even if it eventually finishes.
                # Start a new provider window rather than extending the stale one.
                scan.status = "idle"
                scan.last_error = None
                scan.lease_token = scan.lease_until = None
                scan.next_poll_at = now
                scan.page_offset = 0
                scan.last_page_hash = None
                scan.continuation_json = None
            if not _current(runtime, connection, scan, now):
                scan.last_complete_at = scan.last_full_at = None
                scan.coverage_json = None
            elif scan.last_complete_at and not coverage_proven(scan):
                scan.last_complete_at = scan.last_full_at = scan.coverage_json = None
            if scan.status not in {"running", "held"} or not _current(runtime, connection, scan, now):
                scan.binding_id, scan.connection_revision, scan.owner_id = runtime.binding_id, connection.revision, connection.owner_id
                # Recent reads extend the last proven cutoff with a bounded
                # overlap. The independent lifetime lane below remains the
                # authority for older transactions and late refunds.
                scan.window_start = (datetime(1970, 1, 1) if history
                                     else recent_window_start(now, scan.last_complete_at))
                scan.window_end, scan.page_offset, scan.last_page_hash = now, 0, None
                scan.continuation_json = None
            cursor = _cursor(scan)
            if cursor is None:
                scan.page_offset, scan.last_page_hash = 0, None
                cursor = _new_cursor(scan)
                scan.continuation_json = canonical(cursor)
            from .connections import _open
            try:
                clear = _open(connection, tenant)
            except Refused:
                scan.status, scan.last_error = "held", "connection_unavailable"
                scan.lease_token = scan.lease_until = None
                scan.next_poll_at, scan.updated_at = now + timedelta(minutes=5), now
                continue
            token = str(uuid.uuid4())
            scan.status, scan.lease_token, scan.last_error = "running", token, None
            scan.lease_until, scan.next_poll_at, scan.updated_at = now + timedelta(seconds=60), now, now
            start, end, offset, owner, expiry = scan.window_start, scan.window_end, cursor.get("rows", scan.page_offset), scan.owner_id, connection.expires_at
            connection_revision=connection.revision
        if stop is not None and stop.is_set():
            break
        error = budget = failure = deferred = None
        try:
            from .provider_connector import NativeProviderConnector, session_from_capture
            transport = NativeProviderConnector(session_from_capture(account_id=account, owner_id=owner,
                policy_revision=1, capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"],
                expires_at=expiry.replace(tzinfo=timezone.utc)))
            from .worker import _provider_claim
            budget=_provider_claim(store,tenant,account,transport,connection_revision,'ledger')
            raw = transport.transaction_page(offset=offset, limit=PAGE_SIZE, start=start, end=end,
                **({"history": True} if history else {}), **({"marker": cursor["marker"]} if cursor["marker"] is not None else {}))
            rows, more, next_marker, identities = _provider_page(raw, owner_id=owner, start=start, end=end)
        except Exception as problem:
            from .provider_read_budget import ReadDeferred
            failure=problem
            if isinstance(problem,ReadDeferred):
                deferred=problem.retry_at
                error=('provider_read_deferred' if history else
                       f"provider_read_deferred:{deferred_reason_code(str(problem))}")
            elif isinstance(problem,NativeProtocolError):error='provider_rate_limited' if hasattr(problem,'retry_after') else 'unsupported_ledger_response'
            else:error='ledger_read_failed'
        try:
            with Session(store.engine) as s, s.begin():
                runtime = store._runtime(s, tenant, account)
                scan = s.get(Scan, account)
                if scan.lease_token != token or scan.status != "running":
                    continue
                now = utcnow()
                if not _current(runtime, s.get(OfProviderConnection, account), scan, now):
                    error = "connection_changed"
                if budget is not None:
                    from .provider_read_budget import current
                    try:current(store,tenant,budget,session=s)
                    except Refused:error=error or 'provider_read_stale'
                # Detect repeated provider identities regardless of JSON formatting,
                # row order, optional metadata or a changed terminal-page flag.
                page_hash = hashlib.sha256(canonical(identities).encode()).hexdigest() if error is None else None
                legacy_hash = hashlib.sha256(raw).hexdigest() if error is None else None
                if error is None and (offset + len(identities) > (MAX_MARKER_ROWS if history or cursor["marker"] is not None else MAX_OFFSET)
                        or identities and scan.last_page_hash in {page_hash, legacy_hash}):
                    error = "unsupported_ledger_pagination"
                if error is None:
                    try:
                        next_cursor = _advance_cursor(cursor, next_marker)
                    except NativeProtocolError:
                        error = "unsupported_ledger_pagination"
                scan.lease_token = scan.lease_until = None
                scan.updated_at = now
                if error:
                    scan.status, scan.last_error, scan.next_poll_at = ("running" if deferred else "held"), error, deferred or now + timedelta(minutes=5)
                    continue
                for row in rows:
                    apply_observation(s, account, row, now=now, backfill=history)
                position = offset + len(identities)
                # The older recent-scan schema bounds its offset at 10,000.
                # Marker traversal has no offset dependency. Keep its complete
                # count in the versioned cursor without changing that schema.
                scan.page_offset = position if history else min(position, MAX_OFFSET)
                if not history and position > MAX_OFFSET:
                    next_cursor.update(version=2, rows=position)
                scan.last_page_hash = page_hash
                scan.continuation_json = canonical(next_cursor)
                scan.next_poll_at = (now + timedelta(seconds=1) if more
                                     else recent_refresh_at(now, end))
                if not more:
                    # Freshness describes the frozen provider query cutoff, not
                    # the later wall-clock time at which a long scan finished.
                    scan.status, scan.last_complete_at = "idle", end
                    if history:
                        scan.last_full_at = end
                    scan.coverage_json = _coverage_receipt(scan)
                    if history:
                        scan.next_poll_at = now + timedelta(days=1)
                        recent = s.get(OfLedgerScan, account)
                        if (recent is not None
                                and (recent.binding_id, recent.connection_revision, recent.owner_id)
                                    == (scan.binding_id, scan.connection_revision, scan.owner_id)):
                            recent.last_full_at = end
                            recent.coverage_json = _coverage_receipt(recent)
                processed += 1
        finally:
            from .worker import _release_provider
            _release_provider(store,tenant,budget,failure)
    return processed
