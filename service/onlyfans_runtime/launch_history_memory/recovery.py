"""Independent, resumable archive reads over canonical message/ingress storage.

Only an explicitly injected account-bound reader can perform external work.
No schema is registered with the shared Base, and no startup DDL runs here.
Tables ship in migration 0090_launch.
Archive completeness deliberately never clears or sets runtime.recovery_needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import uuid

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime,
                        ForeignKey, Index, Integer, String, and_, func, or_, select,
                        text)
from sqlalchemy.orm import Session

from db.models import Base, Fan, Message, OfCreatorBinding
from db.onlyfans_models import OfIngress, OfRuntime, OfWork
from onlyfans_runtime.store import Event, LEASE_SECONDS, MAX_PENDING, PAGE_LIMIT, Refused, _id, utcnow

RecoveryBase = Base  # shared registry (see models.MemoryBase)
INFO = {"onlyfans_since": "0090_launch"}
MAX_SCAN_PAGES = 10_000
RETRY_SECONDS = 5
LIVE_RESERVE = PAGE_LIMIT
ERROR_CODES = frozenset({"provider_read_failed", "invalid_page", "backpressure", "live_input_pending",
                         "conflicting_history", "discovery_budget_exhausted", "provider_read_deferred"})


class OfHistoryScan(RecoveryBase):
    __tablename__ = "of_launch_history_scan"
    __table_args__ = (CheckConstraint("status IN ('needed','running','complete')", name="status"),
                      {"info": INFO})
    account_id = Column(String, ForeignKey(OfRuntime.account_id, ondelete="RESTRICT"), primary_key=True)
    binding_id = Column(String, ForeignKey(OfCreatorBinding.id, ondelete="RESTRICT"), nullable=False)
    status = Column(String(16), nullable=False, server_default=text("'needed'"))
    generation = Column(Integer, nullable=False, server_default=text("0"))
    cursor = Column(String(1024))
    page_count = Column(Integer, nullable=False, server_default=text("0"))
    known_fan_after = Column(BigInteger, nullable=False, server_default=text("0"))
    known_fans_complete = Column(Boolean, nullable=False, server_default=text("false"))
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
    retry_at = Column(DateTime)
    last_error = Column(String(40))
    completed_at = Column(DateTime)
    updated_at = Column(DateTime, nullable=False)


class OfHistoryCursor(RecoveryBase):
    __tablename__ = "of_launch_history_cursor"
    __table_args__ = (
        CheckConstraint("status IN ('needed','running','complete')", name="status"),
        CheckConstraint("pages_completed >= 0 AND messages_observed >= 0 AND messages_added >= 0",
                        name="counts"),
        Index("ix_launch_history_cursor_due", "account_id", "status", "retry_at", "generation", "fan_id"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey(OfRuntime.account_id, ondelete="RESTRICT"), primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    binding_id = Column(String, ForeignKey(OfCreatorBinding.id, ondelete="RESTRICT"), nullable=False)
    status = Column(String(16), nullable=False, server_default=text("'needed'"))
    generation = Column(Integer, nullable=False, server_default=text("0"))
    # The first request may be handed an already-committed recent-page boundary.
    # Keeping it distinct from upper_bound makes partial coverage explicit.
    start_before_id = Column(BigInteger)
    upper_bound = Column(BigInteger)
    before_id = Column(BigInteger)
    pages_completed = Column(Integer, nullable=False, server_default=text("0"))
    messages_observed = Column(BigInteger, nullable=False, server_default=text("0"))
    messages_added = Column(BigInteger, nullable=False, server_default=text("0"))
    oldest_message_at = Column(DateTime)
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
    retry_at = Column(DateTime)
    last_error = Column(String(40))
    completed_at = Column(DateTime)
    updated_at = Column(DateTime, nullable=False)


@dataclass(frozen=True)
class ArchiveClaim:
    account_id: str
    binding_id: str
    fan_id: int
    generation: int
    token: str
    before_id: int | None
    connection_revision: int | None = None


@dataclass(frozen=True)
class ArchiveScanClaim:
    account_id: str
    binding_id: str
    generation: int
    token: str
    cursor: str | None
    connection_revision: int | None = None


def affects_recent_context(session, account_id, fan_id, event):
    """Integration predicate evaluated BEFORE a new canonical Message is added.

    A strictly older insertion cannot change the latest-100 message window.
    Historical-memory publication must have its own planner snapshot revision.
    This does not patch Store._apply or weaken any send authorization fence.
    """
    event = event.normalized()
    if event.fan_id != fan_id:
        raise Refused("historical context scope mismatch")
    boundary = session.execute(select(Message.created_at, Message.message_id).where(
        Message.account_id == account_id, Message.fan_id == fan_id, Message.is_unsent.is_(False)).order_by(
            Message.created_at.desc(), Message.message_id.desc()).offset(99).limit(1)).first()
    stamp = datetime.fromisoformat(event.created_at).replace(tzinfo=None)
    return boundary is None or (stamp, event.message_id) > tuple(boundary)


class HistoryRecovery:
    """Short transactions; at most one discovery and one archive page per step.

    All database paths use the existing creator/agency/binding access checks.
    Active incoming work wins over archive writes. Until Claude integrates the
    recent-context revision predicate, this prevents invalidating a live claim.
    """
    def __init__(self, store):
        self.store = store

    def _runtime(self, session, tenant_id, account_id, revision=None):
        runtime = self.store._runtime(session, tenant_id, account_id)
        self.store._read_connection(session, runtime, revision)
        return runtime

    @staticmethod
    def _binding(row, runtime):
        if row.binding_id != runtime.binding_id:
            raise Refused("historical recovery binding changed")

    @staticmethod
    def _ensure_fan(session, runtime, fan_id, before_id=None):
        row = session.get(OfHistoryCursor, (runtime.account_id, fan_id))
        if row is None:
            row = OfHistoryCursor(account_id=runtime.account_id, binding_id=runtime.binding_id,
                                  fan_id=fan_id, start_before_id=before_id, before_id=before_id,
                                  updated_at=utcnow())
            session.add(row)
        else:
            HistoryRecovery._binding(row, runtime)
        return row

    def request_fan(self, tenant_id, account_id, fan_id, *, before_id=None, connection_revision=None):
        """Idempotent resume; never reset a saved cursor or a completed proof.

        ``before_id`` is an exclusive boundary whose newer page was committed
        by recent recovery. Omit it to request the complete available archive.
        Changing a persisted start boundary is refused, never silently skipped.
        """
        _id(fan_id)
        if before_id is not None:
            _id(before_id)
        with Session(self.store.engine) as session, session.begin():
            runtime = self._runtime(session, tenant_id, account_id, connection_revision)
            row = self._ensure_fan(session, runtime, fan_id, before_id)
            if row.start_before_id != before_id:
                raise Refused("historical recovery start boundary changed")

    def request_account(self, tenant_id, account_id, *, connection_revision=None):
        """Resume discovery without erasing an in-flight offset or fan cursor."""
        with Session(self.store.engine) as session, session.begin():
            runtime = self._runtime(session, tenant_id, account_id, connection_revision)
            row = session.get(OfHistoryScan, account_id)
            if row is None:
                session.add(OfHistoryScan(account_id=account_id, binding_id=runtime.binding_id, updated_at=utcnow()))
            else:
                self._binding(row, runtime)

    @staticmethod
    def _eligible(model, now):
        return and_(or_(model.retry_at.is_(None), model.retry_at <= now),
                    or_(model.status == "needed", and_(model.status == "running", model.lease_until <= now)))

    @staticmethod
    def _lease(row, now):
        row.status, row.generation, row.lease_token = "running", row.generation + 1, str(uuid.uuid4())
        row.lease_until, row.updated_at = now + timedelta(seconds=LEASE_SECONDS), now

    @staticmethod
    def _release(row, *, status="needed", error=None):
        row.status, row.last_error, row.updated_at = status, error, utcnow()
        row.lease_token = row.lease_until = None
        row.retry_at = utcnow() + timedelta(seconds=RETRY_SECONDS) if error else None

    def _check_claim(self, session, runtime, claim):
        model = OfHistoryCursor if type(claim) is ArchiveClaim else OfHistoryScan
        key = (claim.account_id, claim.fan_id) if type(claim) is ArchiveClaim else claim.account_id
        row = session.get(model, key)
        if (row is None or row.binding_id != runtime.binding_id or claim.binding_id != runtime.binding_id
                or row.status != "running" or row.generation != claim.generation or row.lease_token != claim.token
                or row.lease_until is None or row.lease_until <= utcnow()
                or (type(claim) is ArchiveClaim and row.before_id != claim.before_id)
                or (type(claim) is ArchiveScanClaim and row.cursor != claim.cursor)):
            raise Refused("stale historical recovery claim")
        return row

    def claim_scan(self, tenant_id, account_id, *, connection_revision=None):
        now = utcnow()
        with Session(self.store.engine) as session, session.begin():
            runtime = self._runtime(session, tenant_id, account_id, connection_revision)
            row = session.get(OfHistoryScan, account_id)
            if row is None:
                return None
            self._binding(row, runtime)
            # One bounded known-fan sweep per call covers archived conversations
            # absent from provider discovery without requiring them all in RAM.
            if not row.known_fans_complete:
                fan_ids = session.scalars(select(Fan.fan_id).where(Fan.account_id == account_id,
                    Fan.fan_id > row.known_fan_after).order_by(Fan.fan_id).limit(PAGE_LIMIT)).all()
                for fan_id in fan_ids:
                    self._ensure_fan(session, runtime, fan_id)
                if fan_ids:
                    row.known_fan_after = fan_ids[-1]
                row.known_fans_complete = len(fan_ids) < PAGE_LIMIT
                row.updated_at = now
            if (row.status == "complete" or (row.retry_at is not None and row.retry_at > now)
                    or (row.status == "running" and row.lease_until > now)):
                return None
            if row.page_count >= MAX_SCAN_PAGES:
                row.last_error, row.updated_at = "discovery_budget_exhausted", now
                return None
            self._lease(row, now)
            return ArchiveScanClaim(account_id, runtime.binding_id, row.generation, row.lease_token,
                                    row.cursor, connection_revision)

    def scan_page(self, tenant_id, claim, fan_ids, *, next_cursor, provider_read_claim=None):
        if type(claim) is not ArchiveScanClaim:
            raise Refused("invalid historical scan claim")
        if type(fan_ids) not in {list, tuple} or len(fan_ids) > PAGE_LIMIT:
            raise Refused("invalid historical discovery page")
        for fan_id in fan_ids:
            _id(fan_id)
        if len(set(fan_ids)) != len(fan_ids):
            raise Refused("duplicate historical discovery identity")
        # The native reader has numeric forward offsets. Requiring that exact
        # contract prevents a replay/cycle from manufacturing completeness.
        if next_cursor is not None:
            if (type(next_cursor) is not str or not next_cursor.isascii() or not next_cursor.isdecimal()
                    or not 1 <= len(next_cursor) <= 7 or next_cursor.startswith("0")
                    or not fan_ids or int(next_cursor) > 1_000_000
                    or int(next_cursor) != int(claim.cursor or 0) + len(fan_ids)):
                raise Refused("invalid historical discovery cursor")
        with Session(self.store.engine) as session, session.begin():
            runtime = self._runtime(session, tenant_id, claim.account_id, claim.connection_revision)
            if provider_read_claim is not None:
                from onlyfans_runtime.provider_read_budget import current
                current(self.store,tenant_id,provider_read_claim,session=session)
            row = self._check_claim(session, runtime, claim)
            for fan_id in fan_ids:
                self._ensure_fan(session, runtime, fan_id)
            row.page_count += 1
            row.cursor = next_cursor
            row.completed_at = utcnow() if next_cursor is None else None
            self._release(row, status="complete" if next_cursor is None else "needed")

    @staticmethod
    def _live_work(session, account_id, fan_id):
        return session.scalar(select(OfWork.fan_id).where(OfWork.account_id == account_id,
            OfWork.fan_id == fan_id, OfWork.status.in_(("waiting", "claimed"))).limit(1)) is not None

    def claim(self, tenant_id, account_id, *, connection_revision=None):
        now = utcnow()
        with Session(self.store.engine) as session, session.begin():
            runtime = self._runtime(session, tenant_id, account_id, connection_revision)
            if runtime.pending_events >= MAX_PENDING - LIVE_RESERVE:
                return None
            live_work = select(OfWork.fan_id).where(OfWork.account_id == OfHistoryCursor.account_id,
                OfWork.fan_id == OfHistoryCursor.fan_id, OfWork.status.in_(("waiting", "claimed"))).exists()
            row = session.scalar(select(OfHistoryCursor).where(OfHistoryCursor.account_id == account_id,
                self._eligible(OfHistoryCursor, now), ~live_work if runtime.enabled else True).order_by(
                    OfHistoryCursor.generation, OfHistoryCursor.fan_id).limit(1))
            if row is None:
                return None
            self._binding(row, runtime)
            self._lease(row, now)
            return ArchiveClaim(account_id, runtime.binding_id, row.fan_id, row.generation,
                                row.lease_token, row.before_id, connection_revision)

    def page(self, tenant_id, claim, events, *, has_more, observations=(), provider_read_claim=None):
        if type(claim) is not ArchiveClaim:
            raise Refused("invalid historical recovery claim")
        if (type(events) not in {list, tuple} or type(has_more) is not bool or len(events) > PAGE_LIMIT
                or (has_more and not events) or any(type(event) is not Event for event in events)):
            raise Refused("invalid historical page")
        normalized = [event.normalized() for event in events]
        ids = [event.message_id for event in normalized]
        if ids != sorted(set(ids), reverse=True) or any(event.fan_id != claim.fan_id for event in normalized):
            raise Refused("invalid historical order or scope")
        if claim.before_id is not None and any(mid >= claim.before_id for mid in ids):
            raise Refused("historical cursor did not advance")
        from onlyfans_runtime.offer_terms import OfferObservation
        if (type(observations) not in {list, tuple} or len(observations) > len(normalized)
                or any(type(o) is not OfferObservation for o in observations)):
            raise Refused("invalid historical offer observations")
        offers = [offer.normalized() for offer in observations]
        by_id = {event.message_id: event for event in normalized}
        for offer in offers:
            event = by_id.get(offer.source_id)
            if (event is None or offer.source_kind != "message" or offer.fan_id != claim.fan_id
                    or event.direction != "out" or offer.media_ids != event.media_ids
                    or offer.price_cents != event.price_cents or offer.source_at != event.created_at):
                raise Refused("offer observation outside this historical page")
        if len({offer.source_id for offer in offers}) != len(offers):
            raise Refused("duplicate historical offer observation")
        with Session(self.store.engine) as session, session.begin():
            runtime = self._runtime(session, tenant_id, claim.account_id, claim.connection_revision)
            if provider_read_claim is not None:
                from onlyfans_runtime.provider_read_budget import current
                current(self.store,tenant_id,provider_read_claim,session=session)
            row = self._check_claim(session, runtime, claim)
            if runtime.pending_events + len(normalized) > MAX_PENDING - LIVE_RESERVE:
                self._release(row, error="backpressure")
                return "backpressure"
            if runtime.enabled and self._live_work(session, claim.account_id, claim.fan_id):
                self._release(row, error="live_input_pending")
                return "live_input_pending"
            added = 0
            # Preflight the whole page before writes; an invalid last row must
            # neither advance a cursor nor partially commit earlier rows.
            for event in normalized:
                if datetime.fromisoformat(event.created_at).replace(tzinfo=None) > utcnow() + timedelta(minutes=5):
                    raise Refused("future historical timestamp")
                existing = session.get(Message, (claim.account_id, claim.fan_id, event.message_id))
                stamp = datetime.fromisoformat(event.created_at).replace(tzinfo=None)
                if existing is not None and (existing.direction != event.direction or existing.body != event.body
                        or existing.price_cents != event.price_cents or existing.created_at != stamp
                        or json.loads(existing.media_ids or "[]") != list(event.media_ids)):
                    # Preserve the shared fail-closed conflict behaviour without
                    # partially importing this page. Only the offending ingress
                    # becomes durable diagnostic evidence; cursor/counts stay put.
                    payload = event.payload()
                    self.store._record(session, runtime, event, payload, "import", utcnow())
                    conflict = session.get(OfIngress, (claim.account_id, hashlib.sha256(payload.encode()).hexdigest()))
                    if conflict.status == "pending":
                        runtime.pending_events -= 1
                    conflict.status, conflict.reason, conflict.applied_at = "quarantined", "conflicting_message_identity", utcnow()
                    self.store._state(session, claim.account_id, claim.fan_id).requires_attention = True
                    runtime.recovery_needed = True
                    self._release(row, error="conflicting_history")
                    return "conflict"
                ingress = session.get(OfIngress, (claim.account_id, hashlib.sha256(event.payload().encode()).hexdigest()))
                if ingress is not None:
                    if ingress.binding_id != runtime.binding_id or ingress.status == "quarantined":
                        raise Refused("historical ingress is not applicable")
                    if ingress.status == "applied" and existing is None:
                        raise Refused("historical canonical row is missing")
                    if ingress.status == "pending" and ingress.source != "import":
                        self._release(row, error="live_input_pending")
                        return "live_input_pending"
                if existing is None:
                    added += 1
            for event in normalized:
                payload = event.payload()
                self.store._record(session, runtime, event, payload, "import", utcnow())
                ingress = session.get(OfIngress, (claim.account_id, hashlib.sha256(payload.encode()).hexdigest()))
                if ingress.status == "pending":
                    self.store._apply(session, runtime, ingress, history_only=True)
                    if ingress.status != "applied":
                        raise Refused("historical canonical application failed")
            # Same pinned offer-import seam, in the same committed transaction.
            self.store._observe_offers(session, runtime, offers)
            if row.upper_bound is None and ids:
                row.upper_bound = ids[0]
            if ids:
                row.before_id = ids[-1]
                oldest = min(datetime.fromisoformat(event.created_at).replace(tzinfo=None) for event in normalized)
                row.oldest_message_at = min(row.oldest_message_at or oldest, oldest)
            row.pages_completed += 1
            row.messages_observed += len(normalized)
            row.messages_added += added
            row.completed_at = None if has_more else utcnow()
            self._release(row, status="needed" if has_more else "complete")
            return "continue" if has_more else "complete"

    def fail(self, tenant_id, claim, *, code="provider_read_failed"):
        """Persist bounded retry metadata; never store provider error text/PII."""
        if type(claim) not in {ArchiveClaim, ArchiveScanClaim} or code not in ERROR_CODES:
            raise Refused("invalid historical failure")
        with Session(self.store.engine) as session, session.begin():
            runtime = self._runtime(session, tenant_id, claim.account_id, claim.connection_revision)
            row = self._check_claim(session, runtime, claim)
            self._release(row, error=code)

    def progress(self, tenant_id, account_id, *, fan_id=None):
        """Counts are observed coverage, never an invented total or percentage."""
        if fan_id is not None:
            _id(fan_id)
        with Session(self.store.engine) as session, session.begin():
            runtime = self._runtime(session, tenant_id, account_id)
            scan = session.get(OfHistoryScan, account_id)
            if scan is not None:
                self._binding(scan, runtime)
            filters = [OfHistoryCursor.account_id == account_id]
            if fan_id is not None:
                filters.append(OfHistoryCursor.fan_id == fan_id)
            mismatched = session.scalar(select(OfHistoryCursor.fan_id).where(*filters,
                OfHistoryCursor.binding_id != runtime.binding_id).limit(1))
            if mismatched is not None:
                raise Refused("historical recovery binding changed")
            states = dict(session.execute(select(OfHistoryCursor.status, func.count()).where(*filters)
                                         .group_by(OfHistoryCursor.status)).all())
            totals = session.execute(select(func.coalesce(func.sum(OfHistoryCursor.pages_completed), 0),
                func.coalesce(func.sum(OfHistoryCursor.messages_observed), 0),
                func.coalesce(func.sum(OfHistoryCursor.messages_added), 0)).where(*filters)).one()
            retrying = session.scalar(select(func.count()).select_from(OfHistoryCursor).where(*filters,
                OfHistoryCursor.status != "complete", OfHistoryCursor.last_error.is_not(None)))
            live_work = select(OfWork.fan_id).where(OfWork.account_id == OfHistoryCursor.account_id,
                OfWork.fan_id == OfHistoryCursor.fan_id, OfWork.status.in_(("waiting", "claimed"))).exists()
            deferred = session.scalar(select(func.count()).select_from(OfHistoryCursor).where(*filters,
                OfHistoryCursor.status != "complete", live_work)) if runtime.enabled else 0
            discovery_complete = scan is not None and scan.status == "complete" and scan.known_fans_complete
            complete = bool(states) and not (states.get("needed", 0) or states.get("running", 0))
            if fan_id is None:
                complete = discovery_complete and not (states.get("needed", 0) or states.get("running", 0))
            result = {"schemaVersion": "of-history-recovery.v1", "accountId": account_id, "fanId": str(fan_id) if fan_id is not None else None,
                "status": "complete" if complete else "incomplete", "discoveryComplete": discovery_complete,
                "discoveryCursor": scan.cursor if scan is not None else None,
                "discoveryLastError": scan.last_error if scan is not None else None,
                "conversationsDiscovered": sum(states.values()), "conversationsComplete": states.get("complete", 0),
                "conversationsRetrying": retrying, "conversationsDeferredForLiveInput": deferred,
                # PostgreSQL SUM(bigint) returns NUMERIC/Decimal. These are
                # exact integral counters, not money or floating-point values.
                "pagesCommitted": int(totals[0]), "messagesObserved": int(totals[1]), "messagesAdded": int(totals[2]),
                "totalMessages": None, "percentComplete": None, "proof": "available_provider_history",
                "recentRecoveryNeeded": runtime.recovery_needed}
            if fan_id is not None:
                row = session.get(OfHistoryCursor, (account_id, fan_id))
                result.update({"beforeId": str(row.before_id) if row and row.before_id is not None else None,
                    "startBeforeId": str(row.start_before_id) if row and row.start_before_id is not None else None,
                    "upperBoundId": str(row.upper_bound) if row and row.upper_bound is not None else None,
                    "oldestMessageAt": row.oldest_message_at.isoformat() + "Z" if row and row.oldest_message_at else None,
                    "generation": row.generation if row else 0,
                    "lastError": row.last_error if row else None,
                    "retryAt": row.retry_at.isoformat() + "Z" if row and row.retry_at else None,
                    "completedAt": row.completed_at.isoformat() + "Z" if row and row.completed_at else None})
            return result

    def step(self, tenant_id, account_id, reader, *, connection_revision=None):
        """Run after the live-input drain, with at most two injected reads.

        Caller coordinates provider scheduling with Claude's shared worker.
        Provider exceptions propagate after a durable retry checkpoint; there
        is no polling loop, implicit retry, sender, activation or model call.
        """
        if reader.account_id != account_id:
            raise Refused("historical reader scope mismatch")
        from onlyfans_runtime.worker import _provider_claim, _release_provider
        from onlyfans_runtime.provider_read_budget import ReadDeferred
        completed = 0
        scan_error = None
        scan = self.claim_scan(tenant_id, account_id, connection_revision=connection_revision)
        if scan is not None:
            budget=error=None
            try:
                try:budget=_provider_claim(self.store,tenant_id,account_id,reader,connection_revision,'history')
                except ReadDeferred:
                    self.fail(tenant_id,scan,code='provider_read_deferred')
                    raise
                try:
                    page = reader.chats(cursor=scan.cursor, limit=PAGE_LIMIT)
                except Exception:
                    self.fail(tenant_id, scan)
                    raise
                try:
                    fenced={'provider_read_claim':budget} if budget is not None else {}
                    self.scan_page(tenant_id, scan, page.fan_ids, next_cursor=page.next_cursor,**fenced)
                except (Refused, AttributeError):
                    self.fail(tenant_id, scan, code="invalid_page")
                    raise
                completed += 1
            except ReadDeferred:
                pass
            except Exception as failure:
                # A failed discovery offset must not starve known fan archives.
                # Each lane still gets at most one call; propagate the error to
                # the shared worker after committing any independent progress.
                error=scan_error=failure
            finally:_release_provider(self.store,tenant_id,budget,error)
        claim = self.claim(tenant_id, account_id, connection_revision=connection_revision)
        if claim is not None:
            budget=error=None
            try:
                try:budget=_provider_claim(self.store,tenant_id,account_id,reader,connection_revision,'history')
                except ReadDeferred:
                    self.fail(tenant_id,claim,code='provider_read_deferred')
                    raise
                try:
                    page = reader.messages(fan_id=claim.fan_id, before_id=claim.before_id, limit=PAGE_LIMIT)
                except Exception:
                    self.fail(tenant_id, claim)
                    raise
                try:
                    fenced={'provider_read_claim':budget} if budget is not None else {}
                    result = self.page(tenant_id, claim, page.events, has_more=page.has_more,
                        observations=page.observations,**fenced)
                except (Refused, AttributeError):
                    self.fail(tenant_id, claim, code="invalid_page")
                    raise
                completed += result in {"continue", "complete"}
            except ReadDeferred:
                pass
            except Exception as failure:
                error=failure
                raise
            finally:_release_provider(self.store,tenant_id,budget,error)
        if scan_error is not None:
            raise scan_error
        return completed
