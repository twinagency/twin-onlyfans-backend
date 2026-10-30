"""Durable admission times, never a sleeper, sender, draft queue or model owner.

Scheduling uses the existing ledger transaction and canonical control generation.
Transport authority is re-read at the existing dispatch boundary. Retired schedules
are evidence, not permission to replay an input or an uncertain delivery.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import random
import statistics

from twin_bridge import store
from onlyfans_runtime.reply_policy import PACING_KEY, FORMAT_KEY, enabled, saved_object
from .ledger import Refused, _dt, _hash, FOLLOWUP_MARK, MAX_REQUEUES_MODEL

TABLE = "native_reply_schedules"
DDL = """CREATE TABLE IF NOT EXISTS native_reply_schedules (
    schedule_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, fan_id BIGINT NOT NULL,
    generation TEXT NOT NULL, anchor_id BIGINT NOT NULL,
    first_received_at TEXT NOT NULL, latest_received_at TEXT NOT NULL,
    provider_created_at TEXT NOT NULL, delay_seconds REAL NOT NULL,
    target_send_at TEXT NOT NULL, pipeline_allowance_s REAL NOT NULL,
    admissions INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
    input_key TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
INDEX = """CREATE UNIQUE INDEX IF NOT EXISTS native_reply_one_pending
    ON native_reply_schedules(account_id,fan_id,generation)
    WHERE state IN ('waiting','generating','held')"""
MAX_ADMISSIONS = 1 + MAX_REQUEUES_MODEL


def stamp(value):
    return datetime.fromisoformat(_dt(value))


@dataclass(frozen=True)
class Policy:
    pacing: bool = False
    formatting: bool = False
    generation: str = ""
    active_since: str = ""


def read_policy(con, account, fan, now):
    if not store.has_table(con, "account_ai_config"):
        return Policy()
    cfg = con.execute("SELECT * FROM account_ai_config WHERE account_id=?", (account,)).fetchone()
    if not cfg or "ai_chatter_config_json" not in cfg.keys():
        return Policy()  # legacy SQLite fixtures have no native configuration
    try:
        raw = saved_object(cfg["ai_chatter_config_json"])
    except (ValueError, TypeError, RecursionError):
        import logging
        logging.getLogger(__name__).warning("native reply switches disabled: invalid stored object")
        return Policy()
    pacing, formatting = enabled(raw, PACING_KEY), enabled(raw, FORMAT_KEY)
    if not (pacing or formatting):
        return Policy()
    # Enhanced work must have canonical authority. The default-off route does
    # not require these extra tables or create a scheduling row.
    runtime = con.execute("SELECT * FROM of_runtime WHERE account_id=?", (account,)).fetchone()
    state = con.execute("SELECT * FROM of_conversation_state WHERE account_id=? AND fan_id=?", (account, fan)).fetchone()
    conn = con.execute("SELECT * FROM of_provider_connection WHERE account_id=?", (account,)).fetchone()
    if not runtime or not state or not conn:
        raise Refused("reply-policy-authority-missing")
    binding = con.execute("SELECT * FROM of_creator_bindings WHERE id=?", (runtime["binding_id"],)).fetchone()
    if (not runtime["enabled"] or state["mode"] != "automated" or state["takeover_state"] not in (None, "none")
            or conn["status"] != "verified" or conn["last_error"] or not conn["verified_at"]
            or stamp(conn["expires_at"]) <= stamp(now) or conn["binding_id"] != runtime["binding_id"]
            or not binding or binding["account_id"] != account or binding["channel"] != "onlyfans"
            or binding["revoked_at"] is not None):
        raise Refused("reply-policy-authority-changed")
    control_at = con.execute("SELECT MAX(created_at) FROM of_control_audit WHERE account_id=? AND fan_id=?",
                             (account, fan)).fetchone()[0]
    since = max(_dt(runtime["activated_at"]), _dt(conn["updated_at"]),
                _dt(control_at) if control_at else _dt(runtime["activated_at"]))
    generation = _hash([account, fan, runtime["binding_id"], _dt(runtime["activated_at"]),
                        conn["revision"], state["revision"], runtime["configuration_revision"], raw])
    return Policy(pacing, formatting, generation, since)


class Authority:
    def __init__(self, ledger, account, fan, policy, *, clock):
        self.ledger, self.account, self.fan, self.policy, self.clock = ledger, account, fan, policy, clock

    def check(self, con=None):
        if con is None:
            with store.readonly(self.ledger.path) as current:
                return self.check(current)
        if read_policy(con, self.account, self.fan, self.clock()) != self.policy:
            raise Refused("reply-policy-changed")


class Scheduler:
    def __init__(self, ledger, *, clock, rng=None):
        self.ledger, self.clock, self.rng = ledger, clock, rng or random.SystemRandom()

    def policy(self, account, fan):
        with store.readonly(self.ledger.path) as con:
            return read_policy(con, account, fan, self.clock())

    def pending(self, pilot, snapshot, policy):
        """Choose ONCE in the ledger transaction; return without holding a slot."""
        now = _dt(self.clock())
        with self.ledger._tx() as con:
            p = self.ledger._active(con, pilot.pilot_id, pilot.owner_token, now)
            self.ledger._canonical_guard(con, p, lock=True)
            if read_policy(con, pilot.account_id, pilot.fan_id, now) != policy or not policy.pacing:
                raise Refused("reply-policy-changed")
            if self.ledger._source(con, p, now) != snapshot:
                raise Refused("source-changed")
            if not store.has_table(con, TABLE):
                raise Refused("reply-pacing-schema-missing")
            row = con.execute("SELECT * FROM native_reply_schedules WHERE account_id=? AND fan_id=? AND generation=? "
                              "AND state IN ('waiting','generating','held')", (pilot.account_id, pilot.fan_id, policy.generation)).fetchone()
            # Unsent stale drafts retain their original burst even after the old
            # claim is completed. Delivered/no-reply schedules are closed explicitly.
            marks = ",".join("?" for _ in snapshot.member_ids)
            members = con.execute("SELECT message_id,created_at,ingested_at FROM messages WHERE account_id=? AND fan_id=? "
                                  "AND message_id IN (" + marks + ")", (pilot.account_id, pilot.fan_id, *snapshot.member_ids)).fetchall()
            if len(members) != len(snapshot.member_ids):
                raise Refused("source-changed")
            first = min(members, key=lambda r: (_dt(r["ingested_at"]), r["message_id"]))
            latest = max(_dt(r["ingested_at"]) for r in members)
            if not (p["cutoff"] < _dt(first["ingested_at"]) <= latest <= now):
                raise Refused("reply-receipt-clock-invalid")
            if row is not None:
                if row["state"] == "generating":
                    prior = con.execute("SELECT state FROM native_pilot_inputs WHERE input_key=?", (row["input_key"],)).fetchone()
                    receipts = con.execute("SELECT state FROM native_pilot_receipts WHERE input_key=?", (row["input_key"],)).fetchall()
                    if any(r[0] != "not_sent" for r in receipts):
                        raise Refused("reply-schedule-delivery-held")
                    if prior and prior[0] == "generating":
                        raise Refused("input-already-claimed")
                    # Only the existing lease-adoption/recovery path may release
                    # an abandoned claim. This never clears a live claim itself.
                    state = "waiting" if row["admissions"] < MAX_ADMISSIONS else "held"
                    con.execute("UPDATE native_reply_schedules SET state=?,updated_at=? WHERE schedule_id=?",
                                (state, now, row["schedule_id"]))
                    row = con.execute("SELECT * FROM native_reply_schedules WHERE schedule_id=?", (row["schedule_id"],)).fetchone()
                if row["state"] == "held":
                    raise Refused("reply-pacing-regeneration-held")
                if row["latest_received_at"] != latest:
                    con.execute("UPDATE native_reply_schedules SET latest_received_at=?,updated_at=? WHERE schedule_id=?",
                                (latest, now, row["schedule_id"]))
                    row = con.execute("SELECT * FROM native_reply_schedules WHERE schedule_id=?", (row["schedule_id"],)).fetchone()
                return dict(row)
            # A completed no-reply input must not anchor a later unanswered turn.
            covered = set()
            for old in con.execute("SELECT i.member_ids_json FROM native_pilot_inputs i JOIN native_pilots p "
                                   "ON p.pilot_id=i.pilot_id WHERE p.account_id=? AND p.fan_id=? AND i.state='complete'",
                                   (pilot.account_id, pilot.fan_id)).fetchall():
                covered.update(json.loads(old[0]))
            unhandled = [r for r in members if r["message_id"] not in covered]
            if not unhandled:
                raise Refused("input-already-claimed")
            first = min(unhandled, key=lambda r: (_dt(r["ingested_at"]), r["message_id"]))
            active = con.execute("SELECT 1 FROM native_pilot_receipts r JOIN native_pilots p ON p.pilot_id=r.pilot_id "
                "JOIN native_pilot_inputs i ON i.input_key=r.input_key WHERE p.account_id=? AND p.fan_id=? "
                "AND r.state='sent' AND i.source_hash NOT LIKE ? AND r.updated_at>=? AND r.updated_at>=? "
                "AND r.updated_at<=? LIMIT 1", (pilot.account_id, pilot.fan_id, FOLLOWUP_MARK + "%", policy.active_since,
                                              _dt(stamp(now) - timedelta(minutes=5)), now)).fetchone() is not None
            delay = float(self.rng.triangular(25.0, 90.0, 35.0 if active else 60.0))
            if not 25.0 <= delay <= 90.0:
                raise Refused("reply-delay-invalid")
            # Measured baseline admission-to-first-confirmed-receipt duration;
            # zero when no observation exists. Never guess an extra post-model delay.
            observed = con.execute("SELECT i.claimed_at,MIN(r.updated_at) AS sent_at FROM native_pilot_inputs i "
                "JOIN native_pilot_receipts r ON r.input_key=i.input_key JOIN native_pilots p ON p.pilot_id=i.pilot_id "
                "WHERE p.account_id=? AND r.state='sent' AND i.source_hash NOT LIKE ? "
                "GROUP BY i.input_key,i.claimed_at ORDER BY i.claimed_at DESC LIMIT 20",
                (pilot.account_id, FOLLOWUP_MARK + "%")).fetchall()
            durations = [(stamp(r["sent_at"]) - stamp(r["claimed_at"])).total_seconds() for r in observed]
            allowance = min(20.0, statistics.median([d for d in durations if d >= 0])) if any(d >= 0 for d in durations) else 0.0
            sid = _hash([pilot.account_id, pilot.fan_id, policy.generation, first["message_id"]])
            if con.execute("SELECT 1 FROM native_reply_schedules WHERE schedule_id=?", (sid,)).fetchone():
                raise Refused("input-already-claimed")
            target = _dt(stamp(first["ingested_at"]) + timedelta(seconds=delay))
            con.execute("INSERT INTO native_reply_schedules(schedule_id,account_id,fan_id,generation,anchor_id,first_received_at,"
                "latest_received_at,provider_created_at,delay_seconds,target_send_at,pipeline_allowance_s,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'waiting',?,?)", (sid, pilot.account_id, pilot.fan_id, policy.generation,
                first["message_id"], _dt(first["ingested_at"]), latest, _dt(first["created_at"]), delay, target, allowance, now, now))
            return dict(con.execute("SELECT * FROM native_reply_schedules WHERE schedule_id=?", (sid,)).fetchone())

    @staticmethod
    def eligible_at(row, quiet_s):
        quiet = min(stamp(row["latest_received_at"]) + timedelta(seconds=quiet_s),
                    stamp(row["first_received_at"]) + timedelta(seconds=20))
        return max(quiet, stamp(row["target_send_at"]) - timedelta(seconds=row["pipeline_allowance_s"]))

    def claim(self, con, row, key):
        """Invoked by claim_input inside its atomic source/owner transaction."""
        current = con.execute("SELECT * FROM native_reply_schedules WHERE schedule_id=?", (row["schedule_id"],)).fetchone()
        if not current or current["state"] != "waiting":
            raise Refused("reply-schedule-not-waiting")
        if current["admissions"] >= MAX_ADMISSIONS:
            raise Refused("reply-pacing-regeneration-held")
        # This method is a private admission hook; Service has already checked
        # the effective quiet interval and every ordinary native gate.
        con.execute("UPDATE native_reply_schedules SET state='generating',admissions=admissions+1,input_key=?,updated_at=? "
                    "WHERE schedule_id=?", (key, _dt(self.clock()), row["schedule_id"]))

    def settle(self, row, *, retry=False):
        with self.ledger._tx() as con:
            current = con.execute("SELECT * FROM native_reply_schedules WHERE schedule_id=?", (row["schedule_id"],)).fetchone()
            if not current or current["state"] != "generating":
                return
            receipts = con.execute("SELECT state FROM native_pilot_receipts WHERE input_key=?", (current["input_key"],)).fetchall()
            states = {r[0] for r in receipts}
            if states.intersection(("dispatching", "delivery_unknown")):
                state = "unknown"
            elif "sent" in states:
                state = "delivered"
            elif retry:
                state = "waiting" if current["admissions"] < MAX_ADMISSIONS else "held"
            else:
                state = "complete"
            con.execute("UPDATE native_reply_schedules SET state=?,updated_at=? WHERE schedule_id=?",
                        (state, _dt(self.clock()), row["schedule_id"]))

    def evidence(self, row, receipts):
        confirmed = [r for r in receipts if r["state"] == "sent"]
        if not confirmed:
            return None
        first = min(stamp(r["updated_at"]) for r in confirmed)
        latency = (first - stamp(row["first_received_at"])).total_seconds()
        with store.readonly(self.ledger.path) as con:
            claim = con.execute("SELECT claimed_at FROM native_pilot_inputs WHERE input_key=?", (confirmed[0]["input_key"],)).fetchone()
        return {"event":"reply-pacing-receipt", "schedule_id":row["schedule_id"],
                "account_id":row["account_id"], "fan_id":row["fan_id"],
                "target_delay_s":row["delay_seconds"], "verified_receipt_latency_s":latency,
                "provider_created_latency_s":(first - stamp(row["provider_created_at"])).total_seconds(),
                "target_error_s":(first - stamp(row["target_send_at"])).total_seconds(),
                "pipeline_allowance_s":row["pipeline_allowance_s"],
                "admitted_at":claim[0] if claim else None,
                "admission_to_receipt_s":(first - stamp(claim[0])).total_seconds() if claim else None,
                "receipt_definition":"provider response validated and recorded locally; not a fan read receipt",
                "receipt_at":_dt(first), "provider_message_id":confirmed[0]["provider_message_id"],
                "outside_window":latency < 25 or latency > 90}
