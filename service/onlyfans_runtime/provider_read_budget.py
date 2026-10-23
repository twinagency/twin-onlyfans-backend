"""Account-wide provider read fencing and shared rate-limit backoff.

Acquire and release are short database transactions. The caller performs I/O
after acquire returns and must check ``current`` before committing its result.
Background work leaves two tokens available for recent-message intake.
"""
from dataclasses import dataclass
from datetime import timedelta
import os
import uuid

from sqlalchemy.orm import Session

from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_content_sync_progress_models import OfProviderReadBudget
from .store import Refused, utcnow

LEASE_SECONDS = 45
CAPACITY = 10
RECENT_RESERVE = 2
KINDS = {"recent", "discovery", "history", "ledger", "offer_refresh", "posts", "vault", "connection_check"}


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
            and connection.expires_at>now and connection.last_error is None)
    if not valid:raise Refused('provider read connection changed')
    return runtime


def acquire(store, tenant, account, *, connection_revision, kind, now=None):
    if (os.environ.get("ONLYFANS_PROCESS_ROLE") != "worker" or kind not in KINDS
            or type(connection_revision) is not int or connection_revision<1):
        raise Refused("provider read worker authority required")
    now = now or utcnow()
    with Session(store.engine) as s, s.begin():
        runtime = _valid(store, s, tenant, account, connection_revision, now, kind)
        row = s.get(OfProviderReadBudget, account, with_for_update=True)
        if row is None:
            row = OfProviderReadBudget(account_id=account, binding_id=runtime.binding_id,
                connection_revision=connection_revision, tokens=CAPACITY, refilled_at=now)
            s.add(row)
        elif (row.binding_id, row.connection_revision) != (runtime.binding_id, connection_revision):
            # A changed session fences the old lease immediately; backoff is
            # retained because a new cookie is not a new provider rate budget.
            row.binding_id, row.connection_revision = runtime.binding_id, connection_revision
            row.lease_token = row.lease_until = None
        if row.lease_token and row.lease_until > now:
            raise ReadDeferred(row.lease_until, "provider read lease occupied")
        if row.backoff_until and row.backoff_until > now:
            raise ReadDeferred(row.backoff_until, "provider read backoff")
        refill = max(0, int((now - row.refilled_at).total_seconds()))
        available = min(CAPACITY, row.tokens + refill)
        # Deep archive recovery may be continuously due. Leave one additional
        # maintenance token for ledger/content work as well as the recent head
        # reserve; the outer scheduler rotates those maintenance lanes.
        reserve = 0 if kind == "recent" else RECENT_RESERVE + (kind == 'history')
        if available <= reserve:
            raise ReadDeferred(now + timedelta(seconds=1), "provider read budget exhausted")
        if refill:
            row.refilled_at += timedelta(seconds=refill)
        row.tokens = available - 1
        row.lease_token, row.lease_until = str(uuid.uuid4()), now + timedelta(seconds=LEASE_SECONDS)
        row.last_kind = kind
        return ReadClaim(account, runtime.binding_id, connection_revision, row.lease_token, row.lease_until,kind)


def current_in_session(store, s, tenant, claim, *, now=None):
    """Fence the result in the caller's domain transaction and lock order.

    Keeping the runtime and budget row locked through the domain commit closes
    the gap between a successful lease check and a concurrent lease/session
    replacement. This function performs no external work.
    """
    now = now or utcnow()
    runtime = _valid(store, s, tenant, claim.account, claim.revision, now, claim.kind)
    row = s.get(OfProviderReadBudget, claim.account, with_for_update=True)
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


def release(store, tenant, claim, *, retry_after=None, now=None):
    now = now or utcnow()
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, claim.account)
        row = s.get(OfProviderReadBudget, claim.account, with_for_update=True)
        if (row is None or row.binding_id != runtime.binding_id or row.binding_id != claim.binding
                or row.connection_revision != claim.revision or row.lease_token != claim.token):
            return False
        if retry_after is not None:
            seconds = min(3600, max(1, int(retry_after)))
            until = now + timedelta(seconds=seconds)
            row.backoff_until = max(row.backoff_until or until, until)
        row.lease_token = row.lease_until = None
    return True
