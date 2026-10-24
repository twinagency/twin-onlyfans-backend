"""Durable limits for a manually activated native Fastt text pilot.

This module imports no application, environment, session or client (the raw-SQL
store helper it uses is stdlib-only unless a PostgreSQL target is opened).
``Ledger(target)`` takes a SQLite file path OR the PostgreSQL URL of the
application database, with identical semantics on both: every write is one
serialised transaction (SQLite BEGIN IMMEDIATE / PostgreSQL advisory lock).
Constructing ``Ledger(target)`` performs no I/O. ``activate`` is the explicit write
boundary: it requires an EXISTING active bridge conversation with no live owner,
unsettled delivery, writer marker, open Twin turn or unused prior budget. It
grants at most ten *provider messages*, once, preserving all prior evidence, and
installs a fenced owner lease through the immutable deadline. The caller then
uses coordination.reserve_ownership with the SAME token for local registration.

Use snapshot -> claim_input before generating, then begin_attempt immediately
before each provider POST. The latter repeats the canonical-source/owner/cap
checks in one BEGIN IMMEDIATE transaction. Each bubble consumes a separate
bridge attempt and budget unit. ``record_sent`` MUST run before native history
bookkeeping; it durably stores the caller's validated provider response.

There is deliberately no automatic resume or owner replacement. A fresh process
must not reuse a live pilot's token. Stop/unknown revoke unused grant, pause the
conversation and retain its reservation/evidence. No method retries delivery.
This cannot fence the official app or provider-scheduled/other-machine writes.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from twin_bridge import store


class Refused(RuntimeError):
    """A fixed, non-private reason for refusing work."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _dt(value: datetime | str) -> str:
    try:
        value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(value, datetime):
            raise ValueError()
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat(sep=" ", timespec="microseconds")
    except (ValueError, TypeError, OverflowError) as exc:
        raise Refused("invalid-timestamp") from exc


def _json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, OverflowError) as exc:
        raise Refused("invalid-evidence") from exc


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Pilot:
    pilot_id: str
    account_id: str
    fan_id: int
    owner_token: str
    cutoff: str
    deadline: str
    baseline_used: int
    end_limit: int
    revision: int
    state: str
    model_calls: int


@dataclass(frozen=True)
class SourceSnapshot:
    message_id: int
    source_hash: str
    created_at: str
    ingested_at: str
    member_ids: tuple[int, ...] = ()
    earliest_created_at: str = ""


@dataclass(frozen=True)
class Attempt:
    id: int
    account_id: str
    fan_id: int
    turn_id: str
    attempt_no: int
    owner_token: str
    revision: int


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS native_pilots (
        pilot_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, fan_id INTEGER NOT NULL,
        owner_token TEXT NOT NULL, cutoff TEXT NOT NULL, deadline TEXT NOT NULL,
        grant_limit INTEGER NOT NULL, baseline_used INTEGER NOT NULL, end_limit INTEGER NOT NULL,
        revision INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT,
        model_calls INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS native_pilot_inputs (
        input_key TEXT PRIMARY KEY, pilot_id TEXT NOT NULL, message_id INTEGER NOT NULL,
        source_hash TEXT NOT NULL, created_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
        member_ids_json TEXT NOT NULL, earliest_created_at TEXT NOT NULL,
        state TEXT NOT NULL, claimed_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(pilot_id,message_id))""",
    """CREATE TABLE IF NOT EXISTS native_pilot_receipts (
        attempt_id INTEGER PRIMARY KEY, pilot_id TEXT NOT NULL, input_key TEXT NOT NULL,
        bubble_no INTEGER NOT NULL, state TEXT NOT NULL, provider_message_id TEXT,
        response_json TEXT, evidence_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(input_key,bubble_no))""",
)


# Persistent-service pilots (see activate_service) are never capped by their own
# conversation budget: the account's delivery allowance is the cap. The
# conversation budget only has to stay above anything the allowance can grant
# (``limit_units`` is at most 10000), so it can never be the first thing to bind.
SERVICE_BUDGET_CEILING = 10000
SERVICE_ID = re.compile(r"svc-[A-Za-z0-9_-]{1,92}")


class Ledger:
    def __init__(self, path: str | Path, *, allowance: bool = False):
        # A PostgreSQL target is the application URL (never printed or logged).
        self.path = path if store.is_pg(path) else Path(path)
        self.pg = store.is_pg(path)
        # allowance=True (persistent service only): every admission also consumes
        # one unit of the account's EXISTING of_delivery_policy, in the same
        # transaction as the durable attempt. The bounded pilot never sets it.
        self.allowance = allowance

    def _check_target(self) -> None:
        # Never silently create an empty database at a misspelled production path.
        if not self.pg and not self.path.is_file():
            raise Refused("database-missing")

    @contextmanager
    def _tx(self, *, initialize: bool = False) -> Iterator[object]:
        self._check_target()
        with store.transaction(self.path) as con:
            # PostgreSQL: fastt_runtime tables are created by store.ensure_runtime_schema (runner/control), not here.
            if initialize and not self.pg:
                for statement in _SCHEMA:
                    con.execute(statement)
            yield con

    @staticmethod
    def _pilot(row) -> Pilot:
        return Pilot(**{key: row[key] for key in Pilot.__dataclass_fields__})

    @staticmethod
    def _unsettled(con, account: str, fan: int) -> None:
        if con.execute("SELECT 1 FROM twin_bridge_send_attempts WHERE account_id=? AND fan_id=? "
                       "AND state IN ('dispatching','delivery_unknown') LIMIT 1", (account, fan)).fetchone():
            raise Refused("delivery-unresolved")
        if con.execute("SELECT 1 FROM twin_bridge_turns WHERE account_id=? AND fan_id=? "
                       "AND state NOT IN ('delivered','cancelled','parked') LIMIT 1", (account, fan)).fetchone():
            raise Refused("twin-work-open")
        if store.has_table(con, "twin_bridge_writes"):
            if con.execute("SELECT 1 FROM twin_bridge_writes WHERE account_id=? AND fan_id IN (?,0) LIMIT 1",
                           (account, fan)).fetchone():
                raise Refused("writer-unresolved")

    def activate(self, *, pilot_id: str, account_id: str, fan_id: int, owner_token: str,
                 cutoff: datetime | str, deadline: datetime | str, limit: int,
                 now: datetime | str) -> Pilot:
        """Grant ``limit`` additional messages once; exact same-owner replay is read-only."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 30:
            raise Refused("invalid-limit")
        if not isinstance(pilot_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", pilot_id):
            raise Refused("invalid-pilot-id")
        from .scope import AccountScope  # Twin "of-<owner>" or bare decimal; see scope.py
        if not AccountScope.valid(account_id) \
                or not isinstance(fan_id, int) or isinstance(fan_id, bool) or fan_id <= 0:
            raise Refused("invalid-identity")
        if not isinstance(owner_token, str) or len(owner_token) < 16 or len(owner_token) > 256:
            raise Refused("invalid-owner")
        now_s, cutoff_s, deadline_s = _dt(now), _dt(cutoff), _dt(deadline)
        if cutoff_s > now_s or deadline_s <= now_s:
            raise Refused("invalid-window")
        with self._tx(initialize=True) as con:
            existing = con.execute("SELECT * FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            if existing:
                expected = (account_id, fan_id, owner_token, cutoff_s, deadline_s, limit)
                actual = tuple(existing[k] for k in ("account_id", "fan_id", "owner_token", "cutoff", "deadline", "grant_limit"))
                if actual != expected:
                    raise Refused("activation-conflict")
                return self._pilot(existing)
            if con.execute("SELECT 1 FROM native_pilots WHERE account_id=? AND fan_id=? AND state='active'",
                           (account_id, fan_id)).fetchone():
                raise Refused("pilot-already-active")
            self._unsettled(con, account_id, fan_id)
            c = con.execute("SELECT * FROM twin_bridge_conversations WHERE account_id=? AND fan_id=?",
                            (account_id, fan_id)).fetchone()
            if c is None or c["state"] != "active":
                raise Refused("conversation-not-active")
            if c["owner_token"] and (not c["owner_expires_at"] or _dt(c["owner_expires_at"]) > now_s):
                raise Refused("owner-live")
            used, old_limit = c["send_budget_used"], c["send_budget_limit"]
            if used < 0 or old_limit != used:
                raise Refused("prior-budget-not-exhausted")
            revision, end = c["revision"] + 1, used + limit
            con.execute("UPDATE twin_bridge_conversations SET send_budget_limit=?,owner_token=?,"
                        "owner_expires_at=?,reserved=?,revision=?,updated_at=? WHERE account_id=? AND fan_id=?",
                        (end, owner_token, deadline_s, True, revision, now_s, account_id, fan_id))
            con.execute("INSERT INTO native_pilots(pilot_id,account_id,fan_id,owner_token,cutoff,deadline,"
                        "grant_limit,baseline_used,end_limit,revision,state,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,'active',?,?)",
                        (pilot_id, account_id, fan_id, owner_token, cutoff_s, deadline_s, limit, used,
                         end, revision, now_s, now_s))
            return self._pilot(con.execute("SELECT * FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone())

    def _active(self, con, pilot_id: str, owner_token: str, now: str):
        p = con.execute("SELECT * FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
        if p is None or p["owner_token"] != owner_token:
            raise Refused("pilot-owner-mismatch")
        if p["state"] != "active":
            raise Refused("pilot-stopped")
        if now >= p["deadline"]:
            raise Refused("pilot-deadline")
        c = con.execute("SELECT * FROM twin_bridge_conversations WHERE account_id=? AND fan_id=?",
                        (p["account_id"], p["fan_id"])).fetchone()
        if c is None or c["state"] != "active" or not c["reserved"] or c["owner_token"] != owner_token \
                or c["revision"] != p["revision"] or not c["owner_expires_at"] \
                or _dt(c["owner_expires_at"]) <= now:
            raise Refused("authority-lost")
        if c["send_budget_limit"] != p["end_limit"]:
            raise Refused("budget-changed")
        if c["send_budget_used"] >= p["end_limit"]:
            raise Refused("budget-exhausted")
        self._unsettled(con, p["account_id"], p["fan_id"])
        return p

    @staticmethod
    def _source(con, p, now: str,
                exclude_input_key: str | None = None) -> SourceSnapshot | None:
        # Only proven outputs from the CURRENT input are excluded. A newer external
        # outbound, system event, unsupported fan input, etc. holds the pilot.
        rows = con.execute("SELECT m.message_id,m.direction,m.body,m.media_ids,m.media_count,m.price_cents,"
                          "m.is_tip,m.is_unsent,m.created_at,m.ingested_at,m.raw_json FROM messages m "
                          "WHERE m.account_id=? AND m.fan_id=? AND NOT (m.direction='out' AND m.is_unsent) "
                          "AND NOT EXISTS (SELECT 1 FROM native_pilot_receipts r "
                          "WHERE r.pilot_id=? AND r.input_key=? AND r.state='sent' "
                          "AND r.provider_message_id=CAST(m.message_id AS TEXT)) "
                          "ORDER BY m.created_at DESC,m.message_id DESC LIMIT 101",
                          (p["account_id"], p["fan_id"], p["pilot_id"], exclude_input_key)).fetchall()
        if not rows or rows[0]["direction"] != "in":
            return None
        members = []
        for row in rows:
            if row["direction"] != "in":
                break
            try:
                created, ingested = _dt(row["created_at"]), _dt(row["ingested_at"])
            except Refused:
                return None
            if created <= p["cutoff"]:
                break
            if not (p["cutoff"] < ingested <= now) or created > now:
                return None
            if row["is_unsent"] != 0 or row["is_tip"] != 0 or row["price_cents"] != 0 \
                    or row["media_count"] != 0 or not isinstance(row["body"], str) or not row["body"].strip() \
                    or not isinstance(row["message_id"], int) or row["message_id"] <= 0:
                return None
            try:
                if json.loads(row["media_ids"]) != []:
                    return None
            except (TypeError, ValueError):
                return None
            members.append(dict(row))
            if len(members) > 100:
                return None  # A truncated burst is never treated as complete.
        if not members:
            return None
        latest = members[0]
        members.reverse()
        return SourceSnapshot(latest["message_id"], _hash(members), _dt(latest["created_at"]),
                              _dt(latest["ingested_at"]), tuple(r["message_id"] for r in members),
                              _dt(members[0]["created_at"]))

    def snapshot(self, *, pilot_id: str, owner_token: str, now: datetime | str) -> SourceSnapshot | None:
        now_s = _dt(now)
        with self._tx() as con:
            return self._source(con, self._active(con, pilot_id, owner_token, now_s), now_s)

    def claim_input(self, *, pilot_id: str, owner_token: str, snapshot: SourceSnapshot,
                    now: datetime | str) -> str:
        """Claim once before any model call. A crash never grants regeneration."""
        if not isinstance(snapshot, SourceSnapshot):
            raise Refused("invalid-snapshot")
        now_s = _dt(now)
        with self._tx() as con:
            p = self._active(con, pilot_id, owner_token, now_s)
            if self._source(con, p, now_s) != snapshot:
                raise Refused("source-changed")
            if con.execute("SELECT 1 FROM native_pilot_inputs WHERE pilot_id=? AND (message_id=? OR state='generating')",
                           (pilot_id, snapshot.message_id)).fetchone():
                raise Refused("input-already-claimed")
            key = _hash([pilot_id, snapshot.message_id])
            con.execute("INSERT INTO native_pilot_inputs(input_key,pilot_id,message_id,source_hash,created_at,ingested_at,"
                        "member_ids_json,earliest_created_at,state,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'generating',?,?)",
                        (key, pilot_id, snapshot.message_id, snapshot.source_hash, snapshot.created_at,
                         snapshot.ingested_at, _json(snapshot.member_ids), snapshot.earliest_created_at, now_s, now_s))
            return key

    def reserve_model_call(self, *, pilot_id: str, owner_token: str, now: datetime | str, limit: int = 80) -> int:
        """Charge a model attempt before calling it, including attempts that fail."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 80:
            raise Refused("invalid-model-limit")
        now_s = _dt(now)
        with self._tx() as con:
            p = self._active(con, pilot_id, owner_token, now_s)
            if p["model_calls"] >= limit:
                raise Refused("model-budget-exhausted")
            con.execute("UPDATE native_pilots SET model_calls=model_calls+1,updated_at=? WHERE pilot_id=?", (now_s, pilot_id))
            return p["model_calls"] + 1

    def begin_attempt(self, *, pilot_id: str, owner_token: str, input_key: str,
                      bubble_no: int, revision: int, now: datetime | str) -> Attempt:
        """Atomic final source + authority check, budget charge, durable intent."""
        if not isinstance(bubble_no, int) or isinstance(bubble_no, bool) or not 1 <= bubble_no <= 10:
            raise Refused("invalid-bubble")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise Refused("invalid-revision")
        now_s = _dt(now)
        with self._tx() as con:
            p = self._active(con, pilot_id, owner_token, now_s)
            if revision != p["revision"]:
                raise Refused("revision-changed")
            i = con.execute("SELECT * FROM native_pilot_inputs WHERE input_key=? AND pilot_id=?", (input_key, pilot_id)).fetchone()
            if i is None or i["state"] != "generating":
                raise Refused("input-not-generating")
            expected = SourceSnapshot(i["message_id"], i["source_hash"], i["created_at"], i["ingested_at"],
                                      tuple(json.loads(i["member_ids_json"])), i["earliest_created_at"])
            if self._source(con, p, now_s, exclude_input_key=input_key) != expected:
                raise Refused("source-changed")
            if con.execute("SELECT 1 FROM native_pilot_receipts WHERE input_key=? AND bubble_no=?", (input_key, bubble_no)).fetchone():
                raise Refused("attempt-already-exists")
            if self.allowance:
                self._consume_allowance(con, p["account_id"], now_s)
            tid = "native-pilot-" + _hash([pilot_id, input_key, bubble_no])
            aid = store.insert_id(con, "INSERT INTO twin_bridge_send_attempts(account_id,fan_id,turn_id,attempt_no,state,"
                                  "owner_token,revision,created_at,updated_at) VALUES(?,?,?,1,'dispatching',?,?,?,?)",
                                  (p["account_id"], p["fan_id"], tid, owner_token, revision, now_s, now_s))
            con.execute("UPDATE twin_bridge_conversations SET send_budget_used=send_budget_used+1,updated_at=? "
                        "WHERE account_id=? AND fan_id=?", (now_s, p["account_id"], p["fan_id"]))
            con.execute("INSERT INTO native_pilot_receipts(attempt_id,pilot_id,input_key,bubble_no,state,created_at,updated_at) "
                        "VALUES(?,?,?,?,'dispatching',?,?)", (aid, pilot_id, input_key, bubble_no, now_s, now_s))
            return Attempt(aid, p["account_id"], p["fan_id"], tid, 1, owner_token, revision)

    @staticmethod
    def _policy_row(con, account: str):
        return con.execute("SELECT enabled,limit_units,used_units,throttle_seconds,next_dispatch_at,active_intent_id "
                           "FROM of_delivery_policy WHERE account_id=?", (account,)).fetchone()

    @classmethod
    def _consume_allowance(cls, con, account: str, now_s: str) -> None:
        """Spend ONE unit of the account's delivery allowance, or refuse.

        Never resets, lowers or raises ``used_units`` except by this single
        increment, and the increment is conditional on the row still reading as
        checked, so it cannot pass ``limit_units`` even against Twin's own worker.
        The unit is never refunded: an unknown outcome keeps it spent.
        """
        row = cls._policy_row(con, account)
        if row is None:
            raise Refused("allowance-missing")
        if not row["enabled"]:
            raise Refused("allowance-disabled")
        if row["used_units"] >= row["limit_units"]:
            raise Refused("allowance-exhausted")
        if row["active_intent_id"]:
            raise Refused("allowance-busy")
        if row["next_dispatch_at"] is not None and _dt(row["next_dispatch_at"]) > now_s:
            raise Refused("allowance-throttled")
        due = _dt(datetime.fromisoformat(now_s) + timedelta(seconds=row["throttle_seconds"]))
        cur = con.execute("UPDATE of_delivery_policy SET used_units=used_units+1,next_dispatch_at=? "
                          "WHERE account_id=? AND enabled AND used_units=? AND used_units<limit_units "
                          "AND active_intent_id IS NULL",
                          (due, account, row["used_units"]))
        if cur.rowcount != 1:
            raise Refused("allowance-changed")

    def allowance_status(self, account: str) -> dict | None:
        """Read-only copy of the account's delivery policy numbers (None when absent)."""
        with store.readonly(self.path) as con:
            row = self._policy_row(con, account)
        if row is None:
            return None
        due = row["next_dispatch_at"]
        return {"enabled": bool(row["enabled"]), "limit": row["limit_units"], "used": row["used_units"],
                "throttle_seconds": row["throttle_seconds"], "busy": bool(row["active_intent_id"]),
                "next_dispatch_at": None if due is None else _dt(due)}

    def validate_dispatch(self, *, pilot_id: str, owner_token: str, attempt_id: int,
                          now: datetime | str) -> None:
        """Read-only recheck after thread handoff, immediately BEFORE guarded_send.

        This admitted attempt is the sole permitted unsettled intent; its last
        budget unit may already equal the ceiling. All other unsettled intents,
        writer markers, invalidated burst members and authority changes refuse.
        Call before entering guarded_send, which creates its OWN writer marker.
        No database transaction is kept open during the eventual network POST.
        """
        now_s = _dt(now)
        self._check_target()
        with store.readonly(self.path) as con:
            p = con.execute("SELECT * FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            if p is None or p["owner_token"] != owner_token:
                raise Refused("pilot-owner-mismatch")
            if p["state"] != "active":
                raise Refused("pilot-stopped")
            if now_s >= p["deadline"]:
                raise Refused("pilot-deadline")
            c = con.execute("SELECT * FROM twin_bridge_conversations WHERE account_id=? AND fan_id=?",
                            (p["account_id"], p["fan_id"])).fetchone()
            if c is None or c["state"] != "active" or not c["reserved"] or c["owner_token"] != owner_token \
                    or c["revision"] != p["revision"] or not c["owner_expires_at"] \
                    or _dt(c["owner_expires_at"]) <= now_s:
                raise Refused("authority-lost")
            if c["send_budget_limit"] != p["end_limit"] \
                    or not p["baseline_used"] < c["send_budget_used"] <= p["end_limit"]:
                raise Refused("budget-changed")
            a = con.execute("SELECT * FROM twin_bridge_send_attempts WHERE id=?", (attempt_id,)).fetchone()
            r = con.execute("SELECT * FROM native_pilot_receipts WHERE attempt_id=? AND pilot_id=?", (attempt_id, pilot_id)).fetchone()
            if a is None or r is None or a["account_id"] != p["account_id"] or a["fan_id"] != p["fan_id"] \
                    or a["owner_token"] != owner_token or a["revision"] != p["revision"]:
                raise Refused("attempt-owner-mismatch")
            if a["state"] != "dispatching" or r["state"] != "dispatching":
                raise Refused("attempt-not-dispatching")
            if con.execute("SELECT 1 FROM twin_bridge_send_attempts WHERE account_id=? AND fan_id=? "
                           "AND id<>? AND state IN ('dispatching','delivery_unknown') LIMIT 1",
                           (p["account_id"], p["fan_id"], attempt_id)).fetchone():
                raise Refused("delivery-unresolved")
            if con.execute("SELECT 1 FROM twin_bridge_turns WHERE account_id=? AND fan_id=? "
                           "AND state NOT IN ('delivered','cancelled','parked') LIMIT 1",
                           (p["account_id"], p["fan_id"])).fetchone():
                raise Refused("twin-work-open")
            if store.has_table(con, "twin_bridge_writes"):
                if con.execute("SELECT 1 FROM twin_bridge_writes WHERE account_id=? AND fan_id IN (?,0) LIMIT 1",
                               (p["account_id"], p["fan_id"])).fetchone():
                    raise Refused("writer-unresolved")
            i = con.execute("SELECT * FROM native_pilot_inputs WHERE input_key=? AND pilot_id=?",
                            (r["input_key"], pilot_id)).fetchone()
            if i is None or i["state"] != "generating":
                raise Refused("input-not-generating")
            expected = SourceSnapshot(i["message_id"], i["source_hash"], i["created_at"], i["ingested_at"],
                                      tuple(json.loads(i["member_ids_json"])), i["earliest_created_at"])
            if self._source(con, p, now_s, exclude_input_key=r["input_key"]) != expected:
                raise Refused("source-changed")

    def record_sent(self, *, pilot_id: str, owner_token: str, attempt_id: int,
                    provider_message_id: str, validated_response: dict, evidence: dict,
                    now: datetime | str) -> None:
        """Persist validated raw receipt AND sent evidence before native bookkeeping.

        Caller validates account, recipient, text and response shape. Recording
        an already-dispatched result remains legal after stop/deadline; erasing
        evidence because time elapsed would be unsafe. Identical replay is safe.
        """
        if not isinstance(provider_message_id, str) or not provider_message_id.isascii() \
                or not provider_message_id.isdecimal() or int(provider_message_id) <= 0 \
                or not isinstance(validated_response, dict) or not isinstance(evidence, dict) \
                or isinstance(validated_response.get("id"), bool) \
                or str(validated_response.get("id")) != provider_message_id:
            raise Refused("invalid-receipt")
        response_s, evidence_s, now_s = _json(validated_response), _json(evidence), _dt(now)
        if len(response_s.encode("utf-8")) > 262144 or len(evidence_s.encode("utf-8")) > 65536:
            raise Refused("oversize-receipt")
        with self._tx() as con:
            self._outcome_owner(con, pilot_id, owner_token, attempt_id)
            r = con.execute("SELECT * FROM native_pilot_receipts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if r["state"] == "sent":
                if (r["provider_message_id"], r["response_json"], r["evidence_json"]) != (provider_message_id, response_s, evidence_s):
                    raise Refused("receipt-conflict")
                return
            if r["state"] != "dispatching":
                raise Refused("attempt-not-dispatching")
            if con.execute("SELECT 1 FROM native_pilot_receipts WHERE pilot_id=? AND provider_message_id=? AND attempt_id<>?",
                           (pilot_id, provider_message_id, attempt_id)).fetchone():
                raise Refused("provider-id-reused")
            con.execute("UPDATE native_pilot_receipts SET state='sent',provider_message_id=?,response_json=?,evidence_json=?,updated_at=? "
                        "WHERE attempt_id=?", (provider_message_id, response_s, evidence_s, now_s, attempt_id))
            con.execute("UPDATE twin_bridge_send_attempts SET state='sent',provider_message_id=?,evidence_json=?,updated_at=? WHERE id=?",
                        (provider_message_id, evidence_s, now_s, attempt_id))

    @staticmethod
    def _outcome_owner(con, pilot_id: str, owner_token: str, attempt_id: int) -> None:
        row = con.execute("SELECT p.owner_token,a.owner_token AS attempt_owner FROM native_pilot_receipts r "
                          "JOIN native_pilots p ON p.pilot_id=r.pilot_id JOIN twin_bridge_send_attempts a ON a.id=r.attempt_id "
                          "WHERE r.attempt_id=? AND p.pilot_id=?", (attempt_id, pilot_id)).fetchone()
        if row is None or row["owner_token"] != owner_token or row["attempt_owner"] != owner_token:
            raise Refused("attempt-owner-mismatch")

    @staticmethod
    def _stop(con, pilot_id: str, owner_token: str, reason: str, now_s: str, *, unknown: bool = False) -> None:
        p = con.execute("SELECT * FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
        if p is None or p["owner_token"] != owner_token:
            raise Refused("pilot-owner-mismatch")
        con.execute("UPDATE native_pilots SET state='stopped',reason=?,updated_at=? WHERE pilot_id=?", (reason, now_s, pilot_id))
        con.execute("UPDATE native_pilot_inputs SET state='stopped',updated_at=? WHERE pilot_id=? AND state='generating'", (now_s, pilot_id))
        # Never modify a replacement owner's conversation. The pilot itself is
        # nevertheless stopped, preventing the old token from admitting work.
        con.execute("UPDATE twin_bridge_conversations SET state=?,reason=?,send_budget_limit=send_budget_used,"
                    "revision=revision+1,owner_token=NULL,owner_expires_at=NULL,reserved=?,updated_at=? "
                    "WHERE account_id=? AND fan_id=? AND owner_token=? AND revision=?",
                    ("attention" if unknown else "paused", "native-pilot-" + reason, True, now_s,
                     p["account_id"], p["fan_id"], owner_token, p["revision"]))

    def stop(self, *, pilot_id: str, owner_token: str, reason: str, now: datetime | str) -> None:
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", reason):
            raise Refused("invalid-reason")
        with self._tx() as con:
            self._stop(con, pilot_id, owner_token, reason, _dt(now))

    def record_unknown(self, *, pilot_id: str, owner_token: str, attempt_id: int,
                       now: datetime | str) -> None:
        now_s = _dt(now)
        with self._tx() as con:
            self._outcome_owner(con, pilot_id, owner_token, attempt_id)
            r = con.execute("SELECT state FROM native_pilot_receipts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if r["state"] == "sent":
                raise Refused("already-proven-sent")
            con.execute("UPDATE native_pilot_receipts SET state='delivery_unknown',updated_at=? WHERE attempt_id=?", (now_s, attempt_id))
            con.execute("UPDATE twin_bridge_send_attempts SET state='delivery_unknown',reason='native-pilot-unknown',updated_at=? WHERE id=?",
                        (now_s, attempt_id))
            self._stop(con, pilot_id, owner_token, "delivery-unknown", now_s, unknown=True)

    def finish_input(self, *, pilot_id: str, owner_token: str, input_key: str, now: datetime | str) -> None:
        with self._tx() as con:
            p = con.execute("SELECT owner_token FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            if p is None or p["owner_token"] != owner_token:
                raise Refused("pilot-owner-mismatch")
            con.execute("UPDATE native_pilot_inputs SET state='complete',updated_at=? WHERE pilot_id=? AND input_key=? AND state='generating'",
                        (_dt(now), pilot_id, input_key))

    # ── persistent service pilots ────────────────────────────────────────────
    # Same rows, same fences and the same admission/unknown rules as a bounded
    # pilot; what differs is that the lease is renewed instead of fixed, the
    # cap is the account's delivery allowance, and a restarted process may adopt
    # its own pilot once the old lease has lapsed and nothing is unresolved.

    def activate_service(self, *, pilot_id: str, account_id: str, fan_id: int, owner_token: str,
                         now: datetime | str, ttl_s: float, cutoff: datetime | str | None = None) -> Pilot:
        """Create or adopt the ONE stable service pilot for an account/fan.

        Refuses (never waits, never forces) while another owner's lease is live,
        any send is unsettled (dispatching / unknown / writer marker / open Twin
        turn), or the conversation is not active (an operator pause, or the
        attention state an unknown outcome leaves behind). Adoption keeps the
        original ``cutoff`` and every used count; it only issues a new fenced
        token and revision. A claim abandoned by a crash is stopped, never
        regenerated (no receipt can exist for it, or ``_unsettled`` would refuse).

        ``cutoff`` (all-fans mode only) sets a NEW pilot's cutoff to an earlier instant (the
        runner's start) so a fan first seen mid-run can still be answered for a message that
        arrived since then; it can never be in the future and never changes an adopted pilot.
        """
        if not isinstance(pilot_id, str) or not SERVICE_ID.fullmatch(pilot_id):
            raise Refused("invalid-pilot-id")
        from .scope import AccountScope
        if not AccountScope.valid(account_id) or not isinstance(fan_id, int) or isinstance(fan_id, bool) or fan_id <= 0:
            raise Refused("invalid-identity")
        if not isinstance(owner_token, str) or not 16 <= len(owner_token) <= 256:
            raise Refused("invalid-owner")
        if not isinstance(ttl_s, (int, float)) or isinstance(ttl_s, bool) or not 5 <= ttl_s <= 3600:
            raise Refused("invalid-ttl")
        now_s = _dt(now)
        lease_s = _dt(datetime.fromisoformat(now_s) + timedelta(seconds=ttl_s))
        cutoff_s = now_s if cutoff is None else _dt(cutoff)
        if cutoff_s > now_s:
            raise Refused("invalid-cutoff")
        with self._tx(initialize=True) as con:
            c = con.execute("SELECT * FROM twin_bridge_conversations WHERE account_id=? AND fan_id=?",
                            (account_id, fan_id)).fetchone()
            if c is None or c["state"] != "active":
                raise Refused("conversation-not-active")
            existing = con.execute("SELECT * FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            if con.execute("SELECT 1 FROM native_pilots WHERE account_id=? AND fan_id=? AND state='active' AND pilot_id<>?",
                           (account_id, fan_id, pilot_id)).fetchone():
                raise Refused("pilot-already-active")
            self._unsettled(con, account_id, fan_id)
            lease_live = bool(c["owner_token"] and c["owner_expires_at"] and _dt(c["owner_expires_at"]) > now_s)
            if existing is not None:
                if (existing["account_id"], existing["fan_id"]) != (account_id, fan_id):
                    raise Refused("activation-conflict")
                if existing["state"] == "active" and (lease_live or _dt(existing["deadline"]) > now_s):
                    raise Refused("owner-live")
                end = existing["end_limit"]
                if c["send_budget_used"] >= end:
                    raise Refused("budget-exhausted")
                revision = max(c["revision"], existing["revision"]) + 1
                con.execute("UPDATE native_pilots SET owner_token=?,deadline=?,revision=?,state='active',reason=NULL,"
                            "updated_at=? WHERE pilot_id=?", (owner_token, lease_s, revision, now_s, pilot_id))
                con.execute("UPDATE native_pilot_inputs SET state='stopped',updated_at=? WHERE pilot_id=? AND state='generating'",
                            (now_s, pilot_id))
            else:
                if lease_live:
                    raise Refused("owner-live")
                used, old_limit = c["send_budget_used"], c["send_budget_limit"]
                if used < 0 or old_limit != used:
                    raise Refused("prior-budget-not-exhausted")
                revision, end = c["revision"] + 1, used + SERVICE_BUDGET_CEILING
                con.execute("INSERT INTO native_pilots(pilot_id,account_id,fan_id,owner_token,cutoff,deadline,"
                            "grant_limit,baseline_used,end_limit,revision,state,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,'active',?,?)",
                            (pilot_id, account_id, fan_id, owner_token, cutoff_s, lease_s, SERVICE_BUDGET_CEILING,
                             used, end, revision, now_s, now_s))
            con.execute("UPDATE twin_bridge_conversations SET send_budget_limit=?,owner_token=?,owner_expires_at=?,"
                        "reserved=?,revision=?,updated_at=? WHERE account_id=? AND fan_id=?",
                        (end, owner_token, lease_s, True, revision, now_s, account_id, fan_id))
            return self._pilot(con.execute("SELECT * FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone())

    def service_watermark(self, *, pilot_id: str, account_id: str, now: datetime | str) -> str:
        """The account's all-fans activation instant: written ONCE (first start), then reused.

        Stored as a marker row in the existing ``native_pilots`` table (state ``watermark``,
        fan 0): never ``active``, never a lease, no new table or migration. Every restart
        returns the first value, so an input that arrived after activation but before a
        restart is still answerable, while anything older (the pre-activation backlog) never is.
        Delete the row to re-baseline deliberately.
        """
        if not isinstance(pilot_id, str) or not SERVICE_ID.fullmatch(pilot_id):
            raise Refused("invalid-pilot-id")
        now_s = _dt(now)
        with self._tx(initialize=True) as con:
            row = con.execute("SELECT account_id,state,cutoff FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            if row is not None:
                if row["account_id"] != account_id or row["state"] != "watermark":
                    raise Refused("activation-conflict")
                return row["cutoff"]
            con.execute("INSERT INTO native_pilots(pilot_id,account_id,fan_id,owner_token,cutoff,deadline,"
                        "grant_limit,baseline_used,end_limit,revision,state,created_at,updated_at) "
                        "VALUES(?,?,0,'watermark',?,?,0,0,0,0,'watermark',?,?)",
                        (pilot_id, account_id, now_s, now_s, now_s, now_s))
            return now_s

    def renew(self, *, pilot_id: str, owner_token: str, now: datetime | str, ttl_s: float) -> str:
        """Move the pilot deadline and the conversation lease forward; returns the new deadline.

        Refuses (``authority-lost``) once the lease has already lapsed or another
        token/revision owns the conversation: a lapsed lease may have been adopted,
        so it is never silently revived.
        """
        if not isinstance(ttl_s, (int, float)) or isinstance(ttl_s, bool) or not 5 <= ttl_s <= 3600:
            raise Refused("invalid-ttl")
        now_s = _dt(now)
        lease_s = _dt(datetime.fromisoformat(now_s) + timedelta(seconds=ttl_s))
        with self._tx() as con:
            # Deliberately NOT ``_active``: a lease must stay renewable while one
            # POST is in flight (an unsettled attempt would make ``_active`` refuse).
            p = con.execute("SELECT * FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            if p is None or p["owner_token"] != owner_token:
                raise Refused("pilot-owner-mismatch")
            if p["state"] != "active":
                raise Refused("pilot-stopped")
            c = con.execute("SELECT * FROM twin_bridge_conversations WHERE account_id=? AND fan_id=?",
                            (p["account_id"], p["fan_id"])).fetchone()
            if now_s >= p["deadline"] or c is None or c["state"] != "active" or not c["reserved"] \
                    or c["owner_token"] != owner_token or c["revision"] != p["revision"] \
                    or not c["owner_expires_at"] or _dt(c["owner_expires_at"]) <= now_s:
                raise Refused("authority-lost")
            con.execute("UPDATE native_pilots SET deadline=?,updated_at=? WHERE pilot_id=?", (lease_s, now_s, pilot_id))
            con.execute("UPDATE twin_bridge_conversations SET owner_expires_at=?,updated_at=? "
                        "WHERE account_id=? AND fan_id=? AND owner_token=? AND revision=?",
                        (lease_s, now_s, p["account_id"], p["fan_id"], owner_token, p["revision"]))
            return lease_s

    def release_service(self, *, pilot_id: str, owner_token: str, now: datetime | str) -> None:
        """Clean shutdown: end the lease, keep the pilot, state and reservation.

        Only the current owner may release. A claim with NO receipt is deleted so
        the unanswered message is generated again after restart (nothing was
        sent for it); a claim with any receipt is stopped, never regenerated.
        Unsettled sends are left exactly as they are and still block adoption.
        """
        now_s = _dt(now)
        with self._tx() as con:
            p = con.execute("SELECT * FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            if p is None or p["owner_token"] != owner_token:
                raise Refused("pilot-owner-mismatch")
            con.execute("DELETE FROM native_pilot_inputs WHERE pilot_id=? AND state='generating' AND NOT EXISTS "
                        "(SELECT 1 FROM native_pilot_receipts r WHERE r.input_key=native_pilot_inputs.input_key)", (pilot_id,))
            con.execute("UPDATE native_pilot_inputs SET state='stopped',updated_at=? WHERE pilot_id=? AND state='generating'",
                        (now_s, pilot_id))
            if p["state"] == "active":
                con.execute("UPDATE native_pilots SET deadline=?,updated_at=? WHERE pilot_id=?", (now_s, now_s, pilot_id))
                con.execute("UPDATE twin_bridge_conversations SET owner_token=NULL,owner_expires_at=NULL,"
                            "revision=revision+1,updated_at=? WHERE account_id=? AND fan_id=? AND owner_token=? AND revision=?",
                            (now_s, p["account_id"], p["fan_id"], owner_token, p["revision"]))

    def count_model_call(self, *, pilot_id: str, owner_token: str, now: datetime | str) -> int:
        """Durably count a model attempt for a service pilot (no per-pilot cap; the
        per-call limits and the account's daily budget bound spend)."""
        now_s = _dt(now)
        with self._tx() as con:
            p = self._active(con, pilot_id, owner_token, now_s)
            con.execute("UPDATE native_pilots SET model_calls=model_calls+1,updated_at=? WHERE pilot_id=?", (now_s, pilot_id))
            return p["model_calls"] + 1

    def input_claimed(self, *, pilot_id: str, message_id: int) -> bool:
        with store.readonly(self.path) as con:
            return con.execute("SELECT 1 FROM native_pilot_inputs WHERE pilot_id=? AND message_id=?",
                               (pilot_id, message_id)).fetchone() is not None

    def status(self, pilot_id: str) -> dict:
        """Return only public metadata/counters, never owner tokens or message text."""
        with self._tx() as con:
            p = con.execute("SELECT pilot_id,account_id,fan_id,cutoff,deadline,baseline_used,end_limit,revision,state,reason,model_calls "
                            "FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            if p is None:
                raise Refused("pilot-missing")
            result = dict(p)
            result["attempts"] = {r["state"]: r["n"] for r in con.execute(
                "SELECT state,COUNT(*) AS n FROM native_pilot_receipts WHERE pilot_id=? GROUP BY state", (pilot_id,))}
            return result

    def receipts(self, *, pilot_id: str, input_key: str | None = None) -> list[dict]:
        """Metadata only; confirmed IDs may be used for live-history reconciliation."""
        with self._tx() as con:
            query = ("SELECT attempt_id,input_key,bubble_no,state,provider_message_id,created_at,updated_at "
                     "FROM native_pilot_receipts WHERE pilot_id=?")
            args: tuple = (pilot_id,)
            if input_key is not None:
                query += " AND input_key=?"
                args += (input_key,)
            return [dict(r) for r in con.execute(query + " ORDER BY attempt_id", args)]
