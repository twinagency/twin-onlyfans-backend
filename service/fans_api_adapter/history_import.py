"""Budgeted, resumable history and ownership import for disabled account preparation.

The owners reused here stay the owners: the preparation scope and its request guard (ceiling, authority fence,
read-only allowlist), the vault job (window), ``FansWebhookAdapter`` ingestion (messages, transactions,
chargebacks, unlock evidence) and the provider client (classified failures). This module adds only coverage
state: per-source sub-budgets, per-fan cursors, retained failed pages and quarantined rows, first-seen message
facts and a read meter. It has no sender, planner, model or media path.

Coverage is derived from cursors, retained gaps and counters, never from elapsed time, and a missing record is
never "not purchased".
"""
from datetime import datetime
from hashlib import sha256
import json
import re
from urllib.parse import parse_qsl

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from db.onlyfans_history_models import (OfFansHistoryCursor, OfFansImportGap, OfFansImportSource,
                                        OfFansReadMeter, SOURCES)
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow

VERSION = "history-import.v1"
HISTORY_SOURCES = ("chats", "transactions", "chargebacks")
PAGE_SIZE = 100
# Pagination safety caps per cursor/source. Reaching one is a retained, exhausted gap, never a completion.
MAX_PAGES = {"chats": 1000, "transactions": 5000, "chargebacks": 5000}
_DATE = re.compile(r"20[0-9]{2}-[01][0-9]-[0-3][0-9] [0-2][0-9]:[0-5][0-9]:[0-5][0-9]\Z")
_CHAT_KEYS = frozenset({"limit", "order", "first_id", "last_id", "id", "skip_users"})
_NUMERIC = re.compile(r"[1-9][0-9]{0,18}\Z")


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise Refused("invalid history import limit")
    return value


def _shape(spec):
    """Structural validation of the explicit command. There are no defaults."""
    if type(spec) is not dict or set(spec) != {"contract_version", "start_date", "webhooks", "sources"}:
        raise Refused("invalid history import fields")
    if spec["contract_version"] != VERSION:
        raise Refused("invalid history import version")
    start = spec["start_date"]
    if type(start) is not str or not _DATE.fullmatch(start):
        raise Refused("invalid history import start date")
    try:
        if datetime.strptime(start, "%Y-%m-%d %H:%M:%S") > utcnow():
            raise Refused("history import start date is in the future")
    except ValueError:
        raise Refused("invalid history import start date") from None
    hooks = spec["webhooks"]
    if hooks is not None and (type(hooks) is not dict or set(hooks) != {"reference"} or type(hooks["reference"]) is not str
                              or not hooks["reference"].strip() or len(hooks["reference"]) > 200):
        raise Refused("invalid history import webhook attestation")
    sources = spec["sources"]
    if type(sources) is not dict or set(sources) != set(SOURCES):
        raise Refused("explicit budgets for every history source required")
    for value in sources.values():
        if type(value) is not dict or set(value) != {"requests", "credits", "min_interval_seconds"}:
            raise Refused("invalid history source budget")
        _int(value["requests"], 1, 10000)
        _int(value["credits"], 1, 10**9)
        _int(value["min_interval_seconds"], 0, 86400)
    return json.loads(_encoded(spec))


def _initial_cursor(source, spec):
    if source == "transactions":
        return {"start_date": spec["start_date"], "marker": None, "first_page_at": None, "end_reached": False}
    if source == "chargebacks":
        return {"start_date": spec["start_date"], "offset": 0, "first_page_at": None, "end_reached": False}
    if source == "chats":
        return {"universe": "unenumerated"}
    return {}


def _iso(moment):
    return None if moment is None else moment.replace(microsecond=0).isoformat() + "Z"


def configure(store, tenant, account, spec, *, expected_revision, command_key, actor, reason):
    """Start or raise the explicit history import for a configured preparation account.

    Idempotent per command key, revision fenced, and budgets only ever rise: spent reads are evidence.
    """
    from conversation_engine import preparation as prep
    prep._command(command_key, actor, reason)
    spec = _shape(spec)
    _int(expected_revision, 0, 2**31 - 1)
    request_hash = prep.fingerprint(["history-import", tenant, account, spec, expected_revision, actor, reason])
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = s.get(OfLaunchCommand, (account, command_key))
        if prior is not None:
            if prior.kind != "history_import" or prior.request_hash != request_hash:
                raise Refused("history import command conflict")
            return {**json.loads(prior.result_json), "replayed": True}
        scope, pconfig = prep.current(s, runtime, lock=True)
        if scope is None or scope.status != "configured":
            raise Refused("history import requires a configured preparation")
        limits = pconfig["limits"]
        rows = {r.source: r for r in s.scalars(select(OfFansImportSource).where(
            OfFansImportSource.account_id == account).with_for_update())}
        revision = max((r.revision for r in rows.values()), default=0)
        if revision != expected_revision:
            raise Refused("history import revision conflict")
        per = limits["provider_credits_per_request"]
        wanted = spec["sources"]
        if any(v["credits"] < per for v in wanted.values()):
            raise Refused("a source credit budget must cover at least one request")
        imported = (sum(r.requests_used for r in rows.values()), sum(r.credits_reserved for r in rows.values()))
        headroom = (limits["provider_requests"] - (scope.provider_requests_used - imported[0]),
                    limits["provider_credit_budget"] - (scope.provider_credits_reserved - imported[1]))
        if (sum(v["requests"] for v in wanted.values()) > headroom[0]
                or sum(v["credits"] for v in wanted.values()) > headroom[1]):
            raise Refused("source budgets exceed the account read ceiling")
        now = utcnow()
        before = {} if not rows else _budget_view(rows)
        if rows:
            if json.loads(rows["transactions"].cursor_json)["start_date"] != spec["start_date"]:
                raise Refused("history import start date is retained")
            held = json.loads(rows["vault_window"].evidence_json)
            if held["webhooks"] is not None and spec["webhooks"] != {"reference": held["webhooks"]["reference"]}:
                raise Refused("webhook attestation is retained")
            for source, new in wanted.items():
                old = rows[source]
                if new["requests"] < old.requests_budget or new["credits"] < old.credits_budget:
                    raise Refused("history source budgets only increase")
            changed = (spec["sources"] != held["spec"]["sources"] or (held["webhooks"] is None) != (spec["webhooks"] is None))
            if changed:
                revision += 1
                for source, new in wanted.items():
                    old = rows[source]
                    old.requests_budget, old.credits_budget, old.min_interval_seconds = new["requests"], new["credits"], new["min_interval_seconds"]
                    old.revision, old.updated_at = revision, now
                    if old.last_error == "source_budget_exhausted":
                        old.last_error = None
                evidence = {"spec": spec, "webhooks": held["webhooks"] or _attestation(spec, actor, now)}
                rows["vault_window"].evidence_json = _encoded(evidence)
        else:
            revision = 1
            for source in SOURCES:
                want = wanted[source]
                s.add(OfFansImportSource(account_id=account, source=source, state="not_started", revision=1, boundary_at=now,
                    cursor_json=_encoded(_initial_cursor(source, spec)), requests_budget=want["requests"],
                    credits_budget=want["credits"], min_interval_seconds=want["min_interval_seconds"],
                    evidence_json=_encoded({"spec": spec, "webhooks": _attestation(spec, actor, now)}) if source == "vault_window" else "{}",
                    created_at=now, updated_at=now))
            s.flush()
            rows = {r.source: r for r in s.scalars(select(OfFansImportSource).where(OfFansImportSource.account_id == account))}
        result = {"configured": True, "revision": revision, "sources": _budget_view(rows)}
        s.add(OfLaunchCommand(account_id=account, command_key=command_key, binding_id=runtime.binding_id, kind="history_import",
            request_hash=request_hash, actor=actor, reason=reason, before_json=_encoded(before), result_json=_encoded(result),
            created_at=now))
        return {**result, "replayed": False}


def _attestation(spec, actor, now):
    hooks = spec["webhooks"]
    return None if hooks is None else {"reference": hooks["reference"], "attestedBy": actor, "at": _iso(now)}


def _budget_view(rows):
    return {name: {"state": r.state, "budget": {"requests": r.requests_budget, "credits": r.credits_budget,
                   "requestsUsed": r.requests_used, "creditsReserved": r.credits_reserved},
                   "minIntervalSeconds": r.min_interval_seconds} for name, r in sorted(rows.items())}


def started(s, account):
    return s.scalar(select(OfFansImportSource.source).where(OfFansImportSource.account_id == account).limit(1)) is not None


def enlist_fan(store, account, fan_id, *, priority=0):
    """List a fan for history reading, e.g. when he messages. A completed cursor is never reopened here."""
    if type(fan_id) is not int or not 0 < fan_id < 2**63 or type(priority) is not int or not 0 <= priority <= 3:
        raise Refused("invalid fan listing")
    now = utcnow()
    with Session(store.engine) as s, s.begin():
        if not started(s, account):
            raise Refused("history import is not configured")
        insert = pg_insert(OfFansHistoryCursor).values(account_id=account, fan_id=fan_id, status="needed", priority=priority,
            created_at=now, updated_at=now)
        s.execute(insert.on_conflict_do_update(index_elements=["account_id", "fan_id"],
            set_={"priority": text_least(OfFansHistoryCursor.priority, priority), "updated_at": now},
            where=OfFansHistoryCursor.status != "complete"))


def text_least(column, value):
    from sqlalchemy import func
    return func.least(column, value)


# ---------------------------------------------------------------- guard hooks (called inside the guard transaction)
def validate_read(s, account, config, parsed):
    """Classify an allowlisted history read and validate its exact shape. None means: not a history path."""
    acct = config["provider_account"]
    path = parsed.path
    fan = None
    found = re.fullmatch("/api/" + re.escape(acct) + r"/chats/([0-9]{1,19})/messages", path)
    if found:
        source, fan = "chats", int(found.group(1))
        if not 0 < fan < 2**63:
            raise Refused("history fan refused")
    elif path == "/api/" + acct + "/transactions":
        source = "transactions"
    elif path == "/api/" + acct + "/chargebacks":
        source = "chargebacks"
    else:
        return None
    pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=12)
    query = dict(pairs)
    if len(query) != len(pairs):
        raise Refused("history query refused")
    row = s.get(OfFansImportSource, (account, source))
    if row is None:
        raise Refused("history import is not configured")
    start = json.loads(row.cursor_json).get("start_date")
    limit = query.get("limit")
    if limit is not None and (not limit.isdecimal() or not 1 <= int(limit) <= PAGE_SIZE):
        raise Refused("history page size refused")
    if source == "chats":
        if not set(query) <= _CHAT_KEYS or any(k in query and not _NUMERIC.fullmatch(query[k]) for k in ("first_id", "last_id", "id")):
            raise Refused("history query refused")
        if query.get("order", "desc") not in ("asc", "desc") or query.get("skip_users", "all") not in ("all", "none"):
            raise Refused("history query refused")
        if s.get(OfFansHistoryCursor, (account, fan)) is None:
            raise Refused("history fan is not listed")
    elif source == "transactions":
        if not set(query) <= {"limit", "startDate", "marker"} or query.get("startDate") != start or len(query.get("marker", "")) > 512:
            raise Refused("history query refused")
    else:
        offset = query.get("offset", "0")
        if (not set(query) <= {"limit", "offset", "start_date"} or query.get("start_date") != start
                or not offset.isdecimal() or not 0 <= int(offset) <= 1_000_000):
            raise Refused("history query refused")
    return source


def reserve(s, account, source, credits, now):
    """Charge one request to the source sub-budget and the hourly meter (same transaction as the scope counters).

    A vault read before any import was configured has no source row and stays account-level only.
    """
    row = s.get(OfFansImportSource, (account, source), with_for_update=True)
    if row is None:
        if source == "vault_window":
            return
        raise Refused("history import is not configured")
    if row.requests_used + 1 > row.requests_budget or row.credits_reserved + credits > row.credits_budget:
        from conversation_engine.preparation import SourceBudgetExhausted
        raise SourceBudgetExhausted(source)
    row.requests_used += 1
    row.credits_reserved += credits
    row.updated_at = now
    hour = now.replace(minute=0, second=0, microsecond=0)
    insert = pg_insert(OfFansReadMeter).values(account_id=account, source=source, hour=hour, requests=1,
        credits_reserved=credits, updated_at=now)
    s.execute(insert.on_conflict_do_update(index_elements=["account_id", "source", "hour"], set_={
        "requests": OfFansReadMeter.requests + 1, "credits_reserved": OfFansReadMeter.credits_reserved + credits,
        "updated_at": now}))


# ---------------------------------------------------------------- planning
_ORDER = {"transactions": 0, "chargebacks": 1, "chats": 2}
_EPOCH = datetime(1970, 1, 1)


def _row_time(item):
    """Best-effort provider timestamp of one row (naive UTC), or None. Used only for counters, never for state."""
    from datetime import timezone
    try:
        node = item
        if type(node) is dict and type(node.get("payment")) is dict:
            node = {**node["payment"], **{k: v for k, v in node.items() if k != "payment"}}
        value = node.get("createdAt") or node.get("created_at")
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except (AttributeError, TypeError, ValueError):
        return None


def _discover(s, account, now):
    """List every fan we already know locally or whom payment evidence names, once, with a reading priority.

    There is no verified provider chat-list call, so the universe is never claimed to be enumerated; a fan who
    has not messaged and has no payment is simply not listed and stays unknown.
    """
    from datetime import timedelta
    from sqlalchemy import and_, exists, update
    from db.models import Fan, Message
    from db.onlyfans_fans_intake_models import OfFansEvidence
    cur, evidence = OfFansHistoryCursor, OfFansEvidence
    listed = exists().where(and_(cur.account_id == account, cur.fan_id == Fan.fan_id))
    new = {r[0] for r in s.execute(select(Fan.fan_id).where(Fan.account_id == account, ~listed).limit(5000))}
    payer_listed = exists().where(and_(cur.account_id == account, cur.fan_id == evidence.fan_id))
    payers = {r[0] for r in s.execute(select(evidence.fan_id).where(evidence.account_id == account,
        evidence.kind.in_(("unlock", "transaction", "chargeback")), ~payer_listed).distinct().limit(5000))}
    since = now - timedelta(days=1)
    live = select(Message.fan_id).where(Message.account_id == account, Message.direction == "in", Message.created_at >= since)
    if new | payers:
        live_now = {r[0] for r in s.execute(live.where(Message.fan_id.in_(new | payers)).distinct())}
        values = [{"account_id": account, "fan_id": fan, "status": "needed", "created_at": now, "updated_at": now,
                   "priority": 0 if fan in live_now else 2 if fan in payers else 3} for fan in sorted(new | payers)]
        for start in range(0, len(values), 1000):
            s.execute(pg_insert(cur).values(values[start:start + 1000]).on_conflict_do_nothing(index_elements=["account_id", "fan_id"]))
    s.execute(update(cur).where(cur.account_id == account, cur.status.in_(("needed", "running")), cur.priority > 0,
        cur.fan_id.in_(live.distinct())).values(priority=0, updated_at=now))


def _blocked(s, account, source):
    return s.scalar(select(OfFansImportGap.gap_id).where(OfFansImportGap.account_id == account, OfFansImportGap.source == source,
        OfFansImportGap.kind == "page", OfFansImportGap.exhausted.is_(True), OfFansImportGap.resolved_at.is_(None)).limit(1)) is not None


def _plan(s, account, now):
    """(ready work items, due times being waited for). Pure of provider I/O; lists newly known fans."""
    from sqlalchemy import func, or_
    rows = {r.source: r for r in s.scalars(select(OfFansImportSource).where(OfFansImportSource.account_id == account,
        OfFansImportSource.source.in_(HISTORY_SOURCES)))}
    if len(rows) != len(HISTORY_SOURCES):
        return [], []
    _discover(s, account, now)
    ready, waits = [], []
    cur = OfFansHistoryCursor
    for source in HISTORY_SOURCES:
        row = rows[source]
        item = {"source": source, "order": _ORDER[source], "paced": row.last_read_at or _EPOCH, "retries": row.retry_attempts}
        if source == "chats":
            pending = s.scalar(select(cur.fan_id).where(cur.account_id == account, cur.status.in_(("needed", "running"))).limit(1)) is not None
        else:
            state = json.loads(row.cursor_json)
            pending = not state.get("end_reached") and not _blocked(s, account, source)
        if not pending:
            continue
        if row.requests_used >= row.requests_budget or row.last_error == "source_budget_exhausted":
            # Visible, not a failure: only a higher explicit budget (configure) clears it.
            row.last_error = "source_budget_exhausted"
            continue
        if row.next_read_at is not None and row.next_read_at > now:
            waits.append(row.next_read_at)
            continue
        if source == "chats":
            choice = s.scalar(select(cur).where(cur.account_id == account, cur.status.in_(("needed", "running")),
                or_(cur.retry_not_before.is_(None), cur.retry_not_before <= now)).order_by(cur.priority, cur.fan_id).limit(1))
            if choice is None:
                waits.append(s.scalar(select(func.min(cur.retry_not_before)).where(cur.account_id == account,
                    cur.status.in_(("needed", "running")), cur.retry_not_before > now)))
                continue
            item.update(fan=choice.fan_id, next_page=choice.next_page, retries=choice.retry_attempts, live=choice.priority == 0)
        else:
            item.update(state=state, live=False)
        ready.append(item)
    return ready, [w for w in waits if w is not None]


def eligibility(s, account, now=None):
    """'ready' (a read is due), 'delayed' (waiting for a persisted due time) or 'idle' (nothing automatic is left)."""
    now = now or utcnow()
    ready, waits = _plan(s, account, now)
    if ready:
        return {"state": "ready", "at": None}
    if waits:
        return {"state": "delayed", "at": min(waits)}
    return {"state": "idle", "at": None}


def _next_work(store, account, now):
    with Session(store.engine) as s, s.begin():
        ready, waits = _plan(s, account, now)
    if not ready:
        return None
    # A fan in live conversation goes first; otherwise the source read longest ago takes its turn.
    ready.sort(key=lambda w: (0 if w.get("live") else 1, w["paced"], w["order"]))
    return ready[0]


# ---------------------------------------------------------------- reading one page
def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _clean(text):
    return re.sub(r"[^a-z0-9 _:.\-]", "", str(text).lower())[:96]


def _ref(work):
    source = work["source"]
    if source == "chats":
        return work["next_page"] or "first"
    state = work["state"]
    return "marker:%s" % state["marker"] if source == "transactions" and state.get("marker") else \
        "offset:%d" % state["offset"] if source == "chargebacks" else "first"


def _page_gap_id(source, fan, ref):
    return sha256(("page|%s|%s|%s" % (source, fan or "", ref)).encode()).hexdigest()[:40]


def _classify_projection(status, detail):
    if status == "held":
        return "conflict"
    if status == "projected":
        return "dup" if str(detail).endswith("_duplicate") else "new"
    return "ignored"


def _ingest(intake, tenant, account, work, item):
    """One row through the existing owner. Returns new|dup|conflict|ignored; raises for a row to quarantine."""
    from fans_api_adapter import payment_evidence as ev
    from fans_api_adapter.events import IntakeBackpressure
    source = work["source"]
    if source == "chats":
        if type(item) is not dict:
            raise ValueError("row is not an object")
        detail = []
        outcome = intake.ingest_history_item(tenant, account, str(work["fan"]), item, detail=detail)
        recorded = detail[0] if detail else outcome
        if recorded == "backpressure":
            raise IntakeBackpressure("history intake queue is full")
        return "dup" if recorded == "duplicate" else "new"
    if source == "transactions":
        if type(item) is not dict or type(item.get("type")) is not str:
            raise ValueError("row has no type")
        ev.transaction_id(item.get("id"))
        return _classify_projection(*intake.ingest_transaction_row(account, item))
    if type(item) is not dict:
        raise ValueError("row is not an object")
    return _classify_projection(*intake.ingest_chargeback_row(account, item))


def _drain(store, tenant, account):
    """Apply what history ingestion queued, so the shared ingress bound does not stall a long import."""
    for _ in range(50):
        try:
            if store.drain(tenant, account) < 100:
                return
        except Refused:
            return


def _meter(s, account, source, now, *, outcome=None, rows=0, balance=None):
    """Record the outcome of the request the guard just reserved, in that request's (latest) hour row.

    An outcome is only ever attached to an existing request: a refused read made none, so nothing is recorded.
    A reserved request with no recorded outcome is what the view reports as dispatched-unrecorded.
    """
    row = s.scalar(select(OfFansReadMeter).where(OfFansReadMeter.account_id == account, OfFansReadMeter.source == source)
                   .order_by(OfFansReadMeter.hour.desc()).limit(1).with_for_update())
    if row is None:
        return
    if outcome and row.ok + row.rate_limited + row.failed < row.requests:
        setattr(row, outcome, getattr(row, outcome) + 1)
        row.rows_read += rows
        if balance is not None:
            row.balance_first = balance if row.balance_first is None else row.balance_first
            row.balance_last = balance
    row.updated_at = now


def _upsert_page_gap(s, account, work, *, reason, status, attempts, exhausted, now):
    ref = _ref(work)
    fan = work.get("fan")
    insert = pg_insert(OfFansImportGap).values(account_id=account, gap_id=_page_gap_id(work["source"], fan, ref),
        source=work["source"], fan_id=fan, kind="page", cursor_ref=ref, reason_code=reason, http_status=status,
        attempts=attempts, exhausted=exhausted, created_at=now, updated_at=now)
    s.execute(insert.on_conflict_do_update(index_elements=["account_id", "gap_id"], set_={"reason_code": reason,
        "http_status": status, "attempts": attempts, "exhausted": exhausted, "updated_at": now, "resolved_at": None,
        "resolved_by": None, "resolution": None}))


def _lock(s, account, work):
    source = s.get(OfFansImportSource, (account, work["source"]), with_for_update=True)
    cursor = s.get(OfFansHistoryCursor, (account, work["fan"]), with_for_update=True) if work["source"] == "chats" else None
    return source, cursor


def _fail(store, guard, client, account, work, *, failure, cause, status, retry_after, now):
    """Persist one failed page: the cursor is untouched and the page is retained as a gap with its due time."""
    from fans_api_adapter.vault_sync import MAX_RETRY_ATTEMPTS, retry_delay
    with Session(store.engine) as s, s.begin():
        try:
            guard.ensure_current(s)
        except Refused:
            return {"outcome": "authority_changed", "stop": True}
        source, cursor = _lock(s, account, work)
        holder = cursor or source
        prior = s.get(OfFansImportGap, (account, _page_gap_id(work["source"], work.get("fan"), _ref(work))))
        attempts = (prior.attempts if prior is not None and prior.resolved_at is None else 0) + 1
        exhausted = failure in ("schema", "refused") or (failure == "transient" and attempts >= MAX_RETRY_ATTEMPTS)
        _upsert_page_gap(s, account, work, reason=cause, status=status, attempts=attempts, exhausted=exhausted, now=now)
        source.pages_failed += 1
        source.updated_at = now
        due = None
        if failure == "transient" and not exhausted:
            due = now + _delta(retry_delay(attempts, retry_after))
        holder.retry_attempts = attempts
        if cursor is not None:
            cursor.last_error, cursor.updated_at = cause, now
            cursor.retry_not_before = due
            if exhausted:
                cursor.status = "gapped"
        else:
            source.last_error = cause
            source.next_read_at = due if failure == "transient" else source.next_read_at
        if failure != "refused":
            _meter(s, account, work["source"], now, outcome="rate_limited" if cause == "provider_rate_limited" else "failed",
                   balance=client.credits_balance)
    return {"outcome": "failed", "failure": failure, "cause": cause, "stop": failure == "auth", "exhausted": exhausted}


def _delta(seconds):
    from datetime import timedelta
    return timedelta(seconds=seconds)


def _commit(store, tenant, account, guard, client, work, result, now):
    """Ingest one observed page through the existing owners, then advance the cursor and counters in one commit."""
    from fans_api_adapter.events import FansEventIntake, IntakeBackpressure
    source_name = work["source"]
    with Session(store.engine) as s, s.begin():
        try:
            guard.ensure_current(s)
        except Refused:
            return {"outcome": "authority_changed", "stop": True}
        boundary = s.get(OfFansImportSource, (account, source_name)).boundary_at
    if source_name == "chargebacks":
        body = result.data
        items = body.get("data", {}).get("list") if type(body) is dict and type(body.get("data")) is dict else None
        if type(items) is not list or len(items) > PAGE_SIZE:
            return _fail(store, guard, client, account, work, failure="schema", cause="provider_response_invalid",
                         status=result.http_status, retry_after=None, now=now)
    else:
        items = list(result.data)
    _drain(store, tenant, account)
    intake = FansEventIntake(store)
    counts = {"new": 0, "dup": 0, "conflict": 0, "ignored": 0, "quarantined": 0}
    late, quarantined, newest, oldest = 0, [], None, None
    for item in items:
        stamp = _row_time(item)
        if stamp is not None and stamp >= boundary:
            late += 1
        try:
            outcome = _ingest(intake, tenant, account, work, item)
        except IntakeBackpressure:
            return _backpressure(store, guard, account, work, now)
        except (Refused, ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
            reason = "row_refused" if isinstance(exc, Refused) else "row_malformed"
            detail = _clean(exc) if isinstance(exc, Refused) else type(exc).__name__.lower()
            quarantined.append({"gap": sha256(("row|%s|%s|%s" % (source_name, work.get("fan") or "", _digest(item))).encode()).hexdigest()[:40],
                                "reason": reason, "detail": detail, "raw": _digest(item)})
            outcome = "quarantined"
        counts[outcome] += 1
        if source_name == "chats" and type(item) is dict:
            ident = item.get("id")
            if type(ident) is int and (newest is None or ident > newest):
                newest = ident
            if stamp is not None and (oldest is None or stamp < oldest):
                oldest = stamp
    _drain(store, tenant, account)
    done = utcnow()
    with Session(store.engine) as s, s.begin():
        try:
            guard.ensure_current(s)
        except Refused:
            return {"outcome": "authority_changed", "stop": True}
        source, cursor = _lock(s, account, work)
        ref = _ref(work)
        for q in quarantined:
            inserted = s.execute(pg_insert(OfFansImportGap).values(account_id=account, gap_id=q["gap"], source=source_name,
                fan_id=work.get("fan"), kind="row", cursor_ref=ref, reason_code=q["reason"], detail=q["detail"],
                raw_sha256=q["raw"], attempts=0, exhausted=False, created_at=done, updated_at=done)
                .on_conflict_do_nothing(index_elements=["account_id", "gap_id"])).rowcount
            if not inserted:                               # already retained from an earlier read of this row
                counts["quarantined"] -= 1
                counts["dup"] += 1
        source.rows_seen += len(items)
        source.rows_new += counts["new"]
        source.rows_duplicate += counts["dup"]
        source.rows_conflict += counts["conflict"]
        source.rows_ignored += counts["ignored"]
        source.rows_quarantined += counts["quarantined"]
        source.rows_late += late
        source.pages_ok += 1
        source.last_read_at, source.updated_at = done, done
        source.retry_attempts, source.last_error = 0, None
        interval = source.min_interval_seconds
        source.next_read_at = done + _delta(interval) if interval else None
        if source.state == "not_started":
            source.state = "running"
        end = False
        if cursor is not None:
            cursor.pages += 1
            cursor.messages_seen += len(items)
            cursor.first_page_at = cursor.first_page_at or done
            if newest is not None and (cursor.newest_message_id is None or newest > cursor.newest_message_id):
                cursor.newest_message_id = newest
            if oldest is not None and (cursor.oldest_message_at is None or oldest < cursor.oldest_message_at):
                cursor.oldest_message_at = oldest
            cursor.retry_attempts, cursor.retry_not_before, cursor.last_error, cursor.updated_at = 0, None, None, done
            if result.next_page is None:
                cursor.status, cursor.completed_at, cursor.next_page, end = "complete", done, None, True
            else:
                cursor.status, cursor.next_page = "running", result.next_page
                if cursor.pages >= MAX_PAGES["chats"]:
                    cursor.status = "gapped"
                    _upsert_page_gap(s, account, {**work, "next_page": result.next_page}, reason="page_limit", status=None,
                                     attempts=0, exhausted=True, now=done)
        else:
            state = json.loads(source.cursor_json)
            state["first_page_at"] = state.get("first_page_at") or _iso(done)
            if source_name == "transactions":
                state["marker"], state["end_reached"] = result.next_page, result.next_page is None
            else:
                state["end_reached"] = len(items) < PAGE_SIZE
                if not state["end_reached"]:
                    state["offset"] += PAGE_SIZE
            end = state["end_reached"]
            if not end and source.pages_ok >= MAX_PAGES[source_name]:
                _upsert_page_gap(s, account, {**work, "state": state}, reason="page_limit", status=None, attempts=0, exhausted=True, now=done)
            if end:
                source.completed_at = done
            source.cursor_json = _encoded(state)
        from sqlalchemy import update
        s.execute(update(OfFansImportGap).where(OfFansImportGap.account_id == account,
            OfFansImportGap.gap_id == _page_gap_id(source_name, work.get("fan"), ref), OfFansImportGap.resolved_at.is_(None))
            .values(resolved_at=done, resolved_by="retry_succeeded", resolution="retry_succeeded", updated_at=done))
        _meter(s, account, source_name, done, outcome="ok", rows=len(items), balance=client.credits_balance)
    return {"outcome": "ok", "rows": len(items), "end": end, "quarantined": counts["quarantined"]}


def _backpressure(store, guard, account, work, now):
    with Session(store.engine) as s, s.begin():
        try:
            guard.ensure_current(s)
        except Refused:
            return {"outcome": "authority_changed", "stop": True}
        source, cursor = _lock(s, account, work)
        holder = cursor or source
        due = now + _delta(30)
        if cursor is not None:
            cursor.retry_not_before = due
        else:
            source.next_read_at = due
        holder.last_error = "intake_backpressure"
        holder.updated_at = now
        _meter(s, account, work["source"], now, outcome="ok")
    return {"outcome": "backpressure", "stop": True}


def _read(store, tenant, account, client, guard, work, now):
    from conversation_engine.preparation import AuthorityChanged, BudgetExhausted, SourceBudgetExhausted
    source = work["source"]
    try:
        if source == "chats":
            result = client.history(str(work["fan"]), limit=PAGE_SIZE, next_page=work["next_page"], lenient_rows=True)
        elif source == "transactions":
            state = work["state"]
            result = client.transactions(limit=PAGE_SIZE, marker=state["marker"], start_date=state["start_date"], lenient_rows=True)
        else:
            state = work["state"]
            result = client.chargebacks(limit=PAGE_SIZE, offset=state["offset"], start_date=state["start_date"])
    except AuthorityChanged:
        return {"outcome": "authority_changed", "stop": True}
    except SourceBudgetExhausted:
        with Session(store.engine) as s, s.begin():
            s.get(OfFansImportSource, (account, source), with_for_update=True).last_error = "source_budget_exhausted"
        return {"outcome": "source_budget_exhausted", "stop": False}
    except BudgetExhausted:
        return {"outcome": "budget_exhausted", "failure": "budget", "stop": True}
    except Refused:
        return _fail(store, guard, client, account, work, failure="refused", cause="request_refused", status=None, retry_after=None, now=now)
    except ValueError:
        return _fail(store, guard, client, account, work, failure="refused", cause="continuation_invalid", status=None, retry_after=None, now=now)
    if result.state not in ("observed", "unverified_observation"):
        return _fail(store, guard, client, account, work, failure=result.failure or "transient",
                     cause=result.cause or "provider_unavailable", status=result.http_status, retry_after=result.retry_after, now=now)
    return _commit(store, tenant, account, guard, client, work, result, now)


def step(store, tenant, account, client, *, pages=1):
    """Read at most ``pages`` history pages for one account through the preparation guard.

    Called by prepare_one after the vault step, under the same per-account advisory lock. It never sends and
    has no model path. Returns a small fixed-vocabulary report; the durable state is in the import tables.
    """
    from conversation_engine.preparation import PreparationRequestGuard
    from fans_api_adapter.protocol import FansApiClient
    guard = getattr(client, "_request_guard", None)
    if (type(client) is not FansApiClient or not client._identity_verified or type(guard) is not PreparationRequestGuard
            or guard.store is not store or (guard.tenant, guard.account) != (tenant, account)
            or type(pages) is not int or not 1 <= pages <= 10):
        raise Refused("budgeted preparation guard required")
    report = {"pages": 0, "reads": [], "failure": None, "cause": None, "pending": False}
    for _ in range(pages):
        now = utcnow()
        work = _next_work(store, account, now)
        if work is None:
            break
        outcome = _read(store, tenant, account, client, guard, work, now)
        report["reads"].append({"source": work["source"], "fan": work.get("fan"), "outcome": outcome["outcome"]})
        if outcome["outcome"] in ("ok", "failed"):
            report["pages"] += 1
        if outcome.get("failure") in ("auth", "budget") or outcome["outcome"] == "authority_changed":
            report["failure"] = "authority" if outcome["outcome"] == "authority_changed" else outcome["failure"]
            report["cause"] = outcome.get("cause")
        if outcome.get("stop"):
            break
    with Session(store.engine) as s, s.begin():
        report["pending"] = eligibility(s, account)["state"] != "idle"
    return report


# ---------------------------------------------------------------- operator commands on retained gaps
def _gap_command(store, tenant, account, gap_id, *, command_key, actor, reason, kind, action):
    from conversation_engine import preparation as prep
    prep._command(command_key, actor, reason)
    if type(gap_id) is not str or not re.fullmatch(r"[0-9a-f]{40}", gap_id):
        raise Refused("invalid gap")
    request_hash = prep.fingerprint([action, tenant, account, gap_id, actor, reason])
    now = utcnow()
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        prior = s.get(OfLaunchCommand, (account, command_key))
        if prior is not None:
            if prior.kind != "history_gap" or prior.request_hash != request_hash:
                raise Refused("history gap command conflict")
            return {**json.loads(prior.result_json), "replayed": True}
        gap = s.get(OfFansImportGap, (account, gap_id), with_for_update=True)
        if gap is None or gap.kind != kind or gap.resolved_at is not None:
            raise Refused("no open gap of that kind")
        before = {"gap": gap_id, "attempts": gap.attempts, "exhausted": gap.exhausted}
        if action == "retry":
            gap.attempts, gap.exhausted, gap.updated_at = 0, False, now
            if gap.source == "chats":
                cursor = s.get(OfFansHistoryCursor, (account, gap.fan_id), with_for_update=True)
                if cursor.status == "gapped":
                    cursor.status = "running" if cursor.pages else "needed"
                cursor.retry_attempts, cursor.retry_not_before, cursor.last_error, cursor.updated_at = 0, None, None, now
            else:
                source = s.get(OfFansImportSource, (account, gap.source), with_for_update=True)
                source.retry_attempts, source.next_read_at, source.last_error, source.updated_at = 0, None, None, now
            outcome = "reopened"
        else:
            gap.resolved_at, gap.resolved_by, gap.resolution, gap.updated_at = now, actor, "reviewed", now
            outcome = "reviewed"
        result = {"gapId": gap_id, "source": gap.source, "kind": gap.kind, "outcome": outcome}
        s.add(OfLaunchCommand(account_id=account, command_key=command_key, binding_id=runtime.binding_id, kind="history_gap",
            request_hash=request_hash, actor=actor, reason=reason, before_json=_encoded(before), result_json=_encoded(result),
            created_at=now))
        return {**result, "replayed": False}


def retry_gap(store, tenant, account, gap_id, *, command_key, actor, reason):
    """Explicitly re-open a retained failed page after the provider or integration was fixed."""
    return _gap_command(store, tenant, account, gap_id, command_key=command_key, actor=actor, reason=reason, kind="page", action="retry")


def dismiss_row_gap(store, tenant, account, gap_id, *, command_key, actor, reason):
    """A person reviewed one quarantined row. The row and the review stay on record; a page gap cannot be dismissed."""
    return _gap_command(store, tenant, account, gap_id, command_key=command_key, actor=actor, reason=reason, kind="row", action="dismiss")


def record_vault_outcome(store, account, result):
    """Meter the outcome of the vault page the preparation pass just attempted (request counts come from the guard)."""
    error = result.get("error")
    if error in (None, "page_budget_exhausted") and result.get("pages", 0) > 0:
        outcome = "ok"
    elif error in ("vault_read_failed", "vault_retry_exhausted") and result.get("failure") in ("transient", "auth", "schema"):
        outcome = "rate_limited" if result.get("cause") == "provider_rate_limited" else "failed"
    else:
        return
    with Session(store.engine) as s, s.begin():
        if s.get(OfFansImportSource, (account, "vault_window")) is not None:
            _meter(s, account, "vault_window", utcnow(), outcome=outcome, rows=result.get("upserted", 0))
