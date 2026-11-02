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
import os
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from twin_bridge import store
from .overlap import overlapped_replies


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
    """CREATE TABLE IF NOT EXISTS native_pilot_followups (
        job_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, fan_id INTEGER NOT NULL, kind TEXT NOT NULL,
        payload_json TEXT NOT NULL, run_at TEXT NOT NULL, last_in_id INTEGER, last_in_at TEXT,
        state TEXT NOT NULL, reason TEXT, input_key TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    # Engine v2 (additive): the account runner lease and the per-fan work read model.
    """CREATE TABLE IF NOT EXISTS engine_runner_lease (
        account_id TEXT PRIMARY KEY, owner_token TEXT NOT NULL, holder TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 1, acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
        expires_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS engine_work (
        account_id TEXT NOT NULL, fan_id INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT,
        message_id INTEGER, waiting_since TEXT, state_since TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY(account_id,fan_id))""",
    # Throughput work (additive): durable operator-visible conditions (provider throttle, send stall).
    """CREATE TABLE IF NOT EXISTS engine_alert (
        account_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, first_at TEXT NOT NULL, last_at TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 1, detail_json TEXT, PRIMARY KEY(account_id,kind))""",
    # Concurrent delivery (additive): one durable owner per conversation, one counted slot per
    # in-flight provider request, and the account token bucket. None of these grant authority by
    # themselves; the pilot/conversation receipt checks still do.
    """CREATE TABLE IF NOT EXISTS engine_conversation_lease (
        tenant_id TEXT NOT NULL, account_id TEXT NOT NULL, fan_id INTEGER NOT NULL,
        holder TEXT NOT NULL, owner_token TEXT NOT NULL, attempt_id TEXT, state TEXT NOT NULL,
        acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        next_send_at TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY(tenant_id,account_id,fan_id))""",
    """CREATE TABLE IF NOT EXISTS engine_dispatch_slot (
        slot_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, account_id TEXT NOT NULL,
        fan_id INTEGER NOT NULL, attempt_id TEXT, state TEXT NOT NULL,
        admitted_at TEXT NOT NULL, expires_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS ix_engine_dispatch_slot_account
        ON engine_dispatch_slot(account_id,state,expires_at)""",
    """CREATE TABLE IF NOT EXISTS engine_provider_bucket (
        account_id TEXT PRIMARY KEY, tokens TEXT NOT NULL, updated_at TEXT NOT NULL)""",
)

# Runner-owned follow-ups (see service.py --followups). The legacy scheduled_jobs table is never used.
FOLLOWUP_KINDS = ("nudge", "discount", "post_buy")
# Kinds keyed to ONE offer id (payload carries it): a restart or a duplicate purchase event can queue each at most once.
OFFER_KEYED_KINDS = ("discount", "post_buy")
FOLLOWUP_MARK = "followup:"            # source_hash prefix of an input that answers a follow-up, not a fan message
FOLLOWUP_MAX_AHEAD = timedelta(hours=24)
FOLLOWUP_TTL = timedelta(hours=6)      # a job this long past its time is stale (downtime): dropped, never sent late
OFFER_PAYLOAD_KEY = {"discount": "discount_offer", "post_buy": "post_buy"}   # the payload key carrying that offer id
# Job-specific reasons: the job is dropped. Anything else (lease, allowance, unsettled send) is not the job's fault.
FOLLOWUP_DROPS = ("followup-not-queued", "followup-not-due", "followup-expired", "followup-chat-moved",
                  "followup-fan-replied", "input-already-claimed")


# Persistent-service pilots (see activate_service) are never capped by their own
# conversation budget: the account's delivery allowance is the cap. The
# conversation budget only has to stay above anything the allowance can grant
# (``limit_units`` is at most 10000), so it can never be the first thing to bind.
SERVICE_BUDGET_CEILING = 10000

# An input whose turn failed with NO provider send attempt standing (a model timeout/error, or a POST the
# provider certainly refused) may be answered again, a bounded number of times. The count lives in the
# input's state while it waits (``requeued:<n>``), so no schema change is needed.
REQUEUED = "requeued:"
MAX_REQUEUES_MODEL = 1
MAX_REQUEUES_PROVIDER = 2
REQUEUE_HORIZON = timedelta(hours=24)
# An outbound message with no automation kind and no employee is an UNKNOWN sender (a blast, a script, another tool). It
# does not count as an answer to a fan's message (engine v2; FASTT_UNLABELLED_COUNTS_AS_ANSWER=1 restores the old reading).
# Younger than the grace it still counts: our own just-sent reply is unlabelled until its attribution row is written.
UNLABELLED_GRACE = timedelta(seconds=90)
SERVICE_ID = re.compile(r"svc-[A-Za-z0-9_-]{1,92}")


def settled_pointer(con, account, fan, pointer, *, prefix=""):
    """Interpret an existing native pointer from its two positive durable owners.

    This does not clear the pointer or release any conversation lease. Other
    writers remain fenced until the existing final reporting step reconciles it.
    """
    if not (isinstance(pointer, str) and pointer.startswith("fastt-pilot-")
            and pointer[12:].isascii() and pointer[12:].isdigit()
            and len(pointer[12:]) <= 19 and 0 < int(pointer[12:]) < 2**63):
        return False
    return con.execute("SELECT 1 FROM " + prefix + "twin_bridge_send_attempts a JOIN " + prefix +
        "native_pilot_receipts r ON r.attempt_id=a.id WHERE a.id=? AND a.account_id=? AND a.fan_id=? AND "
        "((a.state='sent' AND r.state='sent') OR (a.state='refused' AND r.state='not_sent'))",
        (int(pointer[12:]), account, fan)).fetchone() is not None


class Ledger:
    def __init__(self, path: str | Path, *, allowance: bool = False, min_send_interval_s: float | None = None,
                 lock_scope: str | None = None, per_account_send_gap: bool = False,
                 tenant_id: str | None = None, send_concurrency: int = 4,
                 conversation_min_gap_s: float = 0.0, dispatch_lease_s: float = 120.0):
        # A PostgreSQL target is the application URL (never printed or logged).
        self.path = path if store.is_pg(path) else Path(path)
        self.pg = store.is_pg(path)
        # allowance=True (persistent service only): every admission also consumes
        # one unit of the account's EXISTING of_delivery_policy, in the same
        # transaction as the durable attempt. The bounded pilot never sets it.
        self.allowance = allowance
        # Engine v2: the one account this Ledger writes for (the per-account engine process). PostgreSQL write
        # transactions are then scoped to it (see twin_bridge.store.PG_LOCK_KEY) instead of one global lock.
        self.lock_scope = lock_scope
        # Engine v2: the account send interval is never below the floor (20 s), whatever the policy row says.
        from . import tuning
        # Per-account mode (FASTT_PER_ACCOUNT_SEND_GAP=1): the floor is the shared 10 s ceiling and the account's own
        # policy value decides the gap (see onlyfans_runtime.send_gap). Off: the 20 s floor, exactly as before.
        self.per_account_send_gap = bool(per_account_send_gap) and allowance
        floor = tuning.SEND_GAP_HARD_FLOOR_S if self.per_account_send_gap else tuning.MIN_SEND_INTERVAL_FLOOR_S
        self.min_send_interval_s = max(float(floor), float(min_send_interval_s or 0)) if allowance else 0.0
        # Concurrent mode is selected by the account's policy row. These are the process-side
        # ceilings; the durable row can only lower them.
        self.tenant_id = str(tenant_id or os.environ.get("ONLYFANS_WORKER_TENANT") or "default")
        self.send_concurrency = max(1, min(64, int(send_concurrency)))
        self.conversation_min_gap_s = max(0.0, min(3600.0, float(conversation_min_gap_s)))
        self.dispatch_lease_s = max(30.0, min(900.0, float(dispatch_lease_s)))
        self.fan_daily_limit = int(os.environ.get("FASTT_FAN_DAILY_REPLY_LIMIT", "2000")) if allowance else 0
        if not 0 <= self.fan_daily_limit <= 2000:
            raise ValueError("invalid-fan-daily-reply-limit")
        # Persistent service only, when fan photos are described (FASTT's inbound_describe): how
        # long a caption-less photo may wait for its description before it is skipped as before.
        # 0 = descriptions are not used here at all (the bounded pilot, every older fixture).
        self.describe_wait_s = 0
        # input_key -> how many times it has been requeued so far (set when a requeued input is re-claimed).
        self._requeues: dict[str, int] = {}

    def unlabelled_clause(self, alias: str, now) -> tuple[str, tuple]:
        """``(sql, params)``: a condition true for an outbound row that is NOT an answer (unknown sender, past the grace,
        not one of our own receipts), to be used as ``AND NOT (<sql>)`` where "a later message answers the fan" is
        decided. ``("", ())`` when the old reading is selected or the sender columns do not exist (older fixtures)."""
        if os.environ.get("FASTT_UNLABELLED_COUNTS_AS_ANSWER") == "1":
            return "", ()
        if getattr(self, "_sender_cols", None) is None:
            try:
                with store.readonly(self.path) as con:
                    if self.pg:
                        names = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                                           "WHERE table_schema='public' AND table_name='messages'")}
                    else:
                        names = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
                self._sender_cols = {"automation_kind", "sent_by_employee_id"} <= names
            except Exception:
                self._sender_cols = False
        if not self._sender_cols:
            return "", ()
        cutoff = _dt(datetime.fromisoformat(_dt(now)) - UNLABELLED_GRACE)
        a = alias
        return (f"({a}.direction='out' AND {a}.automation_kind IS NULL AND {a}.sent_by_employee_id IS NULL "
                f"AND CAST({a}.created_at AS TEXT)<? AND NOT EXISTS (SELECT 1 FROM native_pilot_receipts ur "
                f"WHERE ur.provider_message_id=CAST({a}.message_id AS TEXT)))", (cutoff,))

    def _check_target(self) -> None:
        # Never silently create an empty database at a misspelled production path.
        if not self.pg and not self.path.is_file():
            raise Refused("database-missing")

    @contextmanager
    def _tx(self, *, initialize: bool = False) -> Iterator[object]:
        self._check_target()
        with store.transaction(self.path, self.lock_scope) as con:
            if self.pg and con.execute("SELECT to_regnamespace('onlyfans_restore')").fetchone()[0]:
                raise Refused("restore-quarantined")
            # PostgreSQL: fastt_runtime tables are created by store.ensure_runtime_schema (runner/control), not here.
            if initialize and not self.pg:
                for statement in _SCHEMA:
                    con.execute(statement)
            yield con

    @contextmanager
    def _view(self) -> Iterator[object]:
        """Consistent metadata reads do not need the account writer lock.

        In particular a model/gateway status callback runs on the async loop;
        waiting there on a writer which needs that loop can deadlock admission.
        Quarantine and target checks remain identical to the write boundary.
        """
        self._check_target()
        with store.readonly(self.path) as con:
            if self.pg and con.execute("SELECT to_regnamespace('onlyfans_restore')").fetchone()[0]:
                raise Refused("restore-quarantined")
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
    def _described(con, p, message_id: int) -> bool:
        row = con.execute("SELECT image_desc FROM messages WHERE account_id=? AND fan_id=? AND message_id=?",
                          (p["account_id"], p["fan_id"], message_id)).fetchone()
        return bool(row and isinstance(row[0], str) and row[0].strip())

    def _source(self, con, p, now: str,
                exclude_input_key: str | None = None, ignore_newer_than: SourceSnapshot | None = None) -> SourceSnapshot | None:
        # Exclude this input's proven outputs, and completed replies whose recorded
        # input predates a newly arrived message. External/unknown sends still hold.
        latest_in = con.execute(
            "SELECT message_id FROM messages WHERE account_id=? AND fan_id=? AND direction='in' "
            "ORDER BY created_at DESC,message_id DESC LIMIT 1", (p["account_id"], p["fan_id"])).fetchone()
        overlap = overlapped_replies(con, p["account_id"], p["fan_id"], latest_in[0]) if latest_in else []
        ignored = {int(r[0]) for r in overlap}
        covered = {int(mid) for r in overlap for mid in json.loads(r[1])}
        overlap_sql = ("AND m.message_id NOT IN (" + ",".join("?" for _ in ignored) + ") ") if ignored else ""
        unl_sql, unl_args = self.unlabelled_clause("m", now)
        rows = con.execute("SELECT m.message_id,m.direction,m.body,m.media_ids,m.media_count,m.price_cents,"
                          "m.is_tip,m.is_unsent,m.created_at,m.ingested_at,m.raw_json FROM messages m "
                          "WHERE m.account_id=? AND m.fan_id=? AND NOT (m.direction='out' AND m.is_unsent) "
                          "AND NOT EXISTS (SELECT 1 FROM native_pilot_receipts r "
                          "WHERE r.pilot_id=? AND r.input_key=? AND r.state='sent' "
                          "AND r.provider_message_id=CAST(m.message_id AS TEXT)) "
                          + overlap_sql + (f"AND NOT {unl_sql} " if unl_sql else "")
                          + "ORDER BY m.created_at DESC,m.message_id DESC LIMIT 101",
                          (p["account_id"], p["fan_id"], p["pilot_id"], exclude_input_key, *sorted(ignored), *unl_args)).fetchall()
        if ignore_newer_than is not None:
            # Hard-maximum burst: the reply was prepared for ``ignore_newer_than``; fan messages that arrived after its
            # newest member (and only those, only inbound) do not invalidate it. They stay unanswered and are
            # answered next (see overlap: a reply to an older input never consumes a later message).
            anchor = (ignore_newer_than.created_at, ignore_newer_than.message_id)
            while rows and rows[0]["direction"] == "in" and (_dt(rows[0]["created_at"]), rows[0]["message_id"]) > anchor:
                rows = rows[1:]
        if not rows or rows[0]["direction"] != "in":
            return None
        members = []
        for row in rows:
            if row["direction"] != "in" or row["message_id"] in covered:
                break
            try:
                created, ingested = _dt(row["created_at"]), _dt(row["ingested_at"])
            except Refused:
                return None
            if created <= p["cutoff"]:
                break
            if not (p["cutoff"] < ingested <= now) or created > now:
                return None
            if row["is_unsent"] != 0 or not isinstance(row["message_id"], int) or row["message_id"] <= 0:
                return None
            try:
                media = json.loads(row["media_ids"])
            except (TypeError, ValueError):
                return None
            if not isinstance(media, list):
                return None
            text = isinstance(row["body"], str) and bool(row["body"].strip())
            if row["is_tip"] != 0 or row["price_cents"] != 0 or row["media_count"] != 0 or media:
                # A fan photo/video/gif/tip/paid message does not stop the AI: intake flagged the
                # chat for a human (who may take over). Its caption is answerable text; without
                # one the row is not an input, so the AI answers his text around it.
                if not text:
                    plain_media = (row["is_tip"] == 0 and row["price_cents"] == 0
                                   and (row["media_count"] != 0 or bool(media)))
                    if not (self.describe_wait_s and plain_media):
                        continue
                    if not self._described(con, p, row["message_id"]):
                        # A description may still be on its way: hold the chat rather than answer
                        # around his photo; once the window passes it is skipped as before.
                        if now < _dt(datetime.fromisoformat(ingested) + timedelta(seconds=self.describe_wait_s)):
                            return None
                        continue
                    # A described photo is answerable input (its description is what the AI reads).
            elif not text:
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

    def unanswered_reply_ids(self, *, pilot_id: str, owner_token: str, input_key: str, now) -> tuple[int, ...]:
        """Receipt-proven exclusions for the claimed input's native history/last-look gates."""
        with self._tx() as con:
            p = self._active(con, pilot_id, owner_token, _dt(now))
            i = con.execute("SELECT message_id FROM native_pilot_inputs WHERE pilot_id=? AND input_key=? "
                            "AND state='generating'", (pilot_id, input_key)).fetchone()
            if i is None:
                raise Refused("input-not-generating")
            return tuple(sorted({int(r[0]) for r in overlapped_replies(
                con, p["account_id"], p["fan_id"], i[0])}))

    def claim_input(self, *, pilot_id: str, owner_token: str, snapshot: SourceSnapshot,
                    now: datetime | str, reply_authority=None) -> str:
        """Claim before a model call; recovery requires explicit pre-wire proof."""
        if not isinstance(snapshot, SourceSnapshot):
            raise Refused("invalid-snapshot")
        now_s = _dt(now)
        with self._tx() as con:
            p = self._active(con, pilot_id, owner_token, now_s)
            if self._source(con, p, now_s) != snapshot:
                raise Refused("source-changed")
            if con.execute("SELECT 1 FROM native_pilot_inputs WHERE pilot_id=? AND state='generating'", (pilot_id,)).fetchone():
                raise Refused("input-already-claimed")
            prior = con.execute("SELECT input_key,state FROM native_pilot_inputs WHERE pilot_id=? AND message_id=?",
                                (pilot_id, snapshot.message_id)).fetchone()
            if prior is not None:
                if not str(prior["state"]).startswith(REQUEUED) or prior["input_key"] != _hash([pilot_id, snapshot.message_id]):
                    raise Refused("input-already-claimed")
                # A requeued input (nothing was sent for it) is claimed again on the freshly proven snapshot.
                con.execute("UPDATE native_pilot_inputs SET source_hash=?,created_at=?,ingested_at=?,member_ids_json=?,"
                            "earliest_created_at=?,state='generating',claimed_at=?,updated_at=? WHERE input_key=?",
                            (snapshot.source_hash, snapshot.created_at, snapshot.ingested_at, _json(snapshot.member_ids),
                             snapshot.earliest_created_at, now_s, now_s, prior["input_key"]))
                if reply_authority is not None:
                    from .pacing import claimed
                    claimed(con, reply_authority, prior["input_key"], now_s)
                self._requeues[prior["input_key"]] = int(str(prior["state"])[len(REQUEUED):] or 1)
                return prior["input_key"]
            key = _hash([pilot_id, snapshot.message_id])
            if reply_authority is not None:
                from .pacing import claimed
                claimed(con, reply_authority, key, now_s)
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

    def _canonical_guard(self, con, pilot, *, lock=False, own_attempt=None):
        """Order native admission with Twin controls when this is a managed account.

        The pilot advisory lock is already held on admission. Canonical controls
        never acquire it: both sides serialize on the existing account runtime row.
        Standalone fixtures/pilots without a canonical account retain their contract.
        """
        if self.pg and con.execute("SELECT to_regnamespace('onlyfans_restore')").fetchone()[0]:
            raise Refused("restore-quarantined")
        if not self.pg or not store.has_table(con, "public.of_runtime"):
            return False
        # In concurrent mode delivery admission takes a shared runtime lock:
        # different fans may overlap, while a control transition still takes the
        # exclusive lock and waits for the short admission transaction to settle.
        # Legacy gap mode retains the original exclusive lock.
        from . import admission
        policy = self._policy_row(con, pilot["account_id"])
        concurrent = policy is not None and admission.is_concurrent(policy)
        lock_sql = (" FOR SHARE" if concurrent else " FOR UPDATE") if lock else ""
        runtime = con.execute("SELECT enabled,binding_id FROM public.of_runtime WHERE account_id=?" +
            lock_sql, (pilot["account_id"],)).fetchone()
        if runtime is None:
            return False
        binding = con.execute("SELECT account_id,channel,revoked_at FROM public.of_creator_bindings WHERE id=?",
            (runtime["binding_id"],)).fetchone()
        state = con.execute("SELECT s.mode,s.takeover_state,s.unresolved_attempt_id,f.is_muted "
            "FROM public.of_conversation_state s JOIN public.fans f ON f.account_id=s.account_id AND f.fan_id=s.fan_id "
            "WHERE s.account_id=? AND s.fan_id=?", (pilot["account_id"],pilot["fan_id"])).fetchone()
        from .reporting import attempt_ref
        own = attempt_ref(own_attempt) if own_attempt is not None else None
        pointer = state["unresolved_attempt_id"] if state else None
        settled = settled_pointer(con, pilot["account_id"], pilot["fan_id"], pointer)
        if (not runtime["enabled"] or not binding or binding["account_id"] != pilot["account_id"]
                or binding["channel"] != "onlyfans" or binding["revoked_at"] is not None
                or not state or state["mode"] != "automated" or state["takeover_state"] != "none"
                or state["is_muted"] or (pointer not in (None,own) and not settled)):
            raise Refused("canonical-control-held")
        from onlyfans_read.contract import UNRESOLVED_OUTCOMES
        values=tuple(sorted(UNRESOLVED_OUTCOMES))
        if con.execute("SELECT 1 FROM public.of_message_delivery WHERE account_id=? AND fan_id=? "
                "AND outcome IN (" + ",".join("?" for _ in values) + ") LIMIT 1",
                (pilot["account_id"],pilot["fan_id"],*values)).fetchone():
            raise Refused("canonical-delivery-unresolved")
        return True

    def begin_attempt(self, *, pilot_id: str, owner_token: str, input_key: str,
                      bubble_no: int, revision: int, now: datetime | str, tolerate_newer: bool = False,
                      reply_authority=None) -> Attempt:
        """Atomic final source + authority check, budget charge, durable intent."""
        if not isinstance(bubble_no, int) or isinstance(bubble_no, bool) or not 1 <= bubble_no < 2**31:
            raise Refused("invalid-bubble")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise Refused("invalid-revision")
        now_s = _dt(now)
        with self._tx() as con:
            p = self._active(con, pilot_id, owner_token, now_s)
            if bubble_no > 10:
                # Preserve the ten-bubble limit, excluding definitively refused attempts.
                # Each retry still gets a distinct immutable attempt/receipt sequence number.
                refused = con.execute("SELECT count(*) FROM native_pilot_receipts WHERE pilot_id=? AND input_key=? "
                                      "AND state='not_sent'", (pilot_id, input_key)).fetchone()[0] if self.allowance else 0
                if bubble_no - refused > 10:
                    raise Refused("invalid-bubble")
            if revision != p["revision"]:
                raise Refused("revision-changed")
            i = con.execute("SELECT * FROM native_pilot_inputs WHERE input_key=? AND pilot_id=?", (input_key, pilot_id)).fetchone()
            if i is None or i["state"] != "generating":
                raise Refused("input-not-generating")
            expected = SourceSnapshot(i["message_id"], i["source_hash"], i["created_at"], i["ingested_at"],
                                      tuple(json.loads(i["member_ids_json"])), i["earliest_created_at"])
            if i["source_hash"].startswith(FOLLOWUP_MARK):
                if not self._followup_unchanged(con, p, i):
                    raise Refused("source-changed")
            elif self._source(con, p, now_s, exclude_input_key=input_key,
                              ignore_newer_than=expected if tolerate_newer else None) != expected:
                raise Refused("source-changed")
            if con.execute("SELECT 1 FROM native_pilot_receipts WHERE input_key=? AND bubble_no=?", (input_key, bubble_no)).fetchone():
                raise Refused("attempt-already-exists")
            # Do not hold the account runtime lock while the per-fan source
            # query above runs. The final short section re-locks it, rechecks
            # the pilot revision and performs only the atomic admission writes.
            canonical = self._canonical_guard(con, p, lock=True)
            if reply_authority is not None:
                from .pacing import guard
                guard(con, reply_authority, now_s)
            p = self._active(con, pilot_id, owner_token, now_s)
            if revision != p["revision"]:
                raise Refused("revision-changed")
            if self.allowance:
                from . import admission
                policy = self._policy_row(con, p["account_id"])
                if admission.is_concurrent(policy):
                    slot_id = self._consume_concurrent(con, p, now_s, input_key=input_key, bubble_no=bubble_no)
                else:
                    slot_id = None
                    self._consume_allowance(con, p["account_id"], now_s, fan_id=p["fan_id"],
                                            daily_limit=self.fan_daily_limit)
            else:
                slot_id = None
            tid = "native-pilot-" + _hash([pilot_id, input_key, bubble_no])
            aid = store.insert_id(con, "INSERT INTO twin_bridge_send_attempts(account_id,fan_id,turn_id,attempt_no,state,"
                                  "owner_token,revision,created_at,updated_at) VALUES(?,?,?,1,'dispatching',?,?,?,?)",
                                  (p["account_id"], p["fan_id"], tid, owner_token, revision, now_s, now_s))
            if slot_id is not None:
                con.execute("UPDATE engine_dispatch_slot SET attempt_id=? WHERE slot_id=?", (str(aid), slot_id))
                con.execute("UPDATE engine_conversation_lease SET attempt_id=?,holder=?,updated_at=? "
                            "WHERE tenant_id=? AND account_id=? AND fan_id=?",
                            (str(aid), owner_token, now_s, self.tenant_id, p["account_id"], p["fan_id"]))
            con.execute("UPDATE twin_bridge_conversations SET send_budget_used=send_budget_used+1,updated_at=? "
                        "WHERE account_id=? AND fan_id=?", (now_s, p["account_id"], p["fan_id"]))
            con.execute("INSERT INTO native_pilot_receipts(attempt_id,pilot_id,input_key,bubble_no,state,created_at,updated_at) "
                        "VALUES(?,?,?,?,'dispatching',?,?)", (aid, pilot_id, input_key, bubble_no, now_s, now_s))
            if canonical:
                from .reporting import attempt_ref
                con.execute("UPDATE public.of_conversation_state SET unresolved_attempt_id=? WHERE account_id=? AND fan_id=?",
                    (attempt_ref(aid),p["account_id"],p["fan_id"]))
            return Attempt(aid, p["account_id"], p["fan_id"], tid, 1, owner_token, revision)

    def _gap(self, throttle_seconds) -> float:
        """Seconds between two sends on the account for this policy row's throttle value."""
        from onlyfans_runtime.send_gap import effective_gap
        return effective_gap(throttle_seconds, self.min_send_interval_s, self.per_account_send_gap)

    @staticmethod
    def _policy_row(con, account: str):
        # SELECT * so a store without the optional daily_limit_units column (older schema, fixtures) still reads.
        return con.execute("SELECT * FROM of_delivery_policy WHERE account_id=?", (account,)).fetchone()

    @staticmethod
    def _daily_cap(row) -> int | None:
        """The account's optional daily send cap (``of_delivery_policy.daily_limit_units``); None = no daily cap."""
        try:
            value = row["daily_limit_units"]
        except (KeyError, IndexError):
            return None
        return None if value is None else int(value)

    @staticmethod
    def _account_daily_used(con, account: str, now_s: str) -> int:
        """Sends charged to the account in the rolling last 24 hours: the same states the per-fan quota counts
        (dispatching, sent, delivery_unknown), so an unknown outcome stays charged and a definite non-delivery
        (not_sent) gives its place back. A rolling window needs no reset job and cannot drift."""
        cutoff = _dt(datetime.fromisoformat(now_s) - timedelta(hours=24))
        total = con.execute(
            "SELECT count(*) FROM native_pilot_receipts r JOIN twin_bridge_send_attempts a ON a.id=r.attempt_id "
            "WHERE a.account_id=? AND r.created_at>? AND r.created_at<=? "
            "AND r.state IN ('dispatching','sent','delivery_unknown')", (account, cutoff, now_s)).fetchone()[0]
        if store.has_table(con, "of_text_intent"):
            total += con.execute(
                "SELECT count(*) FROM of_text_intent WHERE account_id=? "
                "AND status IN ('dispatching','accepted','unknown') "
                "AND COALESCE(dispatched_at,created_at)>? "
                "AND COALESCE(dispatched_at,created_at)<=?",
                (account, cutoff, now_s)).fetchone()[0]
        return total

    def _consume_allowance(self, con, account: str, now_s: str, *, fan_id: int | None = None, daily_limit: int = 0) -> None:
        """Admit one send under the daily fan quota or legacy account allowance.

        Admission and receipt creation share a serialized transaction. Daily
        mode preserves historical account counters; unknown outcomes stay charged.
        Account throttle, active intents and in-flight FASTT attempts still fence
        both modes. Only definite non-delivery releases a quota place.
        """
        # The text path locks this same policy row. Taking it here makes the
        # daily counter and token-bucket decision shared across native,
        # manual and campaign senders rather than merely process-local.
        row = con.execute("SELECT * FROM of_delivery_policy WHERE account_id=? FOR UPDATE",
                          (account,)).fetchone() if self.pg else self._policy_row(con, account)
        if row is None:
            raise Refused("allowance-missing")
        if not row["enabled"]:
            raise Refused("allowance-disabled")
        daily = bool(daily_limit and fan_id is not None)
        used = self._fan_daily_used(con, account, fan_id, now_s) if daily else row["used_units"]
        if used >= (daily_limit if daily else row["limit_units"]):
            raise Refused("allowance-exhausted")
        cap = self._daily_cap(row)
        if cap is not None and self._account_daily_used(con, account, now_s) >= cap:
            raise Refused("allowance-daily-cap")
        if row["active_intent_id"] or con.execute("SELECT 1 FROM twin_bridge_send_attempts WHERE account_id=? "
                "AND state='dispatching' LIMIT 1", (account,)).fetchone():
            raise Refused("allowance-busy")
        if row["next_dispatch_at"] is not None and _dt(row["next_dispatch_at"]) > now_s:
            raise Refused("allowance-throttled")
        due = _dt(datetime.fromisoformat(now_s) + timedelta(seconds=self._gap(row["throttle_seconds"])))
        charge = "used_units," if daily else "used_units+1,"
        ceiling = "" if daily else "AND used_units<limit_units "
        cur = con.execute("UPDATE of_delivery_policy SET used_units=" + charge + "next_dispatch_at=? "
                          "WHERE account_id=? AND enabled AND used_units=? " + ceiling +
                          "AND active_intent_id IS NULL",
                          (due, account, row["used_units"]))
        if cur.rowcount != 1:
            raise Refused("allowance-changed")

    # ── concurrent delivery (account-specific opt-in) ────────────────────────────────────────────────
    def delivery_mode(self, account: str) -> str:
        """Read the account's delivery mode without opening a write transaction."""
        from . import admission
        with store.readonly(self.path) as con:
            row = self._policy_row(con, account)
        return admission.delivery_mode(row)

    @staticmethod
    def _policy_value(row, name: str, default=None):
        from . import admission
        return admission.value(row, name, default)

    def _consume_concurrent(self, con, p, now_s: str, *, input_key: str, bubble_no: int) -> str:
        """Admit one provider request for one conversation under account budgets.

        Serialised by the caller's per-account transaction. Different conversations can hold
        different slots at the same time; the same conversation can hold at most one.
        """
        from . import admission
        account, fan, owner = p["account_id"], p["fan_id"], p["owner_token"]
        now_dt = datetime.fromisoformat(now_s)
        row = con.execute("SELECT * FROM of_delivery_policy WHERE account_id=? FOR UPDATE",
                          (account,)).fetchone() if self.pg else self._policy_row(con, account)
        if row is None:
            raise Refused("allowance-missing")
        if not row["enabled"]:
            raise Refused("allowance-disabled")
        used = self._fan_daily_used(con, account, fan, now_s) if self.fan_daily_limit else row["used_units"]
        if used >= (self.fan_daily_limit if self.fan_daily_limit else row["limit_units"]):
            raise Refused("allowance-exhausted")
        cap = self._daily_cap(row)
        if cap is not None and self._account_daily_used(con, account, now_s) >= cap:
            raise Refused("allowance-daily-cap")
        cooldown = admission.cooldown_at(row)
        if cooldown is not None and _dt(cooldown) > now_s:
            raise Refused("allowance-throttled")
        if con.execute("SELECT 1 FROM twin_bridge_send_attempts WHERE account_id=? AND fan_id=? "
                       "AND state IN ('dispatching','delivery_unknown') LIMIT 1", (account, fan)).fetchone():
            raise Refused("delivery-unresolved")
        lease = con.execute("SELECT * FROM engine_conversation_lease WHERE tenant_id=? AND account_id=? AND fan_id=?",
                            (self.tenant_id, account, fan)).fetchone()
        if lease is not None:
            if lease["state"] == "unresolved":
                raise Refused("delivery-unresolved")
            if (lease["state"] == "held" and _dt(lease["expires_at"]) > now_s
                    and lease["holder"] != owner):
                raise Refused("conversation-busy")
            if lease["next_send_at"] is not None and _dt(lease["next_send_at"]) > now_s:
                raise Refused("conversation-throttled")
        expires = _dt(now_dt + timedelta(seconds=self.dispatch_lease_s))
        if lease is None:
            con.execute("INSERT INTO engine_conversation_lease(tenant_id,account_id,fan_id,holder,owner_token,attempt_id,"
                        "state,acquired_at,heartbeat_at,expires_at,next_send_at,updated_at) "
                        "VALUES(?,?,?,?,?,NULL,'held',?,?,?,NULL,?)",
                        (self.tenant_id, account, fan, owner, owner, now_s, now_s, expires, now_s))
        else:
            con.execute("UPDATE engine_conversation_lease SET holder=?,owner_token=?,attempt_id=NULL,state='held',"
                        "acquired_at=?,heartbeat_at=?,expires_at=?,updated_at=? "
                        "WHERE tenant_id=? AND account_id=? AND fan_id=?",
                        (owner, owner, now_s, now_s, expires, now_s, self.tenant_id, account, fan))
        con.execute("DELETE FROM engine_dispatch_slot WHERE account_id=? AND state='held' AND expires_at<=?",
                    (account, now_s))
        in_flight = con.execute("SELECT count(*) FROM engine_dispatch_slot WHERE account_id=? AND state='held' "
                                "AND expires_at>?", (account, now_s)).fetchone()[0]
        limit = admission.max_concurrent(row, self.send_concurrency)
        if in_flight >= limit:
            raise Refused("provider-concurrency-limit")
        rate, capacity = admission.requests_per_second(row), admission.burst(row)
        bucket = con.execute("SELECT tokens,updated_at FROM engine_provider_bucket WHERE account_id=?", (account,)).fetchone()
        if bucket is None:
            tokens = float(capacity)
            bucket_clock = now_dt
        else:
            tokens, bucket_clock = admission.refill_state(float(bucket["tokens"]), datetime.fromisoformat(_dt(bucket["updated_at"])),
                                      now_dt, rate, capacity)
        if tokens < 1.0:
            raise Refused("allowance-throttled")
        tokens -= 1.0
        con.execute("INSERT INTO engine_provider_bucket(account_id,tokens,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(account_id) DO UPDATE SET tokens=excluded.tokens,updated_at=excluded.updated_at",
                    (account, str(tokens), _dt(bucket_clock)))
        # A reply can contain several bubbles. ``now_s`` alone is not unique at
        # second precision, so the input and bubble are part of the slot identity;
        # otherwise a fast second bubble would collide with the first slot.
        slot_id = "engine-slot-" + _hash(
            [p["pilot_id"], p["account_id"], p["fan_id"], input_key, bubble_no, owner])
        con.execute("INSERT INTO engine_dispatch_slot(slot_id,tenant_id,account_id,fan_id,attempt_id,state,admitted_at,expires_at) "
                    "VALUES(?,?,?,?,NULL,'held',?,?)",
                    (slot_id, self.tenant_id, account, fan, now_s, expires))
        if not self.fan_daily_limit:
            con.execute("UPDATE of_delivery_policy SET used_units=used_units+1,revision=revision "
                        "WHERE account_id=? AND used_units<limit_units", (account,))
        return slot_id

    def _release_concurrent(self, con, pilot_id: str, attempt_id: int, outcome: str, now_s: str,
                            *, keep_conversation: bool = False) -> None:
        """Release the account slot and either free or hold this conversation."""
        if not self.allowance:
            return
        from . import admission
        p = con.execute("SELECT account_id,fan_id FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
        if p is None:
            return
        con.execute("DELETE FROM engine_dispatch_slot WHERE attempt_id=? AND account_id=?",
                    (str(attempt_id), p["account_id"]))
        if keep_conversation:
            return
        lease = con.execute("SELECT * FROM engine_conversation_lease WHERE tenant_id=? AND account_id=? AND fan_id=?",
                            (self.tenant_id, p["account_id"], p["fan_id"])).fetchone()
        if lease is None or lease["attempt_id"] not in (str(attempt_id), attempt_id):
            return
        row = self._policy_row(con, p["account_id"])
        gap = admission.conversation_gap_seconds(row) if row is not None else self.conversation_min_gap_s
        gap = max(gap, self.conversation_min_gap_s)
        if outcome == "unresolved":
            con.execute("UPDATE engine_conversation_lease SET state='unresolved',heartbeat_at=?,expires_at=?,updated_at=? "
                        "WHERE tenant_id=? AND account_id=? AND fan_id=?",
                        (now_s, _dt(datetime.fromisoformat(now_s) + timedelta(days=1)), now_s,
                         self.tenant_id, p["account_id"], p["fan_id"]))
            return
        next_send = _dt(datetime.fromisoformat(now_s) + timedelta(seconds=gap))
        con.execute("UPDATE engine_conversation_lease SET state='released',holder='',owner_token='',attempt_id=NULL,"
                    "expires_at=?,next_send_at=?,updated_at=? WHERE tenant_id=? AND account_id=? AND fan_id=?",
                    (now_s, next_send, now_s, self.tenant_id, p["account_id"], p["fan_id"]))

    @staticmethod
    def _fan_daily_used(con, account: str, fan_id: int, now_s: str) -> int:
        # Dispatching and unknown outcomes remain charged. No counters are reset.
        cutoff = _dt(datetime.fromisoformat(now_s) - timedelta(hours=24))
        return con.execute(
            "SELECT count(*) FROM native_pilot_receipts r "
            "JOIN twin_bridge_send_attempts a ON a.id=r.attempt_id "
            "WHERE a.account_id=? AND a.fan_id=? AND r.created_at>? AND r.created_at<=? "
            "AND r.state IN ('dispatching','sent','delivery_unknown')",
            (account, fan_id, cutoff, now_s)).fetchone()[0]

    # ── operator-visible alerts and the provider-throttle backoff ──────────────────────────────────────────
    ALERT_PROVIDER_THROTTLE = "provider-throttle"
    ALERT_SEND_STALL = "send-stall"

    def raise_alert(self, account: str, kind: str, now, detail: dict | None = None) -> bool:
        """Open (or refresh) the account's alert of this kind. True when it was newly opened (or re-opened).

        One row per (account, kind); ``count`` grows while the condition repeats. Best effort for the caller: it
        raises like any ledger write, and the engine wraps it so an alert can never stop a reply."""
        import json
        now_s = _dt(now)
        blob = json.dumps(detail or {}, sort_keys=True)[:2000]
        with self._tx(initialize=True) as con:
            row = con.execute("SELECT state,count FROM engine_alert WHERE account_id=? AND kind=?", (account, kind)).fetchone()
            if row is None:
                con.execute("INSERT INTO engine_alert(account_id,kind,state,first_at,last_at,count,detail_json) VALUES(?,?,'open',?,?,1,?)",
                            (account, kind, now_s, now_s, blob))
                return True
            if row["state"] != "open":      # a cleared alert that happens again starts a new episode
                con.execute("UPDATE engine_alert SET state='open',first_at=?,last_at=?,count=1,detail_json=? WHERE account_id=? AND kind=?",
                            (now_s, now_s, blob, account, kind))
                return True
            con.execute("UPDATE engine_alert SET last_at=?,count=count+1,detail_json=? WHERE account_id=? AND kind=?",
                        (now_s, blob, account, kind))
            return False

    def clear_alert(self, account: str, kind: str, now) -> bool:
        """Mark an open alert cleared. True when one was open."""
        with self._tx(initialize=True) as con:
            cur = con.execute("UPDATE engine_alert SET state='cleared',last_at=? WHERE account_id=? AND kind=? AND state='open'",
                              (_dt(now), account, kind))
            return cur.rowcount == 1

    def alerts(self, account: str | None = None, *, only_open: bool = True) -> list[dict]:
        with store.readonly(self.path) as con:
            if not store.has_table(con, "engine_alert"):
                return []
            sql = "SELECT account_id,kind,state,first_at,last_at,count,detail_json FROM engine_alert WHERE 1=1"
            args: list = []
            if account is not None:
                sql += " AND account_id=?"; args.append(account)
            if only_open:
                sql += " AND state='open'"
            return [dict(r) for r in con.execute(sql + " ORDER BY account_id,kind", tuple(args)).fetchall()]

    def sent_since(self, account: str, since) -> int:
        """How many sends of this account the ledger saw settle ``sent`` since ``since`` (0 = nothing moved)."""
        since_s = _dt(since)
        with store.readonly(self.path) as con:
            return int(con.execute(
                "SELECT count(*) FROM native_pilot_receipts r JOIN twin_bridge_send_attempts a ON a.id=r.attempt_id "
                "WHERE a.account_id=? AND r.state='sent' AND r.updated_at>?", (account, since_s)).fetchone()[0])

    def throttle_backoff(self, account: str, now, *, retry_after=None) -> dict:
        """Honour a provider 429 at account level.

        Legacy mode returns the account to the 20 s default gap. Concurrent mode does not
        invent a permanent gap: it records a durable cooldown (Retry-After when supplied)
        that applies to every fan, and leaves the configured request rate intact.

        Returns ``{"changed": bool, "before": int|None, "after": int|None, "alert": bool}``."""
        import json
        from onlyfans_runtime.send_gap import DEFAULT_S
        from . import admission
        default = int(DEFAULT_S)
        now_s = _dt(now)
        changed, before, revision = False, None, None
        cooldown = None
        audit_before = audit_after = None
        audit_reason = "Provider rate limit (HTTP 429): send gap returned to the 20 s default"
        with self._tx(initialize=True) as con:
            row = self._policy_row(con, account)
            if row is not None:
                if admission.is_concurrent(row):
                    # Retry-After is authoritative when present. The ten-second default is
                    # only for a 429 that did not tell us when to retry.
                    delay = max(0.0, min(86400.0, float(retry_after) if retry_after is not None else 10.0))
                    cooldown = _dt(datetime.fromisoformat(now_s) + timedelta(seconds=delay))
                    before = admission.cooldown_at(row)
                    audit_before = {"provider_cooldown_until": None if before is None else str(before)}
                    audit_after = {"provider_cooldown_until": cooldown}
                    audit_reason = "Provider rate limit (HTTP 429): account cooldown set from Retry-After"
                    has_cooldown_column = True
                    try:
                        row["provider_cooldown_until"]
                    except (KeyError, IndexError):
                        has_cooldown_column = False
                    if has_cooldown_column:
                        cur = con.execute(
                            "UPDATE of_delivery_policy SET provider_cooldown_until=CASE "
                            "WHEN provider_cooldown_until IS NULL OR provider_cooldown_until<? THEN ? "
                            "ELSE provider_cooldown_until END WHERE account_id=?",
                            (cooldown, cooldown, account))
                    else:
                        cur = con.execute(
                            "UPDATE of_delivery_policy SET next_dispatch_at=CASE "
                            "WHEN next_dispatch_at IS NULL OR next_dispatch_at<? THEN ? ELSE next_dispatch_at END "
                            " WHERE account_id=?", (cooldown, cooldown, account))
                    changed = cur.rowcount == 1 and (before is None or _dt(before) < cooldown)
                    after = admission.cooldown_at(
                        con.execute("SELECT * FROM of_delivery_policy WHERE account_id=?", (account,)).fetchone())
                else:
                    before = int(row["throttle_seconds"])
                    audit_before = {"throttle_seconds": before}
                    if before < default:
                        cur = con.execute("UPDATE of_delivery_policy SET throttle_seconds=?,revision=revision+1 WHERE account_id=? AND throttle_seconds<?",
                                          (default, account, default))
                        changed = cur.rowcount == 1
                        if changed:
                            revision = con.execute("SELECT revision FROM of_delivery_policy WHERE account_id=?", (account,)).fetchone()[0]
                    cooldown = _dt(datetime.fromisoformat(now_s) + timedelta(seconds=default))
                    audit_after = {"throttle_seconds": default if changed else before}
        if not (row is not None and admission.is_concurrent(row)):
            after = default if changed else before
        if changed and revision is not None:
            # Audit in its own transaction: a role that may not write the change log must not lose the backoff itself.
            try:
                with self._tx() as con:
                    if store.has_table(con, "of_delivery_policy_change"):
                        con.execute("INSERT INTO of_delivery_policy_change(account_id,revision,actor,reason,before_json,after_json,created_at) "
                                    "VALUES(?,?,?,?,?,?,?)",
                                    (account, revision, "fastt-engine-backoff", audit_reason,
                                     json.dumps(audit_before, sort_keys=True),
                                     json.dumps(audit_after, sort_keys=True), now_s))
            except Exception:
                pass
        opened = self.raise_alert(account, self.ALERT_PROVIDER_THROTTLE, now,
                                  {"throttle_before": before, "throttle_after": after,
                                   "cooldown_until": cooldown, "retry_after": retry_after})
        return {"changed": changed, "before": before, "after": after, "cooldown_until": cooldown, "alert": opened}

    def allowance_status(self, account: str, fan_id: int | None = None, *, now=None) -> dict | None:
        """Read-only copy of the account's delivery policy numbers (None when absent)."""
        from . import admission
        with store.readonly(self.path) as con:
            row = self._policy_row(con, account)
            daily = bool(self.fan_daily_limit and fan_id is not None)
            now_s = _dt(now or datetime.utcnow())
            used = self._fan_daily_used(con, account, fan_id, now_s) if row is not None and daily else None
            cap = self._daily_cap(row) if row is not None else None
            cap_used = self._account_daily_used(con, account, now_s) if cap is not None else None
            in_flight = None
            conversation_next = None
            provider_wait = None
            if row is not None and admission.is_concurrent(row):
                in_flight = int(con.execute(
                    "SELECT count(*) FROM engine_dispatch_slot WHERE account_id=? AND state='held' AND expires_at>?",
                    (account, now_s)).fetchone()[0])
                bucket = con.execute(
                    "SELECT tokens,updated_at FROM engine_provider_bucket WHERE account_id=?",
                    (account,)).fetchone()
                if bucket is not None:
                    tokens = admission.refill(
                        float(bucket["tokens"]), datetime.fromisoformat(_dt(bucket["updated_at"])),
                        datetime.fromisoformat(now_s), admission.requests_per_second(row), admission.burst(row))
                    provider_wait = admission.token_wait(tokens, admission.requests_per_second(row))
                else:
                    provider_wait = 0.0
                if fan_id is not None:
                    lease = con.execute(
                        "SELECT next_send_at FROM engine_conversation_lease WHERE tenant_id=? AND account_id=? AND fan_id=?",
                        (self.tenant_id, account, fan_id)).fetchone()
                    conversation_next = None if lease is None else lease["next_send_at"]
        if row is None:
            return None
        due = row["next_dispatch_at"]
        mode = admission.delivery_mode(row)
        cooldown = admission.cooldown_at(row)
        return {"enabled": bool(row["enabled"]), "delivery_mode": mode,
                "limit": self.fan_daily_limit if daily else row["limit_units"], "used": used if daily else row["used_units"],
                "daily_limit": cap, "daily_used": cap_used,
                "throttle_seconds": self._gap(row["throttle_seconds"]),
                "provider_requests_per_second": admission.requests_per_second(row),
                "provider_burst": admission.burst(row),
                "max_concurrent": admission.max_concurrent(row, self.send_concurrency),
                "in_flight": in_flight,
                "provider_wait_s": provider_wait,
                "conversation_next_send_at": None if conversation_next is None else _dt(conversation_next),
                "busy": bool(row["active_intent_id"]),
                "cooldown_until": None if cooldown is None else _dt(cooldown),
                "next_dispatch_at": None if due is None else _dt(due)}

    def validate_dispatch(self, *, pilot_id: str, owner_token: str, attempt_id: int,
                          now: datetime | str, tolerate_newer: bool = False,
                          reply_authority=None) -> None:
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
            self._canonical_guard(con, p, own_attempt=attempt_id)
            if reply_authority is not None:
                from .pacing import guard
                guard(con, reply_authority, now_s)
            policy = self._policy_row(con, p["account_id"]) if self.allowance else None
            if policy is not None:
                from . import admission
                cooldown = admission.cooldown_at(policy) if admission.is_concurrent(policy) else None
                if cooldown is not None and _dt(cooldown) > now_s:
                    raise Refused("allowance-throttled")
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
            if i["source_hash"].startswith(FOLLOWUP_MARK):
                if not self._followup_unchanged(con, p, i):
                    raise Refused("source-changed")
            elif self._source(con, p, now_s, exclude_input_key=r["input_key"],
                              ignore_newer_than=expected if tolerate_newer else None) != expected:
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
            retained = json.loads(r["evidence_json"] or "{}")
            if "dispatch_intent" in retained:
                if "dispatch_intent" in evidence and evidence["dispatch_intent"] != retained["dispatch_intent"]:
                    raise Refused("frozen-dispatch-conflict")
                retained.update(evidence)
                if retained.get("canonical_quote"):
                    retained["quote_lifecycle"] = "provider_accepted"
                evidence_s = _json(retained)
                if len(evidence_s.encode("utf-8")) > 65536:
                    raise Refused("oversize-receipt")
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
            self._after_wire(con, pilot_id, now_s)
            # A reply may be several bubbles. Release this provider slot now,
            # but keep the conversation lease until finish_input() so another
            # sender cannot interleave between bubbles of one reply sequence.
            self._release_concurrent(con, pilot_id, attempt_id, "sent", now_s, keep_conversation=True)

    def _after_wire(self, con, pilot_id: str, now_s: str) -> None:
        """The account's next send may start no earlier than ``interval`` after this POST FINISHED.

        Admission already set ``next_dispatch_at = admission + interval``; the POST itself takes a variable
        time after admission, so the gap between two POSTs could be slightly under the interval. Moving the
        gate forward from the confirmed (or unknown) outcome makes the interval a guarantee, not an
        approximation. Only ever moves it forward."""
        if not self.allowance:
            return
        from . import admission
        p = con.execute("SELECT account_id FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
        row = self._policy_row(con, p["account_id"]) if p else None
        if row is not None and admission.is_concurrent(row):
            return
        if row is None:
            return
        due = _dt(datetime.fromisoformat(now_s) + timedelta(seconds=self._gap(row["throttle_seconds"])))
        if row["next_dispatch_at"] is None or _dt(row["next_dispatch_at"]) < due:
            con.execute("UPDATE of_delivery_policy SET next_dispatch_at=? WHERE account_id=?", (due, p["account_id"]))

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
            self._after_wire(con, pilot_id, now_s)
            self._release_concurrent(con, pilot_id, attempt_id, "unresolved", now_s)

    def record_not_sent(self, *, pilot_id: str, owner_token: str, attempt_id: int, now: datetime | str) -> bool:
        """Settle an admitted attempt that CERTAINLY never reached the wire (refused before the POST, or the
        provider answered with a definite refusal such as HTTP 429: nothing was created).

        The receipt becomes ``not_sent`` and the attempt ``refused``: neither blocks the fan, nothing stops,
        nothing needs an operator. The conversation's budget unit is given back while this owner still holds
        it, and the account's delivery allowance unit too (persistent service only); the throttle
        (``next_dispatch_at``) is left in place so the account still spaces its sends. An attempt that is
        already ``sent`` or ``delivery_unknown`` is NEVER converted: refused. Identical replay is a no-op
        (returns False)."""
        now_s = _dt(now)
        with self._tx() as con:
            self._outcome_owner(con, pilot_id, owner_token, attempt_id)
            r = con.execute("SELECT state FROM native_pilot_receipts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if r["state"] == "not_sent":
                return False
            if r["state"] != "dispatching":
                raise Refused("attempt-not-dispatching")
            p = con.execute("SELECT * FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            con.execute("UPDATE native_pilot_receipts SET state='not_sent',updated_at=? WHERE attempt_id=? AND state='dispatching'",
                        (now_s, attempt_id))
            con.execute("UPDATE twin_bridge_send_attempts SET state='refused',reason='native-pilot-not-sent',updated_at=? "
                        "WHERE id=? AND state='dispatching'", (now_s, attempt_id))
            con.execute("UPDATE twin_bridge_conversations SET send_budget_used=send_budget_used-1,updated_at=? "
                        "WHERE account_id=? AND fan_id=? AND owner_token=? AND revision=? AND send_budget_used>?",
                        (now_s, p["account_id"], p["fan_id"], owner_token, p["revision"], p["baseline_used"]))
            if self.allowance and not self.fan_daily_limit:
                con.execute("UPDATE of_delivery_policy SET used_units=used_units-1 WHERE account_id=? AND used_units>0",
                            (p["account_id"],))
            # A later bubble may fail after an earlier bubble was accepted.
            # Keep the sequence's ownership until it is requeued or finished.
            prior_sent = con.execute(
                "SELECT 1 FROM native_pilot_receipts WHERE input_key=(SELECT input_key FROM native_pilot_receipts "
                "WHERE attempt_id=?) AND state='sent' LIMIT 1", (attempt_id,)).fetchone() is not None
            self._release_concurrent(con, pilot_id, attempt_id, "not_sent", now_s,
                                     keep_conversation=prior_sent)
            return True

    def requeue_input(self, *, pilot_id: str, owner_token: str, input_key: str, now: datetime | str,
                      max_requeues: int | None, not_before: datetime | str | None = None) -> int:
        """Let a definitely unsent input be answered again later. Returns how many times it has now been
        requeued (1, 2, ...), or 0 when it was not requeued.

        Only a message input still ``generating`` with NO receipt in any state but ``not_sent`` qualifies: once any
        send attempt stands for it (sent, dispatching, delivery_unknown) it is never regenerated. A follow-up
        job input is never requeued. A numeric bound remains for model failures; None permits persistent-service
        provider deferral. The account send gate durably retains not_before without changing its allowance."""
        now_s = _dt(now)
        if max_requeues is None and not self.allowance:
            raise Refused("persistent-requeue-required")
        due = _dt(not_before) if not_before is not None else None
        with self._tx() as con:
            p = con.execute("SELECT owner_token,account_id FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            if p is None or p["owner_token"] != owner_token:
                raise Refused("pilot-owner-mismatch")
            i = con.execute("SELECT state,source_hash,created_at FROM native_pilot_inputs WHERE pilot_id=? AND input_key=?",
                            (pilot_id, input_key)).fetchone()
            if i is None or i["state"] != "generating" or i["source_hash"].startswith(FOLLOWUP_MARK):
                return 0
            if self.allowance and _dt(i["created_at"]) < _dt(datetime.fromisoformat(now_s) - REQUEUE_HORIZON):
                return 0          # engine v2: a reply unanswerable for a day is let go (the caller surfaces it)
            if con.execute("SELECT 1 FROM native_pilot_receipts WHERE input_key=? AND state<>'not_sent' LIMIT 1",
                           (input_key,)).fetchone():
                return 0
            used = self._requeues.get(input_key, 0)
            if max_requeues is not None and used >= max_requeues:
                return 0
            if due is not None and self.allowance:
                # A definite provider refusal applies to this account's connection.
                # Persist its cooldown in the existing send gate, including across restarts.
                account = p["account_id"]
                policy = self._policy_row(con, account)
                if policy is None:
                    raise Refused("allowance-missing")
                from . import admission
                concurrent = admission.is_concurrent(policy)
                current = admission.cooldown_at(policy)
                if current is None or _dt(current) < due:
                    if concurrent:
                        try:
                            policy["provider_cooldown_until"]
                        except (KeyError, IndexError):
                            con.execute("UPDATE of_delivery_policy SET next_dispatch_at=? WHERE account_id=?", (due, account))
                        else:
                            con.execute("UPDATE of_delivery_policy SET provider_cooldown_until=? WHERE account_id=?",
                                        (due, account))
                    else:
                        con.execute("UPDATE of_delivery_policy SET next_dispatch_at=? WHERE account_id=?", (due, account))
            con.execute("UPDATE native_pilot_inputs SET state=?,updated_at=? WHERE pilot_id=? AND input_key=? AND state='generating'",
                        (f"{REQUEUED}{used + 1}", now_s, pilot_id, input_key))
            return used + 1

    def release_input(self, *, pilot_id: str, owner_token: str, input_key: str, now: datetime | str) -> bool:
        """Give an UNSENT claim back without spending a retry: the fan's newest message becomes answerable again.

        Used when a reply is cancelled for a reason that is not the message's fault (a human takeover or pause
        landed, the account was held, the send queue timed out, a newer message made the draft stale). The
        draft is never kept: after the hold clears the engine answers the fan's latest state from scratch.
        Refused (False) for a follow-up input or once any send attempt other than a ``not_sent`` one stands."""
        now_s = _dt(now)
        with self._tx() as con:
            p = con.execute("SELECT owner_token FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            if p is None or p["owner_token"] != owner_token:
                raise Refused("pilot-owner-mismatch")
            i = con.execute("SELECT state,source_hash FROM native_pilot_inputs WHERE pilot_id=? AND input_key=?",
                            (pilot_id, input_key)).fetchone()
            if i is None or i["state"] != "generating" or i["source_hash"].startswith(FOLLOWUP_MARK):
                return False
            if con.execute("SELECT 1 FROM native_pilot_receipts WHERE input_key=? AND state<>'not_sent' LIMIT 1",
                           (input_key,)).fetchone():
                return False
            con.execute("UPDATE native_pilot_inputs SET state=?,updated_at=? WHERE pilot_id=? AND input_key=? AND state='generating'",
                        (f"{REQUEUED}0", now_s, pilot_id, input_key))
            return True

    def unresolved_attempts(self, *, account_id: str, now: datetime | str, grace_s: float = 0.0, limit: int = 50) -> list[dict]:
        """Admitted sends of this account whose outcome is not settled and whose owner is gone.

        ``dispatching`` (the process died between admission and the answer) and ``delivery_unknown`` (a timeout
        or unreadable reply). An attempt whose pilot lease is still live is the running process's own and is
        left alone. Metadata only."""
        now_s = _dt(now)
        cutoff = _dt(datetime.fromisoformat(now_s) - timedelta(seconds=grace_s))
        with store.readonly(self.path) as con:
            rows = con.execute(
                "SELECT r.pilot_id,r.attempt_id,r.state AS receipt_state,r.created_at,p.fan_id,p.state AS pilot_state,p.deadline "
                "FROM native_pilot_receipts r JOIN native_pilots p ON p.pilot_id=r.pilot_id "
                "JOIN twin_bridge_send_attempts a ON a.id=r.attempt_id "
                "WHERE p.account_id=? AND r.state IN ('dispatching','delivery_unknown') "
                "AND a.state IN ('dispatching','delivery_unknown') AND r.created_at<=? "
                "AND NOT (p.state='active' AND p.deadline>?) ORDER BY r.attempt_id LIMIT ?",
                (account_id, cutoff, now_s, limit)).fetchall()
        return [dict(r) for r in rows]

    def settle_by_proof(self, *, pilot_id: str, attempt_id: int, account_id: str, fan_id: int,
                        provider_message_id: int, raw: dict, evidence_hash: str, now: datetime | str,
                        resume: bool = True) -> bool:
        """An admitted, unsettled send is PROVEN delivered by exactly one matching message in a completely read
        provider history window (checked by the caller, see autoreconcile). The receipt and attempt become
        ``sent`` with the proof recorded as ``reconciliation`` (actor ``engine-auto``).

        Only ever converts toward ``sent``. With ``resume`` a conversation the unknown outcome had parked in
        ``attention`` goes back to ``active`` (unowned; the next adoption gets a new fenced token), exactly the
        state it had before the send. Returns False when already settled the same way."""
        now_s = _dt(now)
        with self._tx() as con:
            p = con.execute("SELECT * FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            r = con.execute("SELECT * FROM native_pilot_receipts WHERE pilot_id=? AND attempt_id=?", (pilot_id, attempt_id)).fetchone()
            a = con.execute("SELECT * FROM twin_bridge_send_attempts WHERE id=?", (attempt_id,)).fetchone()
            if not p or not r or not a or (p["account_id"], p["fan_id"]) != (account_id, fan_id) \
                    or (a["account_id"], a["fan_id"]) != (account_id, fan_id):
                raise Refused("recovery-scope-mismatch")
            if r["state"] == "sent" and a["state"] == "sent":
                return False
            if r["state"] not in ("dispatching", "delivery_unknown") or a["state"] not in ("dispatching", "delivery_unknown"):
                raise Refused("recovery-attempt-settled")
            if p["state"] == "active" and _dt(p["deadline"]) > now_s:
                raise Refused("recovery-owner-live")
            if con.execute("SELECT 1 FROM native_pilot_receipts x JOIN native_pilots q ON q.pilot_id=x.pilot_id "
                           "WHERE q.account_id=? AND x.provider_message_id=? AND x.attempt_id<>? LIMIT 1",
                           (account_id, str(provider_message_id), attempt_id)).fetchone():
                raise Refused("provider-id-reused")
            retained = json.loads(r["evidence_json"] or "{}")
            retained["reconciliation"] = {"actor": "engine-auto", "evidence_hash": evidence_hash,
                                          "provider_message_id": provider_message_id}
            encoded = _json(retained)
            con.execute("UPDATE native_pilot_receipts SET state='sent',provider_message_id=?,response_json=?,evidence_json=?,updated_at=? "
                        "WHERE attempt_id=?", (str(provider_message_id), _json(raw), encoded, now_s, attempt_id))
            con.execute("UPDATE twin_bridge_send_attempts SET state='sent',provider_message_id=?,evidence_json=?,updated_at=? WHERE id=?",
                        (str(provider_message_id), encoded, now_s, attempt_id))
            if p["state"] != "stopped":
                # A crashed owner's pilot: stop it so adoption re-issues a fenced token and revision.
                con.execute("UPDATE native_pilots SET state='stopped',reason='receipt-reconciled',updated_at=? WHERE pilot_id=?",
                            (now_s, pilot_id))
            self._resume_conversation(con, p, now_s) if resume else None
            return True

    def settle_absent(self, *, pilot_id: str, attempt_id: int, account_id: str, fan_id: int, now: datetime | str,
                      resume: bool = True) -> bool:
        """OPT-IN (FASTT_AUTO_RESOLVE_ABSENT): the caller read the provider history COMPLETELY back past the attempt
        and found no matching message, long after the attempt. The attempt becomes ``refused`` / receipt
        ``not_sent`` (budget and allowance units given back) so the fan can be answered again. Never converts a
        ``sent`` attempt."""
        now_s = _dt(now)
        with self._tx() as con:
            p = con.execute("SELECT * FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            r = con.execute("SELECT * FROM native_pilot_receipts WHERE pilot_id=? AND attempt_id=?", (pilot_id, attempt_id)).fetchone()
            a = con.execute("SELECT * FROM twin_bridge_send_attempts WHERE id=?", (attempt_id,)).fetchone()
            if not p or not r or not a or (p["account_id"], p["fan_id"]) != (account_id, fan_id) \
                    or (a["account_id"], a["fan_id"]) != (account_id, fan_id):
                raise Refused("recovery-scope-mismatch")
            if r["state"] == "not_sent":
                return False
            if r["state"] not in ("dispatching", "delivery_unknown") or a["state"] not in ("dispatching", "delivery_unknown"):
                raise Refused("recovery-attempt-settled")
            if p["state"] == "active" and _dt(p["deadline"]) > now_s:
                raise Refused("recovery-owner-live")
            retained = json.loads(r["evidence_json"] or "{}")
            retained["reconciliation"] = {"actor": "engine-auto", "outcome": "absent"}
            con.execute("UPDATE native_pilot_receipts SET state='not_sent',evidence_json=?,updated_at=? WHERE attempt_id=?",
                        (_json(retained), now_s, attempt_id))
            con.execute("UPDATE twin_bridge_send_attempts SET state='refused',reason='engine-reconciled-absent',updated_at=? WHERE id=?",
                        (now_s, attempt_id))
            con.execute("UPDATE twin_bridge_conversations SET send_budget_used=send_budget_used-1,updated_at=? "
                        "WHERE account_id=? AND fan_id=? AND send_budget_used>?", (now_s, account_id, fan_id, p["baseline_used"]))
            # The fan's message was never answered: open its input again so it is answered from the latest state.
            con.execute("UPDATE native_pilot_inputs SET state=?,updated_at=? WHERE input_key=? AND state IN ('stopped','generating') "
                        "AND NOT EXISTS (SELECT 1 FROM native_pilot_receipts x WHERE x.input_key=native_pilot_inputs.input_key "
                        "AND x.state IN ('sent','dispatching','delivery_unknown'))", (f"{REQUEUED}0", now_s, r["input_key"]))
            if p["state"] != "stopped":
                con.execute("UPDATE native_pilots SET state='stopped',reason='receipt-reconciled',updated_at=? WHERE pilot_id=?",
                            (now_s, pilot_id))
            self._resume_conversation(con, p, now_s) if resume else None
            return True

    def flag_attention(self, *, account_id: str, fan_id: int, now: datetime | str,
                       reason: str = "native-pilot-delivery-unresolved") -> bool:
        """Park an unowned, active conversation in the existing ``attention`` state. Idempotent. Never sends."""
        now_s = _dt(now)
        with self._tx() as con:
            cur = con.execute("UPDATE twin_bridge_conversations SET state='attention',reason=?,owner_token=NULL,"
                              "owner_expires_at=NULL,revision=revision+1,updated_at=? WHERE account_id=? AND fan_id=? "
                              "AND state='active' AND (owner_expires_at IS NULL OR owner_expires_at<=?)",
                              (reason, now_s, account_id, fan_id, now_s))
            return cur.rowcount > 0

    @staticmethod
    def _resume_conversation(con, p, now_s: str) -> None:
        con.execute("UPDATE twin_bridge_conversations SET state='active',reason=NULL,owner_token=NULL,owner_expires_at=NULL,"
                    "reserved=?,revision=revision+1,updated_at=? WHERE account_id=? AND fan_id=? "
                    "AND state IN ('attention','active')", (True, now_s, p["account_id"], p["fan_id"]))

    def finish_input(self, *, pilot_id: str, owner_token: str, input_key: str, now: datetime | str,
                     outcome: str = "complete") -> None:
        if outcome not in ("complete", "failed", "no_reply"):
            raise Refused("invalid-input-outcome")
        with self._tx() as con:
            p = con.execute("SELECT owner_token,account_id,fan_id FROM native_pilots WHERE pilot_id=?",
                            (pilot_id,)).fetchone()
            if p is None or p["owner_token"] != owner_token:
                raise Refused("pilot-owner-mismatch")
            now_s = _dt(now)
            con.execute("UPDATE native_pilot_inputs SET state=?,updated_at=? "
                        "WHERE pilot_id=? AND input_key=? AND state='generating'",
                        (outcome, now_s, pilot_id, input_key))
            self._finish_concurrent_lease(con, p, owner_token, now_s)

    def _finish_concurrent_lease(self, con, p, owner_token: str, now_s: str) -> None:
        """Release a completed reply sequence, never an unresolved one.

        record_sent() deliberately keeps the per-conversation lease while the
        remaining bubbles are produced. This is the one point that gives the
        conversation back after the sequence has settled.
        """
        if not self.allowance:
            return
        from . import admission
        row = self._policy_row(con, p["account_id"])
        if row is None:
            return
        lease = con.execute(
            "SELECT * FROM engine_conversation_lease "
            "WHERE tenant_id=? AND account_id=? AND fan_id=?",
            (self.tenant_id, p["account_id"], p["fan_id"])).fetchone()
        if (lease is None or lease["state"] != "held"
                or lease["holder"] != owner_token):
            return
        # A mode rollback must not strand the lease of a sequence that was
        # admitted in concurrent mode. Release the existing owner even when the
        # current policy row is already back in gap mode.
        gap = max(admission.conversation_gap_seconds(row), self.conversation_min_gap_s)
        next_send = _dt(datetime.fromisoformat(now_s) + timedelta(seconds=gap))
        con.execute(
            "UPDATE engine_conversation_lease SET state='released',holder='',owner_token='',"
            "attempt_id=NULL,expires_at=?,next_send_at=?,updated_at=? "
            "WHERE tenant_id=? AND account_id=? AND fan_id=? AND state='held' AND holder=?",
            (now_s, next_send, now_s, self.tenant_id, p["account_id"], p["fan_id"], owner_token))

    def unhandled_source_ids(self, *, pilot_id: str, source_ids) -> set[int]:
        """Sources not examined by a completed input of this durable pilot.

        Unanswered bursts retain historical context after a human handoff.
        Completion proves the inbound identity guard already examined every
        member. Generating, abandoned and requeued inputs are deliberately not
        evidence of handling.
        """
        pending = {int(mid) for mid in source_ids}
        if not pending:
            return pending
        with store.readonly(self.path) as con:
            # R3: handled means handled for this account/fan, not only for this pilot instance. A resumed
            # conversation starts a new pilot; a question the earlier pilot already examined and handed to a
            # human must not trigger the handoff again.
            rows = con.execute(
                "SELECT i.message_id,i.member_ids_json FROM native_pilot_inputs i "
                "JOIN native_pilots p ON p.pilot_id=i.pilot_id "
                "WHERE i.state='complete' AND (i.pilot_id=? OR (p.account_id=(SELECT account_id FROM native_pilots WHERE pilot_id=?) "
                "AND p.fan_id=(SELECT fan_id FROM native_pilots WHERE pilot_id=?)))",
                (pilot_id, pilot_id, pilot_id)).fetchall()
        for row in rows:
            members = json.loads(row["member_ids_json"] or "[]")
            pending.difference_update(int(mid) for mid in members)
            pending.discard(int(row["message_id"]))
            if not pending:
                break
        return pending

    # ── runner-owned follow-ups (the stock nudge) ────────────────────────────
    # A follow-up has no fan message to answer: its "source" is the newest message of the chat, which
    # must still be OUR OWN last outbound (the anchor), with no fan message newer than the one the job
    # was queued behind. Claiming it uses the same native_pilot_inputs row, so one anchor is claimed at
    # most once per pilot (never replayed) and begin_attempt re-proves the anchor at the wire.

    @staticmethod
    def _newest(con, account: str, fan: int, pilot_id: str | None = None, input_key: str | None = None):
        return con.execute(
            "SELECT m.message_id,m.direction,m.body,m.created_at,m.ingested_at,m.automation_kind FROM messages m "
            "WHERE m.account_id=? AND m.fan_id=? AND NOT (m.direction='out' AND m.is_unsent) "
            "AND NOT EXISTS (SELECT 1 FROM native_pilot_receipts r WHERE r.pilot_id=? AND r.input_key=? "
            "AND r.state='sent' AND r.provider_message_id=CAST(m.message_id AS TEXT)) "
            "ORDER BY m.created_at DESC,m.message_id DESC LIMIT 1", (account, fan, pilot_id, input_key)).fetchone()

    @classmethod
    def _followup_unchanged(cls, con, p, i) -> bool:
        row = cls._newest(con, p["account_id"], p["fan_id"], p["pilot_id"], i["input_key"])
        return row is not None and row["message_id"] == i["message_id"] and row["direction"] == "out"

    def followup_add(self, *, account_id: str, fan_id: int, kind: str, payload: dict, run_at: datetime | str,
                     now: datetime | str) -> str | None:
        """Queue one follow-up; a still-queued one for the same account/fan/kind is superseded. Returns its id."""
        if kind not in FOLLOWUP_KINDS:
            raise Refused("followup-kind")
        if not isinstance(fan_id, int) or isinstance(fan_id, bool) or fan_id <= 0:
            raise Refused("invalid-identity")
        now_s, run_s = _dt(now), _dt(run_at)
        if run_s > _dt(datetime.fromisoformat(now_s) + FOLLOWUP_MAX_AHEAD):
            raise Refused("followup-run-at")
        with self._tx(initialize=True) as con:
            last_in = con.execute("SELECT message_id,created_at FROM messages WHERE account_id=? AND fan_id=? "
                                  "AND direction='in' ORDER BY created_at DESC,message_id DESC LIMIT 1",
                                  (account_id, fan_id)).fetchone()
            if kind in OFFER_KEYED_KINDS:
                # Queue time re-proof: the offer exists and is THIS account's and THIS fan's.
                offer_id = payload.get(OFFER_PAYLOAD_KEY[kind]) if isinstance(payload, dict) else None
                if type(offer_id) is not int or offer_id <= 0 or con.execute(
                        "SELECT 1 FROM content_offers WHERE id=? AND account_id=? AND fan_id=?",
                        (offer_id, account_id, fan_id)).fetchone() is None:
                    raise Refused("followup-offer")
                prior = con.execute("SELECT state,last_in_id FROM native_pilot_followups WHERE account_id=? AND fan_id=? "
                                    "AND kind=? AND payload_json=?", (account_id, fan_id, kind, _json(payload))).fetchall()
                newest_in = last_in["message_id"] if last_in else None
                for row in prior:
                    # post_buy: one opportunity per purchase, ever. discount: a job the eligibility rules dropped may be
                    # queued again, but only once the fan has said something new since.
                    if kind == "post_buy" or row["state"] != "dropped" or row["last_in_id"] == newest_in:
                        return None
            else:
                # One live nudge per fan: a newer offer's nudge replaces an older queued one (its anchor is stale).
                con.execute("UPDATE native_pilot_followups SET state='dropped',reason='superseded',updated_at=? "
                            "WHERE account_id=? AND fan_id=? AND kind=? AND state='queued'", (now_s, account_id, fan_id, kind))
            job_id = _hash([account_id, fan_id, kind, run_s, now_s, payload])[:32]   # the payload too: two offers queued in one instant
            con.execute("INSERT INTO native_pilot_followups(job_id,account_id,fan_id,kind,payload_json,run_at,last_in_id,"
                        "last_in_at,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'queued',?,?)",
                        (job_id, account_id, fan_id, kind, _json(payload), run_s,
                         last_in["message_id"] if last_in else None, _dt(last_in["created_at"]) if last_in else None,
                         now_s, now_s))
            return job_id

    def card_evidence(self, *, account_id: str, fan_id: int, provider_message_id: str) -> dict | None:
        """The vault card this runner sent as ``provider_message_id`` to this fan, from its own sent-receipt evidence:
        ``{"media": [ids], "previews": [ids], "price_cents": n}``, else None (never sent here, or sent without content)."""
        with store.readonly(self.path) as con:
            if not store.has_table(con, "native_pilot_receipts"):
                return None
            row = con.execute("SELECT r.evidence_json FROM native_pilot_receipts r JOIN native_pilots p ON p.pilot_id=r.pilot_id "
                              "WHERE p.account_id=? AND p.fan_id=? AND r.provider_message_id=? AND r.state='sent' "
                              "ORDER BY r.attempt_id DESC LIMIT 1", (account_id, fan_id, str(provider_message_id))).fetchone()
        try:
            content = json.loads(row[0] or "{}").get("content") if row else None
        except ValueError:
            return None
        if not isinstance(content, dict):
            return None
        media, previews, price = content.get("media_ids"), content.get("preview_ids"), content.get("price_cents")
        if (not isinstance(media, list) or not media or any(type(m) is not int for m in media)
                or not isinstance(previews, list) or any(type(m) is not int for m in previews)
                or type(price) is not int or price <= 0):
            return None
        return {"media": list(media), "previews": list(previews), "price_cents": price}

    def followups_retire(self, *, account_id: str, fan_id: int, kinds: tuple, reason: str, now: datetime | str) -> int:
        """Drop this fan's still-queued jobs of ``kinds`` (a purchase makes the unpaid-offer nudge and discount obsolete)."""
        if not kinds or any(k not in FOLLOWUP_KINDS for k in kinds):
            raise Refused("followup-kind")
        with self._tx(initialize=True) as con:
            return con.execute("UPDATE native_pilot_followups SET state='dropped',reason=?,updated_at=? WHERE account_id=? AND fan_id=? "
                               "AND state='queued' AND kind IN (%s)" % ",".join("?" * len(kinds)),
                               (reason, _dt(now), account_id, fan_id, *kinds)).rowcount

    def followups_due(self, *, account_id: str, now: datetime | str, fan_id: int | None = None, limit: int = 20) -> list[dict]:
        with store.readonly(self.path) as con:
            if not store.has_table(con, "native_pilot_followups"):
                return []
            query, args = ("SELECT * FROM native_pilot_followups WHERE account_id=? AND state='queued' AND run_at<=?",
                           [account_id, _dt(now)])
            if fan_id is not None:
                query, args = query + " AND fan_id=?", args + [fan_id]
            return [dict(r) for r in con.execute(query + " ORDER BY run_at,job_id LIMIT ?", (*args, limit))]

    def followup_finish(self, *, job_id: str, state: str, reason: str | None, now: datetime | str) -> bool:
        """Settle a job exactly once: done / dropped / failed. A settled job is never queued again.
        True when this call settled it (a second call is a no-op)."""
        if state not in ("done", "dropped", "failed"):
            raise Refused("followup-state")
        with self._tx(initialize=True) as con:
            return con.execute("UPDATE native_pilot_followups SET state=?,reason=?,updated_at=? WHERE job_id=? "
                               "AND state IN ('queued','claimed')", (state, reason, _dt(now), job_id)).rowcount > 0

    def claim_followup(self, *, pilot_id: str, owner_token: str, job_id: str, now: datetime | str) -> tuple[str, dict]:
        """Claim one due job before generating. Returns ``(input_key, anchor_message_row)``.

        Refuses (job-specific reasons in FOLLOWUP_DROPS) unless the newest message of the chat is still our
        own AI outbound, newer than the pilot cutoff, and the fan has not spoken since the job was queued."""
        now_s = _dt(now)
        with self._tx(initialize=True) as con:
            p = self._active(con, pilot_id, owner_token, now_s)
            job = con.execute("SELECT * FROM native_pilot_followups WHERE job_id=?", (job_id,)).fetchone()
            if job is None or job["state"] != "queued" or (job["account_id"], job["fan_id"]) != (p["account_id"], p["fan_id"]):
                raise Refused("followup-not-queued")
            if job["run_at"] > now_s:
                raise Refused("followup-not-due")
            if now_s > _dt(datetime.fromisoformat(job["run_at"]) + FOLLOWUP_TTL):
                raise Refused("followup-expired")
            anchor = self._newest(con, p["account_id"], p["fan_id"])
            if (anchor is None or anchor["direction"] != "out" or not anchor["automation_kind"]
                    or not isinstance(anchor["message_id"], int) or _dt(anchor["created_at"]) <= p["cutoff"]):
                raise Refused("followup-chat-moved")
            last_in = con.execute("SELECT message_id FROM messages WHERE account_id=? AND fan_id=? AND direction='in' "
                                  "ORDER BY created_at DESC,message_id DESC LIMIT 1", (p["account_id"], p["fan_id"])).fetchone()
            if (last_in["message_id"] if last_in else None) != job["last_in_id"]:
                raise Refused("followup-fan-replied")
            if con.execute("SELECT 1 FROM native_pilot_inputs WHERE pilot_id=? AND (message_id=? OR state='generating')",
                           (pilot_id, anchor["message_id"])).fetchone():
                raise Refused("input-already-claimed")
            key = _hash([pilot_id, FOLLOWUP_MARK, job_id])
            created, ingested = _dt(anchor["created_at"]), _dt(anchor["ingested_at"] or anchor["created_at"])
            con.execute("INSERT INTO native_pilot_inputs(input_key,pilot_id,message_id,source_hash,created_at,ingested_at,"
                        "member_ids_json,earliest_created_at,state,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'generating',?,?)",
                        (key, pilot_id, anchor["message_id"], FOLLOWUP_MARK + _hash([job_id, anchor["message_id"], created]),
                         created, ingested, _json([anchor["message_id"]]), created, now_s, now_s))
            con.execute("UPDATE native_pilot_followups SET state='claimed',input_key=?,updated_at=? WHERE job_id=?",
                        (key, now_s, job_id))
            return key, dict(anchor)

    # ── persistent service pilots ────────────────────────────────────────────
    # Same rows, same fences and the same admission/unknown rules as a bounded
    # pilot; what differs is that the lease is renewed instead of fixed, the
    # cap is the account's delivery allowance, and a restarted process may adopt
    # its own pilot once the old lease has lapsed and nothing is unresolved.

    def _recover_abandoned_inputs(self, con, pilot, now_s: str) -> set[str]:
        """Recover only unchanged pre-wire inputs under the adoption transaction.

        Both retained ledgers must prove no send. A missing/mismatched bridge
        record is not negative evidence. No receipt, budget or model count is erased.
        """
        try:
            self._canonical_guard(con, pilot, lock=True)
        except Refused:
            return set()
        # An orphan from the expired owner has no input proof and cannot be ignored.
        if con.execute("SELECT 1 FROM twin_bridge_send_attempts a WHERE a.account_id=? AND a.fan_id=? "
                       "AND a.owner_token=? AND a.revision=? AND NOT EXISTS "
                       "(SELECT 1 FROM native_pilot_receipts r WHERE r.attempt_id=a.id) LIMIT 1",
                       (pilot["account_id"], pilot["fan_id"], pilot["owner_token"], pilot["revision"])).fetchone():
            return set()
        recovered = set()
        source = self._source(con, pilot, now_s)
        for row in con.execute("SELECT * FROM native_pilot_inputs WHERE pilot_id=? AND state='generating'",
                               (pilot["pilot_id"],)).fetchall():
            if row["source_hash"].startswith(FOLLOWUP_MARK):
                continue
            expected = SourceSnapshot(row["message_id"], row["source_hash"], row["created_at"], row["ingested_at"],
                                      tuple(json.loads(row["member_ids_json"])), row["earliest_created_at"])
            if source != expected:
                continue
            if con.execute("SELECT 1 FROM native_pilot_receipts r LEFT JOIN twin_bridge_send_attempts a "
                           "ON a.id=r.attempt_id WHERE r.input_key=? AND (r.state<>'not_sent' "
                           "OR a.id IS NULL OR a.state IS NULL OR a.state<>'refused' "
                           "OR r.provider_message_id IS NOT NULL OR a.provider_message_id IS NOT NULL "
                           "OR a.account_id<>? OR a.fan_id<>?) LIMIT 1",
                           (row["input_key"], pilot["account_id"], pilot["fan_id"])).fetchone():
                continue
            recovered.add(row["input_key"])
        return recovered

    def activate_service(self, *, pilot_id: str, account_id: str, fan_id: int, owner_token: str,
                         now: datetime | str, ttl_s: float, cutoff: datetime | str | None = None) -> Pilot:
        """Create or adopt the ONE stable service pilot for an account/fan.

        Refuses (never waits, never forces) while another owner's lease is live,
        any send is unsettled (dispatching / unknown / writer marker / open Twin
        turn), or the conversation is not active (an operator pause, or the
        attention state an unknown outcome leaves behind). Adoption keeps the
        original ``cutoff`` and every used count; it only issues a new fenced
        token and revision. An abandoned message input can be re-claimed only
        when current source/controls and both ledgers prove no send; otherwise
        it stays stopped. Follow-up jobs are never regenerated by adoption.

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
                if self.fan_daily_limit:
                    end = max(end, c["send_budget_used"] + 2000000000)
                    con.execute("UPDATE native_pilots SET end_limit=? WHERE pilot_id=?", (end, pilot_id))
                if c["send_budget_used"] >= end:
                    raise Refused("budget-exhausted")
                recovered = self._recover_abandoned_inputs(con, existing, now_s)
                revision = max(c["revision"], existing["revision"]) + 1
                con.execute("UPDATE native_pilots SET owner_token=?,deadline=?,revision=?,state='active',reason=NULL,"
                            "updated_at=? WHERE pilot_id=?", (owner_token, lease_s, revision, now_s, pilot_id))
                con.execute("UPDATE native_pilot_inputs SET state='stopped',updated_at=? WHERE pilot_id=? AND state='generating'",
                            (now_s, pilot_id))
                for input_key in recovered:
                    con.execute("UPDATE native_pilot_inputs SET state=?,updated_at=? WHERE input_key=?",
                                (f"{REQUEUED}1", now_s, input_key))
            else:
                if lease_live:
                    raise Refused("owner-live")
                used, old_limit = c["send_budget_used"], c["send_budget_limit"]
                if used < 0 or old_limit != used:
                    raise Refused("prior-budget-not-exhausted")
                revision, end = c["revision"] + 1, used + (2000000000 if self.fan_daily_limit else SERVICE_BUDGET_CEILING)
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
        sent for it; a ``not_sent`` receipt is not a send); a claim with any other receipt is stopped, never regenerated.
        Unsettled sends are left exactly as they are and still block adoption.
        """
        now_s = _dt(now)
        with self._tx() as con:
            p = con.execute("SELECT * FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            if p is None or p["owner_token"] != owner_token:
                raise Refused("pilot-owner-mismatch")
            con.execute("DELETE FROM native_pilot_inputs WHERE pilot_id=? AND state='generating' AND NOT EXISTS "
                        "(SELECT 1 FROM native_pilot_receipts r WHERE r.input_key=native_pilot_inputs.input_key AND r.state<>'not_sent')", (pilot_id,))
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
            return con.execute("SELECT 1 FROM native_pilot_inputs WHERE pilot_id=? AND message_id=? AND state NOT LIKE 'requeued:%'",
                               (pilot_id, message_id)).fetchone() is not None

    def status(self, pilot_id: str) -> dict:
        """Return only public metadata/counters, never owner tokens or message text."""
        with self._view() as con:
            p = con.execute("SELECT pilot_id,account_id,fan_id,cutoff,deadline,baseline_used,end_limit,revision,state,reason,model_calls "
                            "FROM native_pilots WHERE pilot_id=?", (pilot_id,)).fetchone()
            if p is None:
                raise Refused("pilot-missing")
            result = dict(p)
            result["attempts"] = {r["state"]: r["n"] for r in con.execute(
                "SELECT state,COUNT(*) AS n FROM native_pilot_receipts WHERE pilot_id=? GROUP BY state", (pilot_id,))}
            return result

    @staticmethod
    def counted(attempts: dict) -> int:
        """Admissions that still count against a pilot's send limit (a ``not_sent`` one was given back)."""
        return sum(n for state, n in attempts.items() if state != "not_sent")

    def receipts(self, *, pilot_id: str, input_key: str | None = None) -> list[dict]:
        """Metadata only; confirmed IDs may be used for live-history reconciliation."""
        with self._view() as con:
            query = ("SELECT attempt_id,input_key,bubble_no,state,provider_message_id,created_at,updated_at "
                     "FROM native_pilot_receipts WHERE pilot_id=?")
            args: tuple = (pilot_id,)
            if input_key is not None:
                query += " AND input_key=?"
                args += (input_key,)
            return [dict(r) for r in con.execute(query + " ORDER BY attempt_id", args)]
