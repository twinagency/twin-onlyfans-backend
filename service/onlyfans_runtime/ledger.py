"""Bounded native ledger reads into the existing Transaction/commerce owners.

No legacy reader/executor is imported. Observations have no invented provider
revision. Exact identities are required for media linkage; amount/time matching
never grants ownership. The worker can reconcile while messaging is paused.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import os
import re
import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from db.models import ContentOffer, Fan, Message, OfCreatorBinding, Transaction
from db.onlyfans_models import OfRuntime
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_ledger_models import OfLedgerEvidence, OfLedgerScan
from transaction_contract import STATUS_MAP, classify_kind, strip_html
from .native_context import canonical
from .native_protocol import NativeProtocolError, cents, load
from .store import Refused, _id, utcnow

PAGE_SIZE = 20
MAX_OFFSET = 10000
PAID = frozenset({"pending", "cleared", "refund_pending"})
TERMINAL = frozenset({"refunded", "chargedback", "failed"})
STATUSES = PAID | TERMINAL


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
    """Validate a complete page before advancing the cursor or applying money."""
    _id(owner_id)
    value = load(raw)
    try:
        rows, more = value["list"], value["hasMore"]
        if (type(rows) is not list or len(rows) > PAGE_SIZE or type(more) is not bool
                or more and not rows):
            raise ValueError()
        result, seen = [], set()
        for row in rows:
            if type(row) is not dict:
                raise ValueError()
            provider = row.get("id")
            if type(provider) is int and provider > 0:
                provider = str(provider)
            if (type(provider) is not str or not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", provider)
                    or provider in seen):
                raise ValueError()
            seen.add(provider)
            if "ownerId" in row and _id(row["ownerId"]) != owner_id:
                raise ValueError()
            user = row.get("user")
            if user is not None and type(user) is not dict:
                raise ValueError()
            fan = _id(user["id"]) if user is not None else None
            if fan == owner_id:
                raise ValueError()
            description = row.get("description")
            if type(description) is not str or len(description.encode()) > 4000:
                raise ValueError()
            kind = classify_kind(description)
            if kind == "unknown":
                raise ValueError()
            status = row.get("status")
            if type(status) is not str:
                raise ValueError()
            status = STATUS_MAP.get(status, status)
            if status not in STATUSES or row.get("currency") != "USD":
                raise ValueError()
            amount = cents(row.get("amount"))
            occurred = timestamp(row.get("createdAt"))
            if not start <= occurred <= end or amount <= 0:
                raise ValueError()
            message = row.get("messageId")
            if message is not None:
                message = _id(message)
                if kind != "ppv_message" or fan is None:
                    raise ValueError()
            if kind == "ppv_message" and fan is None:
                raise ValueError()
            cleared = timestamp(row["clearedAt"]) if row.get("clearedAt") is not None else None
            if cleared is not None and cleared < occurred:
                raise ValueError()
            days = row.get("payoutPendingDays")
            if days is not None and (type(days) is not int or not 0 <= days <= 366):
                raise ValueError()
            result.append(dict(provider_transaction_id=provider, fan_id=fan, kind=kind,
                message_id=message, amount_cents=amount, currency="USD", status=status,
                occurred_at=occurred.isoformat(), cleared_at=cleared.isoformat() if cleared else None,
                payout_pending_days=days, description=strip_html(description),
                **{field + "_cents": cents(row[key]) if row.get(key) is not None else None
                   for field, key in [("net", "net"), ("fee", "fee"), ("vat", "vatAmount")]}))
        return result, more
    except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
        raise NativeProtocolError("unsupported native transaction page") from None


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


def apply_observation(s, account, observation, *, now):
    """Caller holds the runtime lock; all canonical effects are one transaction."""
    encoded = canonical(observation)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    if s.get(OfLedgerEvidence, (account, fingerprint)) is not None:
        return "duplicate"
    provider = observation["provider_transaction_id"]
    tx = s.scalar(select(Transaction).where(Transaction.account_id == account,
        Transaction.provider_transaction_id == provider).with_for_update())
    outcome = "applied"
    core = ("fan_id", "kind", "amount_cents", "currency")
    occurred = datetime.fromisoformat(observation["occurred_at"])
    if tx is not None:
        if (tx.source != "ledger" or any(getattr(tx, key) != observation[key] for key in core)
                or tx.occurred_at != occurred
                or tx.message_id is not None and observation["message_id"] not in {None, tx.message_id}):
            outcome = "conflict"
        else:
            outcome = _status(tx.status, observation["status"])
    message, offer = None, None
    mid = observation["message_id"] if observation["message_id"] is not None else tx.message_id if tx else None
    fan_id = observation["fan_id"]
    if mid is not None and outcome == "applied":
        message = s.get(Message, (account, fan_id, mid), with_for_update=True)
        other = s.scalar(select(Transaction.id).where(Transaction.account_id == account,
            Transaction.fan_id == fan_id, Transaction.message_id == mid,
            or_(Transaction.provider_transaction_id != provider,
                Transaction.provider_transaction_id.is_(None))).limit(1))
        offers = s.scalars(select(ContentOffer).where(ContentOffer.account_id == account,
            ContentOffer.fan_id == fan_id, ContentOffer.offer_message_id == mid).with_for_update()).all()
        if (message is None or message.direction != "out" or message.is_unsent
                or message.price_cents != observation["amount_cents"] or occurred < message.created_at
                or other is not None or len(offers) > 1
                or offers and offers[0].price_cents != observation["amount_cents"]):
            outcome = "conflict"
        else:
            offer = offers[0] if offers else None
    if outcome == "applied":
        was_paid = tx is not None and tx.status in PAID
        if tx is None:
            tx = Transaction(account_id=account, provider_transaction_id=provider,
                occurred_at=occurred, source="ledger", **{key: observation[key] for key in core})
            s.add(tx)
            s.flush()
        tx.status, tx.message_id, tx.raw_json = observation["status"], mid, encoded
        tx.cleared_at = datetime.fromisoformat(observation["cleared_at"]) if observation["cleared_at"] else None
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
    s.add(OfLedgerEvidence(account_id=account, evidence_hash=fingerprint,
        provider_transaction_id=provider, fan_id=fan_id, transaction_id=tx.id if tx else None,
        outcome=outcome, observation_json=encoded, recorded_at=now))
    s.flush()
    return outcome


def _current(runtime, connection, scan, now):
    return (connection is not None and connection.binding_id == runtime.binding_id
        and connection.status in {"verified", "paused"} and connection.verified_at is not None
        and connection.expires_at > now and (scan is None or
            (scan.binding_id, scan.connection_revision, scan.owner_id) ==
            (runtime.binding_id, connection.revision, connection.owner_id)))


def process_ledger_pages(store, tenant, *, limit=1, stop=None):
    """At most four leased GET pages, independent of outgoing activation."""
    from onlyfans_read.contract import ID_PATTERN
    if (os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker" or type(tenant) is not str
            or tenant == "deployment" or not ID_PATTERN.fullmatch(tenant)
            or type(limit) is not int or not 1 <= limit <= 4):
        raise Refused("invalid ledger worker scope or bound")
    processed = 0
    for _ in range(limit):
        if stop is not None and stop.is_set():
            break
        now = utcnow()
        with Session(store.engine) as s:
            candidate = s.scalar(select(OfProviderConnection.account_id).join(OfRuntime,
                OfRuntime.account_id == OfProviderConnection.account_id).join(OfCreatorBinding,
                OfCreatorBinding.id == OfRuntime.binding_id).outerjoin(OfLedgerScan,
                OfLedgerScan.account_id == OfRuntime.account_id).where(
                OfCreatorBinding.tenant_id == tenant, OfCreatorBinding.channel == "onlyfans",
                OfCreatorBinding.revoked_at.is_(None), OfProviderConnection.binding_id == OfRuntime.binding_id,
                OfProviderConnection.status.in_(("verified", "paused")), OfProviderConnection.verified_at.is_not(None),
                OfProviderConnection.expires_at > now,
                or_(OfLedgerScan.account_id.is_(None), and_(OfLedgerScan.next_poll_at <= now,
                    or_(OfLedgerScan.lease_until.is_(None), OfLedgerScan.lease_until <= now))))
                .order_by(OfLedgerScan.next_poll_at.asc().nullsfirst(), OfRuntime.account_id).limit(1))
        if candidate is None:
            break
        account = candidate
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account)
            connection, scan = s.get(OfProviderConnection, account), s.get(OfLedgerScan, account)
            now = utcnow()
            if not _current(runtime, connection, None, now):
                continue
            if scan is not None and (scan.next_poll_at > now or scan.lease_until and scan.lease_until > now):
                continue
            if scan is None:
                scan = OfLedgerScan(account_id=account, last_complete_at=None)
                s.add(scan)
            if scan.status != "running" or not _current(runtime, connection, scan, now):
                scan.binding_id, scan.connection_revision, scan.owner_id = runtime.binding_id, connection.revision, connection.owner_id
                # Revisit the full supported history daily: a refund can arrive
                # long after a sale leaves the frequent seven-day window.
                days = 90 if scan.last_full_at is None or now - scan.last_full_at >= timedelta(days=1) else 7
                scan.window_start = now - timedelta(days=days)
                scan.window_end, scan.page_offset, scan.last_page_hash = now, 0, None
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
            start, end, offset, owner, expiry = scan.window_start, scan.window_end, scan.page_offset, scan.owner_id, connection.expires_at
        if stop is not None and stop.is_set():
            break
        error = None
        try:
            from .provider_connector import NativeProviderConnector, session_from_capture
            transport = NativeProviderConnector(session_from_capture(account_id=account, owner_id=owner,
                policy_revision=1, capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"],
                expires_at=expiry.replace(tzinfo=timezone.utc)))
            raw = transport.transaction_page(offset=offset, limit=PAGE_SIZE, start=start, end=end)
            rows, more = parse_page(raw, owner_id=owner, start=start, end=end)
        except NativeProtocolError:
            error = "unsupported_ledger_response"
        except Exception:
            error = "ledger_read_failed"
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account)
            scan = s.get(OfLedgerScan, account)
            if scan.lease_token != token or scan.status != "running":
                continue
            now = utcnow()
            if not _current(runtime, s.get(OfProviderConnection, account), scan, now):
                error = "connection_changed"
            page_hash = hashlib.sha256(raw).hexdigest() if error is None else None
            if error is None and more and (page_hash == scan.last_page_hash or offset + len(rows) > MAX_OFFSET):
                error = "unsupported_ledger_pagination"
            scan.lease_token = scan.lease_until = None
            scan.updated_at = now
            if error:
                scan.status, scan.last_error, scan.next_poll_at = "held", error, now + timedelta(minutes=5)
                continue
            for row in rows:
                apply_observation(s, account, row, now=now)
            scan.page_offset, scan.last_page_hash = offset + len(rows), page_hash
            scan.next_poll_at = now + timedelta(seconds=1 if more else 300)
            if not more:
                # Freshness describes the frozen provider query cutoff, not
                # the later wall-clock time at which a long scan finished.
                scan.status, scan.last_complete_at = "idle", end
                if end - start == timedelta(days=90):
                    scan.last_full_at = end
                if now - end > timedelta(minutes=5):
                    scan.next_poll_at = now + timedelta(seconds=1)
            processed += 1
    return processed
