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
