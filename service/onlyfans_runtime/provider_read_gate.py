"""One lease and one pace per account for content-sync provider reads, shared by every worker.

The refresh worker, the paid-post reader and the vault reader each used to throttle
only themselves (or not at all), so two worker processes, or two kinds of reader,
could read the same creator account at the same time and faster than the intended
one page per five seconds. A reader now takes this account row first:

* the lease is exclusive while it is held and unexpired (a crashed worker's lease
  simply expires), whichever process or reader kind holds it;
* every read pushes `next_read_at` forward, and the next reader of that account,
  of any kind, waits for it.

The pace defaults to five seconds and can be overridden with
ONLYFANS_PROVIDER_READ_INTERVAL_SECONDS (0 disables pacing; the lease stays).
Time is the database clock of the writer, never an injected schedule clock.
"""
from datetime import timedelta
import os
import uuid

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from db.onlyfans_offer_import_models import OfProviderReadGate as Gate
from .store import utcnow

ENV = "ONLYFANS_PROVIDER_READ_INTERVAL_SECONDS"
DEFAULT_INTERVAL_SECONDS = 5.0
LEASE_SECONDS = 90


def interval():
    raw = os.environ.get(ENV)
    if raw is None:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS
    return value if 0 <= value <= 3600 else DEFAULT_INTERVAL_SECONDS


def blocked(account_column, now):
    """SQL predicate: this account cannot be read right now (for worker selection queries)."""
    return exists().where(Gate.account_id == account_column, or_(
        and_(Gate.lease_token.is_not(None), Gate.lease_until > now), Gate.next_read_at > now))


def acquire_in(s, account, holder, *, now=None, lease_seconds=LEASE_SECONDS):
    """Take the account's read lease inside the caller's transaction; the token, or None when busy or paced."""
    now = now or utcnow()
    s.execute(insert(Gate).values(account_id=account, updated_at=now).on_conflict_do_nothing())
    row = s.scalar(select(Gate).where(Gate.account_id == account).with_for_update())
    if row.lease_token is not None and row.lease_until > now:
        return None
    if row.next_read_at is not None and row.next_read_at > now:
        return None
    token = str(uuid.uuid4())
    row.holder, row.lease_token, row.updated_at = holder[:32], token, now
    row.lease_until = now + timedelta(seconds=lease_seconds)
    row.next_read_at = now + timedelta(seconds=interval())
    return token


def acquire(store, account, holder, *, lease_seconds=LEASE_SECONDS):
    with Session(store.engine) as s, s.begin():
        return acquire_in(s, account, holder, lease_seconds=lease_seconds)


def release(store, account, token):
    """Give the lease back (a no-op for a lease that already expired and was taken over); the pace stays."""
    now = utcnow()
    with Session(store.engine) as s, s.begin():
        row = s.scalar(select(Gate).where(Gate.account_id == account).with_for_update())
        if row is not None and row.lease_token == token:
            row.lease_token = row.lease_until = row.holder = None
            row.next_read_at = now + timedelta(seconds=interval())
            row.updated_at = now
