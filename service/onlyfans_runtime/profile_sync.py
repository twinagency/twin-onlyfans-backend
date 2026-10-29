"""Every known fan's display profile, independent of reply/model eligibility.

One shared, connection-fenced read lease covers selection, HTTP and commit.
Attempts survive restart. Missing/deleted profiles stay explicitly unavailable;
they cannot pin the next batch or be reported as resolved names.
"""
from datetime import timedelta
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from db.models import Fan
from .profiles import apply_profiles, profile
from .provider_read_budget import acquire, current, release, native_reader
from .read_failures import ERROR_CODES, failure_code
from .store import Refused, utcnow

BATCH_SIZE = 20
REFRESH = timedelta(days=1)
RETRY = timedelta(minutes=5)


def _due(account, now):
    return select(Fan.fan_id).where(Fan.account_id == account,
        or_(Fan.of_profile_checked_at.is_(None), Fan.of_profile_checked_at <= now - REFRESH,
            and_(Fan.of_profile_error.in_((*ERROR_CODES, 'provider_read_failed')),
                 Fan.of_profile_checked_at <= now - RETRY)))


def step(store, tenant, account, reader, *, connection_revision=None):
    # Synthetic readers do not accidentally gain real provider capabilities.
    if not native_reader(reader): return 0
    if reader.account_id != account: raise Refused("profile reader scope mismatch")
    revision = connection_revision if connection_revision is not None else reader.connection_revision
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        store._read_connection(s, runtime, revision)
        if s.scalar(_due(account, utcnow()).limit(1)) is None: return 0
    budget = acquire(store, tenant, account, connection_revision=revision, kind="history")
    error = None
    try:
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account)
            store._read_connection(s, runtime, revision)
            current(store, tenant, budget, session=s)
            now = utcnow()
            ids = s.scalars(_due(account, now)
                .order_by(Fan.of_profile_checked_at.asc().nullsfirst(), Fan.fan_id).limit(BATCH_SIZE)).all()
        if not ids: return 0
        try:
            profiles = reader.profiles(ids)
        except Exception as failure:
            error = failure
            # Only a still-current lease may record this attempt. A replaced
            # session/binding must not mark the replacement's fans checked.
            with Session(store.engine) as s, s.begin():
                runtime = store._runtime(s, tenant, account)
                store._read_connection(s, runtime, revision)
                current(store, tenant, budget, session=s)
                for fan in ids:
                    row = s.get(Fan, (account, fan))
                    row.of_profile_checked_at, row.of_profile_error = utcnow(), failure_code(failure)
            raise
        with Session(store.engine) as s, s.begin():
            runtime = store._runtime(s, tenant, account)
            store._read_connection(s, runtime, revision)
            current(store, tenant, budget, session=s)
            if type(profiles) is not dict or any(type(fan) is not int or fan not in ids for fan in profiles):
                raise Refused("profile batch outside requested scope")
            apply_profiles(s, runtime, ids, profiles)
            for fan in ids:
                row = s.get(Fan, (account, fan))
                data = profile(profiles.get(fan))
                row.of_profile_checked_at = utcnow()
                row.of_profile_error = (None if data.get('name') or data.get('handle')
                    else 'profile_fields_missing' if fan in profiles else 'profile_unavailable')
        return len(ids)
    except Exception as failure:
        error = failure
        raise
    finally:
        release(store, tenant, budget, retry_after=getattr(error, 'retry_after', None))
