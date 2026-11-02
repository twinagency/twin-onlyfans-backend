"""Background refresh of provider-reported earnings totals (reporting cache, read-only).

For every verified account this stores the creator's own OnlyFans Statistics > Earnings totals for a few
fixed periods, so the dashboard can show whole-period numbers instantly instead of waiting for the full
payment history import. The numbers are the provider's, day-aligned in UTC; they never feed the ledger,
offers or sending. One provider read per account per tick, through the shared read budget.
"""
from .provider_read_budget import is_rate_limit
import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_earnings_total_models import OfEarningsTotal, PERIODS
from db.models import OfCreatorBinding
from db.onlyfans_models import OfRuntime
from .store import Refused, utcnow

TICK_SECONDS = 20
# Recent windows move quickly, lifetime totals barely: refresh them less often.
REFRESH = {"7d": timedelta(minutes=10), "30d": timedelta(minutes=10), "90d": timedelta(minutes=20), "all": timedelta(minutes=60)}
FAILURE_RETRY = timedelta(minutes=5)
LIFETIME_START = datetime(2016, 1, 1)
MAX_CENTS = 2 ** 53 - 1


def period_window(period, now):
    """Frozen UTC window. N-day periods are N calendar days ending now (today included), as the provider buckets by day."""
    if period == "all":
        return LIFETIME_START, now
    days = {"7d": 7, "30d": 30, "90d": 90}[period]
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=days - 1), now


def _cents(value):
    if not value.is_finite():
        raise ValueError("amount")
    try:
        cents = (value * 100).to_integral_value()
    except InvalidOperation:
        raise ValueError("amount") from None
    if not 0 <= cents <= MAX_CENTS:
        raise ValueError("amount")
    return int(cents)


def parse_chart(raw):
    """(gross_cents, net_cents) from the provider chart envelope, or ValueError. Never guesses a missing figure.

    `total.gross` is what fans paid; `total.total` is the creator's net after the provider fee.
    """
    try:
        data = json.loads(raw, parse_float=Decimal, parse_int=Decimal)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("response") from None
    total = data.get("total") if isinstance(data, dict) else None
    if not isinstance(total, dict) or "gross" not in total or "total" not in total:
        raise ValueError("response")
    for key in ("gross", "total"):
        if isinstance(total[key], bool) or not isinstance(total[key], Decimal):
            raise ValueError("response")
    gross, net = _cents(total["gross"]), _cents(total["total"])
    if net > gross:
        raise ValueError("response")
    return gross, net


def due_periods(rows, binding_id, revision, now, retry_after=None):
    """Periods that need a refresh now, most overdue first. `rows` maps period to a stored OfEarningsTotal."""
    retry_after = retry_after or {}
    due = []
    for period in PERIODS:
        if retry_after.get(period, now) > now:
            continue
        row = rows.get(period)
        current = row is not None and (row.binding_id, row.connection_revision) == (binding_id, revision)
        age = now - row.fetched_at if current else None
        if age is None or age >= REFRESH[period]:
            due.append((age is not None, -(age.total_seconds() if age else 0), PERIODS.index(period), period))
    return [item[-1] for item in sorted(due)]


def refresh_one(store, tenant, account, period, *, now=None, clock=utcnow):
    """One provider read and one stored row for (account, period). Returns True when a row was written."""
    from .connections import _open
    from .provider_connector import NativeProviderConnector, session_from_capture
    from .provider_read_budget import current, release
    from .worker import _provider_claim
    now = now or clock()
    with Session(store.engine) as s:
        connection = s.get(OfProviderConnection, account)
        runtime = s.get(OfRuntime, account)
        if connection is None or runtime is None or connection.binding_id != runtime.binding_id:
            return False
        clear = _open(connection, tenant)
        owner, expiry, revision, binding = connection.owner_id, connection.expires_at, connection.revision, connection.binding_id
    start, end = period_window(period, now)
    transport = NativeProviderConnector(session_from_capture(account_id=account, owner_id=owner, policy_revision=1,
        capture=clear["capture"], x_hash=clear["xHash"], proxy_url=clear["proxyUrl"], expires_at=expiry.replace(tzinfo=timezone.utc)))
    budget = _provider_claim(store, tenant, account, transport, revision, "ledger")
    failure = None
    try:
        gross, net = parse_chart(transport.earnings_chart(start=start, end=end))
        with Session(store.engine) as s, s.begin():
            store._runtime(s, tenant, account)
            if budget is not None:
                current(store, tenant, budget, session=s)
            row = s.get(OfEarningsTotal, (account, period))
            values = dict(binding_id=binding, connection_revision=revision, gross_cents=gross, net_cents=net,
                          window_start=start, window_end=end, fetched_at=clock())
            if row is None:
                s.add(OfEarningsTotal(account_id=account, period=period, **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
        return True
    except Exception as problem:
        failure = problem
        raise
    finally:
        if budget is not None:
            release(store, tenant, budget, retry_after=getattr(failure, "retry_after", None), rate_limited=is_rate_limit(failure))


class EarningsTotalsWorker(threading.Thread):
    def __init__(self, store, tenant, stop, *, observe=None, refresh=refresh_one):
        super().__init__(name="onlyfans-earnings-totals", daemon=True)
        self.store, self.tenant, self.stop, self.observe, self.refresh = store, tenant, stop, observe, refresh
        self.retry_after = {}

    def accounts(self, now):
        with Session(self.store.engine) as s:
            query = (select(OfProviderConnection.account_id, OfProviderConnection.binding_id, OfProviderConnection.revision)
                .join(OfRuntime, OfRuntime.account_id == OfProviderConnection.account_id)
                .join(OfCreatorBinding, OfCreatorBinding.id == OfRuntime.binding_id)
                .where(OfCreatorBinding.tenant_id == self.tenant, OfCreatorBinding.channel == "onlyfans",
                       OfCreatorBinding.revoked_at.is_(None), OfProviderConnection.binding_id == OfRuntime.binding_id,
                       OfProviderConnection.status.in_(("verified", "paused")), OfProviderConnection.verified_at.is_not(None),
                       OfProviderConnection.expires_at > now).order_by(OfProviderConnection.account_id))
            found = list(s.execute(query).all())
            stored = {}
            for row in s.scalars(select(OfEarningsTotal).where(OfEarningsTotal.account_id.in_([a for a, *_ in found] or [""]))):
                stored.setdefault(row.account_id, {})[row.period] = row
            return found, stored

    def tick(self):
        now = utcnow()
        found, stored = self.accounts(now)
        for account, binding, revision in found:
            if self.stop.is_set():
                return
            periods = due_periods(stored.get(account, {}), binding, revision, now,
                                  {p: t for (a, p), t in self.retry_after.items() if a == account})
            if not periods:
                continue
            try:
                self.refresh(self.store, self.tenant, account, periods[0])
            except Exception as problem:
                from .provider_read_budget import ReadDeferred
                if isinstance(problem, ReadDeferred):
                    self.retry_after[(account, periods[0])] = problem.retry_at
                else:
                    self.retry_after[(account, periods[0])] = now + FAILURE_RETRY
                    if self.observe:
                        self.observe({"schemaVersion": "of-worker.v1", "state": "earnings_totals_unavailable", "account": account})

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                if self.observe:
                    self.observe({"schemaVersion": "of-worker.v1", "state": "earnings_totals_unavailable"})
            self.stop.wait(TICK_SECONDS)
