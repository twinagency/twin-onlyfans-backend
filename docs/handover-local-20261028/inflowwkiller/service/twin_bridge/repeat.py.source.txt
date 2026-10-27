"""Explicit, once-only allowance for a bounded repeat of the Twin text pilot.

No imports from the application and no network calls. The caller keeps the
original scope, uses the returned cumulative cap, and independently enforces the
deadline and fresh provider-time boundary at the final dispatch gate. This is
an operational pilot helper, not a production scheduler or delivery retry API.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


class Refused(RuntimeError):
    pass


def _stamp(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(dt, datetime):
            raise ValueError()
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.isoformat(sep=" ", timespec="microseconds")
    except (ValueError, TypeError, OverflowError) as exc:
        raise Refused("invalid-time") from exc


@contextmanager
def _transaction(path):
    path = Path(path)
    if not path.is_file():
        raise Refused("database-missing")
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def _exists(con, table):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _public(row):
    return {key: row[key] for key in ("run_id", "account_id", "fan_id", "baseline_used", "end_limit",
                                     "revision", "cutoff", "deadline", "state", "reason")}


def arm(path, *, run_id, account_id, fan_id, expected_used, expected_revision,
        extra_replies, scope_fingerprint, owner_token, now, deadline):
    """Atomically grant 1..30 additional attempts; every replay refuses."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", run_id):
        raise Refused("invalid-run-id")
    if not isinstance(account_id, str) or not re.fullmatch(r"[0-9]{1,30}", account_id) \
            or type(fan_id) is not int or not 0 < fan_id < 2**63:
        raise Refused("invalid-identity")
    if type(extra_replies) is not int or not 1 <= extra_replies <= 30 \
            or type(expected_used) is not int or not 0 <= expected_used < 2**63 - 30 \
            or type(expected_revision) is not int or not 0 < expected_revision < 2**63 - 1:
        raise Refused("invalid-limit-or-revision")
    if not isinstance(owner_token, str) or not 16 <= len(owner_token) <= 256:
        raise Refused("invalid-owner")
    if not isinstance(scope_fingerprint, str) or not re.fullmatch(r"[a-f0-9]{32}", scope_fingerprint):
        raise Refused("invalid-scope")
    now_s, deadline_s = _stamp(now), _stamp(deadline)
    duration = (datetime.fromisoformat(deadline_s) - datetime.fromisoformat(now_s)).total_seconds()
    if not 0 < duration <= 3600:
        raise Refused("invalid-window")
    expiry = _stamp(datetime.fromisoformat(now_s) + timedelta(seconds=min(duration, 120)))
    with _transaction(path) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS twin_bridge_repeat_grants (
            run_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, fan_id INTEGER NOT NULL,
            owner_token TEXT NOT NULL, scope_fingerprint TEXT NOT NULL,
            baseline_used INTEGER NOT NULL, end_limit INTEGER NOT NULL, revision INTEGER NOT NULL,
            cutoff TEXT NOT NULL, deadline TEXT NOT NULL, state TEXT NOT NULL, reason TEXT,
            updated_at TEXT NOT NULL)""")
        if con.execute("SELECT 1 FROM twin_bridge_repeat_grants WHERE run_id=?", (run_id,)).fetchone():
            raise Refused("run-already-recorded")
        if con.execute("SELECT 1 FROM twin_bridge_repeat_grants WHERE account_id=? AND state='active'", (account_id,)).fetchone():
            raise Refused("repeat-already-active")
        if _exists(con, "native_pilots") and con.execute(
                "SELECT 1 FROM native_pilots WHERE account_id=? AND state='active'", (account_id,)).fetchone():
            raise Refused("native-pilot-active")
        if con.execute("SELECT 1 FROM twin_bridge_turns WHERE account_id=? "
                       "AND state NOT IN ('delivered','cancelled','parked')", (account_id,)).fetchone():
            raise Refused("twin-work-open")
        if con.execute("SELECT 1 FROM twin_bridge_send_attempts WHERE account_id=? "
                       "AND state NOT IN ('sent','refused')", (account_id,)).fetchone():
            raise Refused("delivery-unresolved")
        if _exists(con, "twin_bridge_writes") and con.execute(
                "SELECT 1 FROM twin_bridge_writes WHERE account_id=?", (account_id,)).fetchone():
            raise Refused("writer-unresolved")
        if _exists(con, "automation_rules") and con.execute(
                "SELECT 1 FROM automation_rules WHERE account_id=? AND is_enabled!=0", (account_id,)).fetchone():
            raise Refused("automation-enabled")
        if _exists(con, "scheduled_jobs") and con.execute(
                "SELECT 1 FROM scheduled_jobs WHERE account_id=? AND status IN ('pending','running')", (account_id,)).fetchone():
            raise Refused("scheduled-work-open")
        row = con.execute("SELECT * FROM twin_bridge_conversations WHERE account_id=? AND fan_id=?", (account_id, fan_id)).fetchone()
        if row is None or row["state"] != "paused" or row["reserved"] != 1 or row["owner_token"] is not None:
            raise Refused("conversation-not-idle-paused")
        if row["revision"] != expected_revision or row["send_budget_used"] != expected_used \
                or row["send_budget_limit"] != expected_used or row["scope_fingerprint"] != scope_fingerprint:
            raise Refused("prior-state-mismatch")
        if any(row[key] and _stamp(row[key]) >= now_s for key in ("reset_at", "cancelled_at", "takeover_at")):
            raise Refused("cutoff-not-new")
        end, revision = expected_used + extra_replies, expected_revision + 1
        con.execute("UPDATE twin_bridge_conversations SET state='active',reason=NULL,revision=?,reset_at=?,"
                    "owner_token=?,owner_expires_at=?,reserved=1,send_budget_limit=?,updated_at=? "
                    "WHERE account_id=? AND fan_id=?", (revision, now_s, owner_token, expiry, end, now_s, account_id, fan_id))
        con.execute("INSERT INTO twin_bridge_repeat_grants VALUES(?,?,?,?,?,?,?,?,?,?,'active',NULL,?)",
                    (run_id, account_id, fan_id, owner_token, scope_fingerprint, expected_used, end, revision, now_s, deadline_s, now_s))
        return _public(con.execute("SELECT * FROM twin_bridge_repeat_grants WHERE run_id=?", (run_id,)).fetchone())


def _grant(con, run_id, owner_token):
    row = con.execute("SELECT * FROM twin_bridge_repeat_grants WHERE run_id=?", (run_id,)).fetchone()
    if row is None or row["owner_token"] != owner_token:
        raise Refused("run-owner-mismatch")
    return row


def _same_run(conversation, grant, owner_token):
    return conversation is not None and conversation["state"] == "active" \
        and conversation["owner_token"] in (None, owner_token) \
        and conversation["revision"] >= grant["revision"] \
        and conversation["scope_fingerprint"] == grant["scope_fingerprint"] \
        and conversation["reset_at"] == grant["cutoff"] \
        and conversation["send_budget_limit"] == grant["end_limit"] \
        and all(not conversation[key] or _stamp(conversation[key]) < grant["cutoff"]
                for key in ("takeover_at", "cancelled_at"))


def stop(path, *, run_id, owner_token, now, reason):
    """Revoke this run's unused allowance without overwriting newer control."""
    if not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", reason):
        raise Refused("invalid-reason")
    now_s = _stamp(now)
    with _transaction(path) as con:
        grant = _grant(con, run_id, owner_token)
        if grant["state"] != "active":
            return {**_public(grant), "revoked": False}
        row = con.execute("SELECT * FROM twin_bridge_conversations WHERE account_id=? AND fan_id=?",
                          (grant["account_id"], grant["fan_id"])).fetchone()
        revoked = _same_run(row, grant, owner_token)
        if revoked:
            uncertain = con.execute("SELECT 1 FROM twin_bridge_send_attempts WHERE account_id=? AND fan_id=? "
                                    "AND state NOT IN ('sent','refused')", (grant["account_id"], grant["fan_id"])).fetchone()
            con.execute("UPDATE twin_bridge_conversations SET state=?,reason=?,reserved=1,revision=revision+1,"
                        "send_budget_limit=send_budget_used,owner_token=NULL,owner_expires_at=NULL,updated_at=? "
                        "WHERE account_id=? AND fan_id=?", ("attention" if uncertain else "paused",
                        "twin-repeat-" + reason, now_s, grant["account_id"], grant["fan_id"]))
        con.execute("UPDATE twin_bridge_repeat_grants SET state='stopped',reason=?,updated_at=? WHERE run_id=?", (reason, now_s, run_id))
        return {**_public(con.execute("SELECT * FROM twin_bridge_repeat_grants WHERE run_id=?", (run_id,)).fetchone()), "revoked": revoked}


def discard_old_inputs(path, *, run_id, owner_token, now):
    """Park old pending receipts only; final dispatch still needs a fresh-time gate."""
    now_s = _stamp(now)
    with _transaction(path) as con:
        grant = _grant(con, run_id, owner_token)
        row = con.execute("SELECT * FROM twin_bridge_conversations WHERE account_id=? AND fan_id=?",
                          (grant["account_id"], grant["fan_id"])).fetchone()
        if grant["state"] != "active" or now_s >= grant["deadline"] or not _same_run(row, grant, owner_token):
            raise Refused("run-not-active")
        return con.execute("UPDATE twin_bridge_inputs SET state='parked' WHERE account_id=? AND fan_id=? "
                           "AND state='pending' AND (occurred_at<=? OR received_at<=?)",
                           (grant["account_id"], grant["fan_id"], grant["cutoff"], grant["cutoff"])).rowcount
