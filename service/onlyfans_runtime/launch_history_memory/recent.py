"""Explicit recent/archive cutover for Claude's shared worker integration.

Initial recovery (high_water=0) reads the newest page even when an old worker
left a descending cursor. Its remainder is durably handed to archive recovery.
Subsequent outage catch-up MUST still reach the existing high-water boundary;
it is never truncated to one page. This module does not mount itself.
"""
from datetime import datetime, timedelta
import hashlib
import json

from sqlalchemy import BigInteger, CheckConstraint, Column, ForeignKey, ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import Session

from db.models import Message, OfCreatorBinding
from db.onlyfans_models import OfIngress, OfRecovery, OfRuntime
from onlyfans_runtime.store import Event, PAGE_LIMIT, MAX_PENDING, RecoveryClaim, Refused, page_cursor, utcnow
from .recovery import INFO, HistoryRecovery, RecoveryBase


class RecentBootstrap(RecoveryBase):
    """Initial-head coverage, separate from pre-existing legacy deep cursors."""
    __tablename__ = "of_history_recent_bootstrap"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "fan_id"], [OfRecovery.account_id, OfRecovery.fan_id], ondelete="RESTRICT"),
        CheckConstraint("observed_count >= 0 AND observed_count < 200", name="count"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey(OfRuntime.account_id, ondelete="RESTRICT"), primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    binding_id = Column(String, ForeignKey(OfCreatorBinding.id, ondelete="RESTRICT"), nullable=False)
    upper_id = Column(BigInteger, nullable=False, default=0)
    before_id = Column(BigInteger)
    observed_count = Column(Integer, nullable=False, default=0)


def request_before(store, tenant_id, claim, *, connection_revision=None):
    if type(claim) is not RecoveryClaim:
        raise Refused("recorded recent recovery claim required")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant_id, claim.account_id)
        store._read_connection(s, runtime, connection_revision)
        row = s.get(OfRecovery, (claim.account_id, claim.fan_id))
        if (row is None or row.generation != claim.generation or row.lease_token != claim.token
                or row.status != "running" or row.lease_until <= utcnow()):
            raise Refused("stale recent recovery claim")
        if row.high_water or row.last_complete_at is not None:
            return claim.before_id
        boot = s.get(RecentBootstrap, (claim.account_id, claim.fan_id))
        if boot and boot.binding_id != runtime.binding_id:
            raise Refused("recent bootstrap binding changed")
        return boot.before_id if boot else None


def commit_recent_page(store, tenant_id, claim, events, *, has_more,
                       observations=(), connection_revision=None, provider_read_claim=None, next_before=None, skipped_inbound=()):
    """Replace worker.py's recovery_page call together with request_before.

    The initial head, offers, archive handoff and recent completion are one
    transaction. Discovery and all other recent-recovery obligations still gate
    Store._mark_current. Archive status cannot clear quarantine or outage holds.
    """
    if type(claim) is not RecoveryClaim:
        raise Refused("recorded recent recovery claim required")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant_id, claim.account_id)
        store._read_connection(s, runtime, connection_revision)
        existing_recovery = s.get(OfRecovery, (claim.account_id, claim.fan_id))
        if (existing_recovery is None or existing_recovery.status != "running"
                or existing_recovery.generation != claim.generation
                or existing_recovery.lease_token != claim.token
                or existing_recovery.lease_until is None or existing_recovery.lease_until <= utcnow()
                or existing_recovery.high_water != claim.high_water
                or existing_recovery.before_id != claim.before_id):
            raise Refused("stale recent recovery claim")
        initial = existing_recovery is not None and existing_recovery.high_water == 0 and existing_recovery.last_complete_at is None
    if not initial:
        return store.recovery_page(tenant_id, claim, events, has_more=has_more,
            observations=observations, connection_revision=connection_revision,
            provider_read_claim=provider_read_claim, next_before=next_before, skipped_inbound=skipped_inbound)
    if (type(events) not in {list, tuple} or type(has_more) is not bool
            or len(events) > PAGE_LIMIT or (has_more and not events and next_before is None)
            or any(type(e) is not Event for e in events)):
        raise Refused("invalid recent head page")
    normalized = [e.normalized() for e in events]
    ids = [e.message_id for e in normalized]
    if ids != sorted(set(ids), reverse=True) or any(e.fan_id != claim.fan_id for e in normalized):
        raise Refused("invalid recent head order or scope")
    # The head read used request_before(), not claim.before_id; checked below.
    cursor = page_cursor(ids, next_before)
    from onlyfans_runtime.offer_terms import OfferObservation
    if (type(observations) not in {list, tuple} or len(observations) > len(normalized)
            or any(type(o) is not OfferObservation for o in observations)):
        raise Refused("invalid recent offer observations")
    offers = [o.normalized() for o in observations]
    by_id = {e.message_id: e for e in normalized}
    for o in offers:
        event = by_id.get(o.source_id)
        if (event is None or o.source_kind != "message" or o.fan_id != claim.fan_id
                or event.direction != "out" or o.media_ids != event.media_ids
                or o.price_cents != event.price_cents or o.source_at != event.created_at):
            raise Refused("recent offer observation mismatch")
    if len({o.source_id for o in offers}) != len(offers):
        raise Refused("duplicate recent offer observation")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant_id, claim.account_id)
        store._read_connection(s, runtime, connection_revision)
        if provider_read_claim is not None:
            from onlyfans_runtime.provider_read_budget import current
            current(store,tenant_id,provider_read_claim,session=s)
        row = s.get(OfRecovery, (claim.account_id, claim.fan_id))
        if (row is None or row.status != "running" or row.generation != claim.generation
                or row.lease_token != claim.token or row.lease_until <= utcnow()
                or row.high_water != 0 or row.before_id != claim.before_id):
            raise Refused("stale recent recovery claim")
        boot = s.get(RecentBootstrap, (claim.account_id, claim.fan_id))
        if boot and (boot.binding_id != runtime.binding_id
                     or boot.before_id is not None and (any(i >= boot.before_id for i in ids)
                                                        or cursor is not None and cursor >= boot.before_id)):
            raise Refused("recent bootstrap cursor or binding changed")
        from onlyfans_runtime.history_policy import policy
        window = policy(s, claim.account_id, runtime.binding_id)
        if window is not None:
            # Recent head bootstrap has its own small live-read bound. Deep
            # coverage belongs to the independent recent-window cursor.
            normalized = [e for e in normalized if datetime.fromisoformat(e.created_at).replace(tzinfo=None) >= window.cutoff_at]
            kept = {e.message_id for e in normalized}
            offers = [o for o in offers if o.source_id in kept]
        if runtime.pending_events + len(normalized) > MAX_PENDING:
            return "backpressure"
        for event in normalized:
            stamp = datetime.fromisoformat(event.created_at).replace(tzinfo=None)
            if stamp > utcnow() + timedelta(minutes=5):
                raise Refused("future recent timestamp")
            old = s.get(Message, (claim.account_id, claim.fan_id, event.message_id))
            if old is not None and (old.body != event.body or old.direction != event.direction
                    or old.created_at != stamp or old.price_cents != event.price_cents
                    or json.loads(old.media_ids or "[]") != list(event.media_ids)):
                # Existing mandatory recovery remains held; do not publish a
                # false recent-sync proof or a partial archive cutover.
                raise Refused("conflicting recent message identity")
            key = hashlib.sha256(event.payload().encode()).hexdigest()
            ingress = s.get(OfIngress, (claim.account_id, key))
            if ingress and (ingress.binding_id != runtime.binding_id or ingress.status == "quarantined"):
                raise Refused("recent ingress is not applicable")
            if ingress and ingress.status == "applied" and old is None:
                raise Refused("recent canonical row is missing")
            if ingress and ingress.status == "pending" and ingress.source != "import":
                return "live_input_pending"  # Drain preserves the existing live source.
        for event in normalized:
            store._record(s, runtime, event, event.payload(), "import", utcnow())
            key = hashlib.sha256(event.payload().encode()).hexdigest()
            ingress = s.get(OfIngress, (claim.account_id, key))
            if ingress.status == "pending":
                store._apply(s, runtime, ingress, history_only=True)
                if ingress.status != "applied":
                    raise Refused("recent import application failed")
        store._observe_offers(s, runtime, offers)
        store._skipped_input(s, runtime, claim.fan_id, skipped_inbound, ids, boot and boot.upper_id)
        if boot is None:
            boot = RecentBootstrap(account_id=claim.account_id, fan_id=claim.fan_id,
                binding_id=runtime.binding_id, upper_id=max(ids, default=0), observed_count=0)
            s.add(boot)
        boot.observed_count += len(ids)
        if cursor is not None:
            boot.before_id = cursor
        if has_more and boot.observed_count < 100:
            row.before_id, row.upper_bound = boot.before_id, boot.upper_id
            row.status = "needed"
            row.lease_token = row.lease_until = None
            return "continue"
        if has_more:
            HistoryRecovery._ensure_fan(s, runtime, claim.fan_id, before_id=None if window else boot.before_id)
        row.high_water = boot.upper_id
        row.upper_bound = row.before_id = None
        row.status, row.last_complete_at = "complete", utcnow()
        row.lease_token = row.lease_until = None
        store._mark_current(s, runtime)
        return "complete"
