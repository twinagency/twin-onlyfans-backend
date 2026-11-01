"""Account-wide provider read fencing and shared rate-limit backoff.

Acquire and release are short database transactions. The caller performs I/O
after acquire returns and must check ``current`` before committing its result.
Background work leaves two tokens available for recent-message intake.
"""
from dataclasses import dataclass, replace
from datetime import timedelta
import os
import uuid

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_content_sync_progress_models import OfProviderReadBudget, OfProviderReadLease
from .import_capacity import capacity, refill as refill_tokens
from .store import Refused, utcnow

LEASE_SECONDS = 45
CAPACITY = 10
RECENT_RESERVE = 2
LIVE_REPLY_RESERVE = 4        # extra tokens kept for live work while replies wait
BACKGROUND_KINDS = {"discovery", "history", "offer_refresh", "posts", "vault"}   # ledger (money) and recent (intake) are not background
LEDGER_RETRY_SECONDS = 2  # existing LedgerWorker tick cadence
KINDS = {"recent", "discovery", "history", "ledger", "offer_refresh", "posts", "vault", "connection_check"}
PARALLEL_TOKEN = 'parallel-read-leases'
LEDGER_TURN_RESERVED = "provider_read_deferred:ledger-turn-reserved"


class ReadDeferred(Refused):
    def __init__(self, retry_at, reason="provider read deferred"):
        super().__init__(reason)
        self.retry_at = retry_at


@dataclass(frozen=True)
class ReadClaim:
    account: str
    binding: str
    revision: int
    token: str
    until: object
    kind: str = 'recent'
    parallel: bool = False


def _valid(store, s, tenant, account, revision, now, kind):
    runtime = store._runtime(s, tenant, account)
    connection = s.get(OfProviderConnection, account)
    if (connection is None or connection.binding_id != runtime.binding_id
            or connection.revision != revision):
        raise Refused("provider read connection changed")
    if kind == 'connection_check':
        # An elapsed *verification lease* permits only the pre-existing identity
        # probe which renews it. It grants no history/model/delivery capability.
        renewable=(connection.status in {'verified','expired'} and connection.verified_at is not None
            and connection.last_error in {None,'provider_connection_check_retry'})
        valid=(connection.status=='pending' and connection.expires_at>now) or renewable
    else:
        statuses={'verified','paused'} if kind=='ledger' else {'verified'}
        valid=(connection.status in statuses and connection.verified_at is not None
            and connection.expires_at>now)
        if valid and connection.last_error=='provider_connection_check_retry':
            # Preserve retry pacing without granting domain read authority while
            # identity verification is pending. Once backoff ends, the existing
            # connection-error guard still requires a successful identity check.
            budget=s.get(OfProviderReadBudget,account,with_for_update=True)
            if (budget is not None and budget.binding_id==runtime.binding_id
                    and budget.connection_revision==revision
                    and budget.backoff_until is not None and budget.backoff_until>now):
                raise ReadDeferred(budget.backoff_until,'provider read backoff')
        valid=valid and connection.last_error is None
    if not valid:raise Refused('provider read connection changed')
    return runtime


def _replies_waiting(s, account, now):
    """True when the engine's work board shows fans waiting for / receiving a reply (updated in the last 2 minutes).

    Best effort and fail-open: no board (older engine, other schema) means no opinion. Read-only."""
    try:
        from sqlalchemy import text
        with s.begin_nested():          # a missing table must not poison the caller's transaction
            row = s.execute(text("SELECT 1 FROM fastt_runtime.engine_work WHERE account_id=:a "
                                 "AND state IN ('waiting','quiet','preparing','sending') AND updated_at>:since LIMIT 1"),
                            {"a": account, "since": (now - timedelta(minutes=2)).isoformat(sep=" ", timespec="microseconds")}).first()
        return row is not None
    except Exception:
        return False


def _reserve_due_ledger_turn(s, account, runtime, revision, now, last_kind):
    """Keep other reads off a due financial refresh.

    The worker and inbox schedulers are independent. A recent-inbox page can
    take the single account lease just before the LedgerWorker tick, leaving
    the ledger scan deferred even though every token is available. The first
    non-ledger attempt at an idle, due refresh writes a durable reservation on
    the scan. A dead ledger worker cannot monopolise intake: the reservation
    expires with the same 45-second bound as the provider lease.
    """
    from db.onlyfans_ledger_models import OfLedgerScan
    scan = s.get(OfLedgerScan, account)
    if (scan is None or scan.binding_id != runtime.binding_id
            or scan.connection_revision != revision or scan.lease_token is not None):
        return False
    due = scan.next_poll_at is not None and scan.next_poll_at <= now + timedelta(seconds=LEDGER_RETRY_SECONDS)
    if not due:
        return False
    fresh = (scan.updated_at is not None
             and timedelta(0) <= now - scan.updated_at < timedelta(seconds=LEASE_SECONDS))
    if scan.status == "running":
        # A multi-page scan is still the same due ledger turn. It has no domain
        # lease between pages, so without this branch a recent-inbox read can
        # take the provider lease before the next page starts.
        if not fresh:
            return False
        if scan.last_error is None:
            # A completed ledger page yields one turn to live intake. The next
            # recent read then restores priority for the following page.
            if last_kind == "ledger":
                return False
            scan.last_error = LEDGER_TURN_RESERVED
            scan.updated_at = now
        return scan.last_error == LEDGER_TURN_RESERVED or scan.last_error.startswith("provider_read_deferred")
    if scan.status == "idle" and scan.next_poll_at <= now and scan.last_error is None:
        scan.last_error = LEDGER_TURN_RESERVED
        scan.updated_at = now
        return True
    return False


def acquire(store, tenant, account, *, connection_revision, kind, now=None):
    if (os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker" or kind not in KINDS
            or type(connection_revision) is not int or connection_revision<1):
        raise Refused("provider read worker authority required")
    now = now or utcnow()
    policy = capacity(account)
    parallel = policy.inflight > 1
    deferred_at = None
    claim = None
    with Session(store.engine) as s, s.begin():
        runtime = _valid(store, s, tenant, account, connection_revision, now, kind)
        row = s.get(OfProviderReadBudget, account, with_for_update=True)
        if row is None:
            row = OfProviderReadBudget(account_id=account, binding_id=runtime.binding_id,
                connection_revision=connection_revision, tokens=policy.burst, refilled_at=now)
            s.add(row)
        elif (row.binding_id, row.connection_revision) != (runtime.binding_id, connection_revision):
            # A changed session fences the old lease immediately; backoff is
            # retained because a new cookie is not a new provider rate budget.
            row.binding_id, row.connection_revision = runtime.binding_id, connection_revision
            row.lease_token = row.lease_until = None
        if parallel and row.rate_ceiling is not None:
            policy = replace(policy, rps=min(policy.rps, max(1, row.rate_ceiling)))
        # The sentinel also excludes older single-lease workers during rollout.
        # Every mode observes outstanding parallel claims before admitting work.
        active = []
        if parallel or row.lease_token == PARALLEL_TOKEN:
            s.execute(delete(OfProviderReadLease).where(
                OfProviderReadLease.account_id == account,
                (OfProviderReadLease.lease_until <= now) |
                (OfProviderReadLease.connection_revision != connection_revision) |
                (OfProviderReadLease.binding_id != runtime.binding_id)))
            active = s.scalars(select(OfProviderReadLease).where(
                OfProviderReadLease.account_id == account)).all()
            if not active and row.lease_token == PARALLEL_TOKEN:
                row.lease_token = row.lease_until = None
        # Leave separate concurrent slots for recent and financial reads.
        slot_limit = max(1, policy.inflight - 2) if kind in BACKGROUND_KINDS else max(1, policy.inflight - 1) if kind == 'recent' else policy.inflight
        if parallel and len(active) >= slot_limit:
            raise ReadDeferred(now + timedelta(milliseconds=100), 'provider read slots occupied')
        if row.lease_token and row.lease_until > now and not (parallel and row.lease_token == PARALLEL_TOKEN):
            # A successful read normally releases well before its crash lease
            # expires. Recheck at the worker cadence, without stealing the lease.
            raise ReadDeferred(min(row.lease_until, now + timedelta(seconds=LEDGER_RETRY_SECONDS)), "provider read lease occupied")
        if row.backoff_until and row.backoff_until > now:
            raise ReadDeferred(row.backoff_until, "provider read backoff")
        if (kind not in {'ledger', 'connection_check'}
                and not parallel and _reserve_due_ledger_turn(
                    s, account, runtime, connection_revision, now, row.last_kind)):
            # Commit the scan reservation before refusing this read. Raising
            # inside this transaction would roll the reservation back, which is
            # exactly how an always-due ledger lost priority to inbox reads.
            deferred_at = now + timedelta(seconds=LEDGER_RETRY_SECONDS)
        else:
            available, refilled_at = refill_tokens(row.tokens, row.refilled_at, now, policy)
            # Deep archive recovery may be continuously due. Leave one additional
            # maintenance token for ledger/content work as well as the recent head
            # reserve; the outer scheduler rotates those maintenance lanes.
            reserve = 0 if kind == "recent" else RECENT_RESERVE + (kind == 'history')
            if kind in BACKGROUND_KINDS and _replies_waiting(s, account, now):
                # Live replies are waiting (the FASTT engine's work board): history import, the exporter, discovery, offer
                # refresh, posts and vault analysis yield. They keep a trickle (a read per ~5 s as the bucket refills), they
                # are never starved outright.
                reserve += LIVE_REPLY_RESERVE
            cost = 2 if parallel and kind != 'connection_check' else 1
            if available < reserve + cost:
                raise ReadDeferred(now + timedelta(seconds=1), "provider read budget exhausted")
            row.refilled_at = refilled_at
            row.tokens = available - cost
            token, until = str(uuid.uuid4()), now + timedelta(seconds=LEASE_SECONDS)
            if parallel:
                s.add(OfProviderReadLease(token=token, account_id=account,
                    binding_id=runtime.binding_id, connection_revision=connection_revision,
                    kind=kind, lease_until=until))
                row.lease_token = PARALLEL_TOKEN
                row.lease_until = max([until] + [v.lease_until for v in active])
            else:
                row.lease_token, row.lease_until = token, until
            row.last_kind = kind
            claim = ReadClaim(account, runtime.binding_id, connection_revision, token, until, kind, parallel)
    if deferred_at is not None:
        raise ReadDeferred(deferred_at, 'provider ledger turn due')
    return claim


def current_in_session(store, s, tenant, claim, *, now=None):
    """Fence the result in the caller's domain transaction and lock order.

    Keeping the runtime and budget row locked through the domain commit closes
    the gap between a successful lease check and a concurrent lease/session
    replacement. This function performs no external work.
    """
    now = now or utcnow()
    runtime = _valid(store, s, tenant, claim.account, claim.revision, now, claim.kind)
    row = s.get(OfProviderReadBudget, claim.account, with_for_update=True)
    if claim.parallel:
        lease = s.get(OfProviderReadLease, claim.token)
        if (row is None or row.binding_id != runtime.binding_id or row.binding_id != claim.binding
                or row.connection_revision != claim.revision or row.lease_token != PARALLEL_TOKEN
                or lease is None or lease.account_id != claim.account or lease.binding_id != claim.binding
                or lease.connection_revision != claim.revision or lease.kind != claim.kind
                or lease.lease_until <= now):
            raise Refused('stale provider read claim')
        return True
    if (row is None or row.binding_id != runtime.binding_id or row.binding_id != claim.binding
            or row.connection_revision != claim.revision or row.lease_token != claim.token or row.last_kind!=claim.kind
            or row.lease_until is None or row.lease_until <= now):
        raise Refused("stale provider read claim")
    return True


def current(store, tenant, claim, *, now=None, session=None):
    if session is not None:
        return current_in_session(store, session, tenant, claim, now=now)
    with Session(store.engine) as s, s.begin():
        return current_in_session(store, s, tenant, claim, now=now)


def native_reader(reader):
    """Only real account-bound HTTPS adapters consume the provider budget."""
    from .provider_connector import NativeProviderConnector, NativeProviderSession
    return (isinstance(NativeProviderConnector, type) and isinstance(reader, NativeProviderConnector)
        and isinstance(getattr(reader, '_handle', None), NativeProviderSession))


def release(store, tenant, claim, *, retry_after=None, rate_limited=False, now=None):
    now = now or utcnow()
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, claim.account)
        row = s.get(OfProviderReadBudget, claim.account, with_for_update=True)
        if (row is None or row.binding_id != runtime.binding_id or row.binding_id != claim.binding
                or row.connection_revision != claim.revision):
            return False
        if claim.parallel:
            lease = s.get(OfProviderReadLease, claim.token)
            if (lease is None or lease.account_id != claim.account or lease.binding_id != claim.binding
                    or lease.connection_revision != claim.revision or lease.kind != claim.kind
                    or row.lease_token != PARALLEL_TOKEN):
                return False
            s.delete(lease)
            s.flush()
            remaining = s.scalars(select(OfProviderReadLease).where(
                OfProviderReadLease.account_id == claim.account,
                OfProviderReadLease.lease_until > now)).all()
            row.lease_until = max(v.lease_until for v in remaining) if remaining else None
            row.lease_token = PARALLEL_TOKEN if remaining else None
        elif row.lease_token == claim.token:
            row.lease_token = row.lease_until = None
        else:
            return False
        if retry_after is not None:
            seconds = min(3600, max(1, int(retry_after)))
            until = now + timedelta(seconds=seconds)
            row.backoff_until = max(row.backoff_until or until, until)
            if claim.parallel and rate_limited:
                target = capacity(claim.account).rps
                row.rate_ceiling = max(1, min(target, row.rate_ceiling or target) // 2)
                row.rate_recover_at = until + timedelta(minutes=5)
        elif claim.parallel and row.rate_ceiling is not None and row.rate_recover_at is not None and row.rate_recover_at <= now:
            target = capacity(claim.account).rps
            row.rate_ceiling = min(target, row.rate_ceiling + 1)
            row.rate_recover_at = now + timedelta(minutes=5)
    return True

