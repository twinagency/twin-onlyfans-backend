"""Durable concurrent admission shared by manual, campaign, engagement and native text intents.

The caller already holds the account policy row lock (``SELECT ... FOR UPDATE``), so these
reads and writes are one short account transaction. No network call runs inside it.
The three additive tables live in ``fastt_runtime`` and are created by the native runtime
schema owner; the text path refuses concurrent mode if they are unavailable rather than
silently falling back to a single account-wide slot.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from native_pilot import admission


def process_send_concurrency() -> int:
    return admission.bounded_int(os.environ.get("FASTT_SEND_CONCURRENCY"), 4, 1, 32)


def process_dispatch_lease_s() -> float:
    return admission.bounded_float(os.environ.get("FASTT_DISPATCH_LEASE_S"), 120.0, 30.0, 900.0)


def process_conversation_gap_s() -> float:
    return admission.bounded_float(os.environ.get("FASTT_CONVERSATION_MIN_GAP_S"), 0.0, 0.0, 3600.0)


def _iso(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(sep=" ", timespec="microseconds")


def _dt(raw):
    value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def is_after(raw, now: datetime) -> bool:
    """Compare a stored timestamp with the caller's clock in UTC-naive form."""
    return raw is not None and _dt(raw) > _dt(now)


def _prefix(session) -> str:
    try:
        return "fastt_runtime." if session.bind.dialect.name == "postgresql" else ""
    except Exception:
        return "fastt_runtime."


def _has_table(session, name: str) -> bool:
    try:
        if session.bind.dialect.name == "postgresql":
            return session.execute(text("SELECT to_regclass(:name)"), {"name": name}).scalar() is not None
        return session.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
                               {"name": name}).first() is not None
    except Exception:
        return False


def daily_used(session, account_id: str, now: datetime) -> int:
    """Count admitted provider messages across native and text delivery paths.

    The caller holds the account policy row lock. This is the shared rolling
    24-hour counter used by manual/campaign admission; native admission applies
    the same two-table count in ``Ledger._account_daily_used``.
    """
    now = _dt(now)
    cutoff = _iso(now - timedelta(hours=24))
    now_s = _iso(now)
    total = 0
    if _has_table(session, "of_text_intent"):
        total += int(session.execute(text(
            "SELECT count(*) FROM of_text_intent WHERE account_id=:a "
            "AND status IN ('dispatching','accepted','unknown') "
            "AND COALESCE(dispatched_at,created_at)>:cutoff "
            "AND COALESCE(dispatched_at,created_at)<=:now"),
            {"a": account_id, "cutoff": cutoff, "now": now_s}).scalar() or 0)
    t = _prefix(session)
    native_receipts = f"{t}native_pilot_receipts"
    native_attempts = f"{t}twin_bridge_send_attempts"
    if _has_table(session, native_receipts) and _has_table(session, native_attempts):
        total += int(session.execute(text(
            f"SELECT count(*) FROM {native_receipts} r JOIN {native_attempts} a ON a.id=r.attempt_id "
            "WHERE a.account_id=:a AND r.created_at>:cutoff AND r.created_at<=:now "
            "AND r.state IN ('dispatching','sent','delivery_unknown')"),
            {"a": account_id, "cutoff": cutoff, "now": now_s}).scalar() or 0)
    return total


def _ensure_sqlite(session) -> None:
    if _prefix(session):
        return
    for statement in (
        "CREATE TABLE IF NOT EXISTS engine_conversation_lease("
        "tenant_id TEXT NOT NULL,account_id TEXT NOT NULL,fan_id INTEGER NOT NULL,holder TEXT NOT NULL,"
        "owner_token TEXT NOT NULL,attempt_id TEXT,state TEXT NOT NULL,acquired_at TEXT NOT NULL,"
        "heartbeat_at TEXT NOT NULL,expires_at TEXT NOT NULL,next_send_at TEXT,updated_at TEXT NOT NULL,"
        "PRIMARY KEY(tenant_id,account_id,fan_id))",
        "CREATE TABLE IF NOT EXISTS engine_dispatch_slot(slot_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,"
        "account_id TEXT NOT NULL,fan_id INTEGER NOT NULL,attempt_id TEXT,state TEXT NOT NULL,"
        "admitted_at TEXT NOT NULL,expires_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS engine_provider_bucket(account_id TEXT PRIMARY KEY,tokens TEXT NOT NULL,"
        "updated_at TEXT NOT NULL)",
    ):
        session.execute(text(statement))


def mode(policy) -> str:
    return admission.delivery_mode(policy)


def admit(session, tenant_id: str, policy, *, account_id: str, fan_id: int, holder: str,
          attempt_id: str, now: datetime, send_concurrency: int = 4,
          dispatch_lease_s: float = 120.0) -> tuple[str | None, str | None]:
    """Reserve one conversation and one provider slot.

    Returns ``(slot_id, None)`` or ``(None, reason)``. A provider rate/concurrency refusal
    is transient (the caller leaves the intent queued); a conversation ownership conflict is
    also transient. Limits are checked by the caller before this function.
    """
    if not admission.is_concurrent(policy):
        return None, None
    _ensure_sqlite(session)
    t = _prefix(session)
    required = (
        f"{t}engine_conversation_lease",
        f"{t}engine_dispatch_slot",
        f"{t}engine_provider_bucket",
    )
    if not all(_has_table(session, name) for name in required):
        # Keep the intent queued. Never silently fall back to the account-wide
        # legacy sender when an operator enabled concurrent mode.
        return None, "concurrent_schema_unavailable"
    now = _dt(now)
    now_s = _iso(now)
    cooldown = admission.cooldown_at(policy)
    if cooldown is not None and _dt(cooldown) > now:
        return None, "account_throttled"
    cap = getattr(policy, "daily_limit_units", None)
    if cap is not None and daily_used(session, account_id, now) >= int(cap):
        return None, "account_daily_cap"
    unresolved = session.execute(text(
        f"SELECT * FROM {t}engine_conversation_lease "
        "WHERE tenant_id=:t AND account_id=:a AND fan_id=:f"),
        {"t": tenant_id, "a": account_id, "f": fan_id}).mappings().first()
    if unresolved is not None:
        if unresolved["state"] == "unresolved":
            return None, "delivery_unresolved"
        if unresolved["state"] == "held" and _dt(unresolved["expires_at"]) > now \
                and unresolved["holder"] != holder:
            return None, "conversation_busy"
        next_send = session.execute(text(
            f"SELECT next_send_at FROM {t}engine_conversation_lease "
            "WHERE tenant_id=:t AND account_id=:a AND fan_id=:f"),
            {"t": tenant_id, "a": account_id, "f": fan_id}).scalar()
        if next_send is not None and _dt(next_send) > now:
            return None, "conversation_throttled"
    session.execute(text(
        f"DELETE FROM {t}engine_dispatch_slot "
        "WHERE account_id=:a AND state='held' AND expires_at<=:now"), {"a": account_id, "now": now_s})
    in_flight = session.execute(text(
        f"SELECT count(*) FROM {t}engine_dispatch_slot "
        "WHERE account_id=:a AND state='held' AND expires_at>:now"),
        {"a": account_id, "now": now_s}).scalar() or 0
    limit = admission.max_concurrent(policy, send_concurrency)
    if in_flight >= limit:
        return None, "provider_concurrency_limit"
    rate, capacity = admission.requests_per_second(policy), admission.burst(policy)
    bucket = session.execute(text(
        f"SELECT tokens,updated_at FROM {t}engine_provider_bucket WHERE account_id=:a"),
        {"a": account_id}).mappings().first()
    if bucket is None:
        tokens = float(capacity)
        bucket_clock = now
    else:
        tokens, bucket_clock = admission.refill_state(float(bucket["tokens"]), _dt(bucket["updated_at"]), now, rate, capacity)
    if tokens < 1.0:
        return None, "account_throttled"
    tokens -= 1.0
    lease_s = max(30.0, min(900.0, float(dispatch_lease_s)))
    expires = _iso(now + timedelta(seconds=lease_s))
    if unresolved is None:
        session.execute(text(
            f"INSERT INTO {t}engine_conversation_lease"
            "(tenant_id,account_id,fan_id,holder,owner_token,attempt_id,state,acquired_at,heartbeat_at,expires_at,"
            "next_send_at,updated_at) VALUES(:t,:a,:f,:h,:h,:att,'held',:now,:now,:exp, NULL,:now)"),
            {"t": tenant_id, "a": account_id, "f": fan_id, "h": holder, "att": attempt_id,
             "now": now_s, "exp": expires})
    else:
        session.execute(text(
            f"UPDATE {t}engine_conversation_lease SET holder=:h,owner_token=:h,attempt_id=:att,"
            "state='held',acquired_at=:now,heartbeat_at=:now,expires_at=:exp,next_send_at=NULL,updated_at=:now "
            "WHERE tenant_id=:t AND account_id=:a AND fan_id=:f"),
            {"t": tenant_id, "a": account_id, "f": fan_id, "h": holder, "att": attempt_id,
             "now": now_s, "exp": expires})
    session.execute(text(
        f"INSERT INTO {t}engine_provider_bucket(account_id,tokens,updated_at) "
        "VALUES(:a,:tokens,:now) ON CONFLICT(account_id) DO UPDATE "
        "SET tokens=excluded.tokens,updated_at=excluded.updated_at"),
        {"a": account_id, "tokens": str(tokens), "now": _iso(bucket_clock)})
    slot_id = "engine-slot-" + holder + ":" + attempt_id
    session.execute(text(
        f"INSERT INTO {t}engine_dispatch_slot"
        "(slot_id,tenant_id,account_id,fan_id,attempt_id,state,admitted_at,expires_at) "
        "VALUES(:s,:t,:a,:f,:att,'held',:now,:exp)"),
        {"s": slot_id, "t": tenant_id, "a": account_id, "f": fan_id, "att": attempt_id,
         "now": now_s, "exp": expires})
    return slot_id, None


def check_holder(session, tenant_id: str, account_id: str, fan_id: int, holder: str, now: datetime) -> bool:
    t = _prefix(session)
    row = session.execute(text(
        f"SELECT holder,state,expires_at FROM {t}engine_conversation_lease "
        "WHERE tenant_id=:t AND account_id=:a AND fan_id=:f"),
        {"t": tenant_id, "a": account_id, "f": fan_id}).mappings().first()
    return bool(row is not None and row["holder"] == holder and row["state"] == "held"
                and _dt(row["expires_at"]) > now)


def release(session, tenant_id: str, account_id: str, fan_id: int, holder: str, attempt_id: str,
            outcome: str, now: datetime, conversation_gap_s: float = 0.0) -> None:
    if outcome not in {"accepted", "not_accepted", "unknown"}:
        raise ValueError("invalid concurrent outcome")
    t = _prefix(session)
    session.execute(text(
        f"DELETE FROM {t}engine_dispatch_slot WHERE account_id=:a AND attempt_id=:att"),
        {"a": account_id, "att": attempt_id})
    row = session.execute(text(
        f"SELECT holder,attempt_id FROM {t}engine_conversation_lease "
        "WHERE tenant_id=:t AND account_id=:a AND fan_id=:f"),
        {"t": tenant_id, "a": account_id, "f": fan_id}).mappings().first()
    if row is None or row["holder"] != holder or (
            attempt_id is not None and row["attempt_id"] not in (attempt_id, str(attempt_id))):
        return
    now_s = _iso(now)
    if outcome == "unknown":
        session.execute(text(
            f"UPDATE {t}engine_conversation_lease SET state='unresolved',heartbeat_at=:now,"
            "expires_at=:exp,updated_at=:now WHERE tenant_id=:t AND account_id=:a AND fan_id=:f"),
            {"t": tenant_id, "a": account_id, "f": fan_id, "now": now_s,
             "exp": _iso(now + timedelta(days=1))})
        return
    session.execute(text(
        f"UPDATE {t}engine_conversation_lease SET state='released',holder='',owner_token='',"
        "attempt_id=NULL,expires_at=:now,next_send_at=:next,updated_at=:now "
        "WHERE tenant_id=:t AND account_id=:a AND fan_id=:f"),
        {"t": tenant_id, "a": account_id, "f": fan_id, "now": now_s,
         "next": _iso(now + timedelta(seconds=max(0.0, float(conversation_gap_s))))})
