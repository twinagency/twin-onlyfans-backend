"""Durable admission timing, never a sender or a sleeping engine context."""
from __future__ import annotations

import json
import logging
import random
import statistics
from datetime import datetime, timedelta

from twin_bridge import store
from onlyfans_runtime.reply_settings import SWITCHES, enabled
from .ledger import Refused, _dt, _hash

PACING = "native_reply_pacing_enabled"
MAX_ADMISSIONS = 3


def _settings(con, account):
    row = con.execute("SELECT ai_chatter_config_json FROM account_ai_config WHERE account_id=?", (account,)).fetchone()
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate-configuration-key")
            result[key] = value
        return result
    value = json.loads(row[0], object_pairs_hook=unique) if row and row[0] else {}
    if type(value) is not dict:
        raise ValueError("configuration-object-required")
    return {key: enabled(value, key) for key in SWITCHES.values()}


def settings(path, account):
    try:
        with store.readonly(path) as con:
            return _settings(con, account)
    except (ValueError, TypeError, RecursionError):
        logging.getLogger(__name__).warning("invalid-native-reply-configuration")
        return {key: False for key in SWITCHES.values()}
    except store.DB_ERRORS:
        # Older fixtures/clients may have no native configuration column.
        return {key: False for key in SWITCHES.values()}


def authority(con, account, fan):
    """Canonical control generation, independent of workers/pilot leases."""
    flags = _settings(con, account)
    r = con.execute("SELECT enabled,binding_id,activated_at,configuration_revision FROM of_runtime "
                    "WHERE account_id=?", (account,)).fetchone()
    c = con.execute("SELECT binding_id,revision,status,expires_at,automation_starts_at FROM of_provider_connection "
                    "WHERE account_id=?", (account,)).fetchone()
    s = con.execute("SELECT mode,takeover_state,revision FROM of_conversation_state "
                    "WHERE account_id=? AND fan_id=?", (account, fan)).fetchone()
    if (not r or not r['enabled'] or not c or c['status'] != 'verified'
            or c['binding_id'] != r['binding_id'] or not s or s['mode'] != 'automated'
            or s['takeover_state'] not in (None, 'none')):
        raise Refused("reply-controls-held")
    # Include the last change to this flag, including disable/re-enable between ticks.
    epoch = 0
    if store.has_table(con, "of_configuration_change"):
        rows = con.execute("SELECT revision,after_json FROM of_configuration_change WHERE account_id=? "
                           "AND after_json LIKE ? ORDER BY revision DESC LIMIT 1", (account, '%"'+PACING+'"%')).fetchall()
        if rows:
            epoch = rows[0]['revision']
    generation = _hash([account, fan, r['binding_id'], _dt(r['activated_at']), c['revision'], s['revision'], epoch])
    since = _dt(c['automation_starts_at'] or r['activated_at'])
    if store.has_table(con, "of_control_audit"):
        row = con.execute("SELECT max(created_at) FROM of_control_audit WHERE account_id=? AND fan_id=?",
                          (account, fan)).fetchone()
        if row and row[0]:
            since = max(since, _dt(row[0]))
    return {"account": account, "fan": fan, "generation": generation,
            "revision": r['configuration_revision'], "flags": flags, "since": since,
            "expires": _dt(c['expires_at'])}


def guard(con, expected, now):
    current = authority(con, expected['account'], expected['fan'])
    if any(current[k] != expected[k] for k in ('generation', 'revision', 'flags')):
        raise Refused("reply-configuration-changed")
    if _dt(now) >= current['expires']:
        raise Refused("reply-connection-expired")
    key = expected.get('schedule_key')
    if key:
        row = con.execute("SELECT state,generation FROM native_reply_pacing WHERE schedule_key=?", (key,)).fetchone()
        if not row or row['state'] != 'waiting' or row['generation'] != current['generation']:
            raise Refused("reply-schedule-invalidated")


def invalidate_settings(session, account):
    """Inside the editor's existing transaction; receipts/claims are never erased."""
    from sqlalchemy import text
    if session.bind.dialect.name == 'postgresql':
        name = 'fastt_runtime.native_reply_pacing'
        exists = session.execute(text("SELECT to_regclass(:name)"), {"name": name}).scalar()
    else:
        name = 'native_reply_pacing'
        exists = session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
                                 {"name": name}).scalar()
    if exists:
        session.execute(text(f"UPDATE {name} SET state='invalidated' WHERE account_id=:a AND state='waiting'"),
                        {"a": account})


def _measurements(con, account, fan, since, now):
    rows = con.execute("SELECT p.fan_id,i.claimed_at,r.created_at,r.updated_at,i.source_hash "
        "FROM native_pilot_receipts r JOIN native_pilots p ON p.pilot_id=r.pilot_id "
        "JOIN native_pilot_inputs i ON i.input_key=r.input_key WHERE p.account_id=? AND r.state='sent' "
        "AND NOT EXISTS (SELECT 1 FROM native_pilot_receipts earlier WHERE earlier.input_key=r.input_key AND earlier.state='sent' AND earlier.bubble_no<r.bubble_no) AND i.source_hash NOT LIKE 'followup:%' ORDER BY r.updated_at DESC LIMIT 64",
        (account,)).fetchall()
    durations, active = [], False
    lower = max(since, _dt(now - timedelta(minutes=5)))
    for row in rows:
        end, start = datetime.fromisoformat(_dt(row['updated_at'])), datetime.fromisoformat(_dt(row['claimed_at']))
        if start <= end <= now and 0 <= (end-start).total_seconds() <= 180:
            durations.append((end-start).total_seconds())
        if row['fan_id'] == fan and _dt(start) >= since and lower <= _dt(end) <= _dt(now):
            active = True
    # Observed claim-to-first-receipt time includes generation, throttle and preflight.
    # No observations means zero allowance, never an invented performance estimate.
    return min(20.0, statistics.median(durations)) if durations else 0.0, active


def prepare(ledger, pilot, snapshot, *, now, quiet_s=7.0, rng=None):
    """Choose once, then return a due time without claiming inputs or holding locks."""
    now = datetime.fromisoformat(_dt(now))
    rng = rng or random
    with ledger._tx() as con:
        p = ledger._active(con, pilot.pilot_id, pilot.owner_token, _dt(now))
        expected = authority(con, p['account_id'], p['fan_id'])
        if not any(expected['flags'].values()):
            return None
        guard(con, expected, now)
        if not expected['flags'][PACING]:
            return {"authority": expected, "eligible_at": now, "schedule": None}
        if not store.has_table(con, 'native_reply_pacing'):
            raise Refused("reply-pacing-migration-required")
        # Same ownership/source fences as claim_input. This transaction creates no claim.
        if ledger._source(con, p, _dt(now)) != snapshot:
            raise Refused("source-changed")
        con.execute("UPDATE native_reply_pacing SET state='invalidated',updated_at=? "
                    "WHERE account_id=? AND fan_id=? AND generation<>? AND state='waiting'",
                    (_dt(now), p['account_id'], p['fan_id'], expected['generation']))
        row = con.execute("SELECT * FROM native_reply_pacing WHERE account_id=? AND fan_id=? "
                          "AND generation=? AND state='waiting'",
                          (p['account_id'], p['fan_id'], expected['generation'])).fetchone()
        if row is not None and row['input_key']:
            receipt = con.execute("SELECT 1 FROM native_pilot_receipts WHERE input_key=? AND state='sent' LIMIT 1",
                                  (row['input_key'],)).fetchone()
            if receipt:
                con.execute("UPDATE native_reply_pacing SET state='done',updated_at=? WHERE schedule_key=?",
                            (_dt(now),row['schedule_key']))
                row = None
        allowance, active = _measurements(con, p['account_id'], p['fan_id'], expected['since'], now)
        ids = snapshot.member_ids or (snapshot.message_id,)
        marks = ','.join('?' for _ in ids)
        sources = con.execute(f"SELECT message_id,created_at,ingested_at FROM messages WHERE account_id=? "
                              f"AND fan_id=? AND message_id IN ({marks})", (p['account_id'],p['fan_id'],*ids)).fetchall()
        if len(sources) != len(ids):
            raise Refused("source-changed")
        for source in sources:
            if not p['cutoff'] < _dt(source['ingested_at']) <= _dt(now) or _dt(source['created_at']) > _dt(now):
                raise Refused("reply-timestamp-invalid")
        if row is None:
            # Prior completed native claims can still be visible in a no-reply history.
            covered = set()
            for old in con.execute("SELECT member_ids_json FROM native_pilot_inputs WHERE pilot_id=? "
                                   "AND (state='complete' OR EXISTS (SELECT 1 FROM native_pilot_receipts r "
                                   "WHERE r.input_key=native_pilot_inputs.input_key AND r.state='sent'))", (pilot.pilot_id,)).fetchall():
                covered.update(json.loads(old[0]))
            unhandled = [s for s in sources if s['message_id'] not in covered]
            if not unhandled:
                raise Refused("input-already-claimed")
            first = min(unhandled, key=lambda s: (_dt(s['ingested_at']),s['message_id']))
            received = _dt(first['ingested_at'])
            delay = float(rng.triangular(25.0,90.0,35.0 if active else 60.0))
            if not 25 <= delay <= 90:
                raise Refused("reply-delay-invalid")
            key = _hash([p['account_id'],p['fan_id'],expected['generation'],first['message_id']])
            target = _dt(datetime.fromisoformat(received)+timedelta(seconds=delay))
            con.execute("INSERT INTO native_reply_pacing(schedule_key,account_id,fan_id,generation,anchor_id,"
                "first_received_at,provider_created_at,target_at,delay_seconds,active_exchange,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'waiting',?,?)",
                (key,p['account_id'],p['fan_id'],expected['generation'],first['message_id'],received,
                 _dt(first['created_at']),target,delay,int(active),_dt(now),_dt(now)))
            row = con.execute("SELECT * FROM native_reply_pacing WHERE schedule_key=?", (key,)).fetchone()
        row = dict(row)
        first = datetime.fromisoformat(row['first_received_at'])
        latest = max(datetime.fromisoformat(_dt(s['ingested_at'])) for s in sources)
        quiet = min(latest+timedelta(seconds=quiet_s), first+timedelta(seconds=20))
        target = datetime.fromisoformat(row['target_at'])
        eligible = max(quiet, target-timedelta(seconds=allowance))
        expected['schedule_key'] = row['schedule_key']
        return {"authority":expected,"eligible_at":eligible,"schedule":row,"pipeline_allowance_s":allowance}


def claimed(con, expected, input_key, now):
    guard(con, expected, now)
    key = expected.get('schedule_key')
    if not key:
        return
    row = con.execute("SELECT admissions FROM native_reply_pacing WHERE schedule_key=?", (key,)).fetchone()
    if row['admissions'] >= MAX_ADMISSIONS:
        raise Refused("reply-regeneration-budget-held")
    con.execute("UPDATE native_reply_pacing SET admissions=admissions+1,input_key=?,updated_at=? WHERE schedule_key=?",
                (input_key,_dt(now),key))


def finish(ledger, decision, *, now, error=None, sent=False):
    if not decision or not decision['schedule']:
        return
    # Stale unsent work retains its original target for bounded re-admission.
    retryable = error and (error in ('source-changed','new-message-during-generation','reply-configuration-changed')
                           or error.startswith(('model-', 'provider-', 'allowance-')))
    if retryable and not sent:
        return
    with ledger._tx() as con:
        con.execute("UPDATE native_reply_pacing SET state='done',updated_at=? WHERE schedule_key=? AND state='waiting'",
                    (_dt(now),decision['schedule']['schedule_key']))


def delivery_event(expected, schedule, *, receipt_at, wire_at, provider_created_at=None,
                   admitted_at=None, draft_ready_at=None, throttle_done_at=None, eligible_at=None):
    """Actual validated receipt arrival, never a planned time reported as delivery."""
    receipt_at = datetime.fromisoformat(_dt(receipt_at))
    first = datetime.fromisoformat(schedule['first_received_at'])
    origin = datetime.fromisoformat(schedule['provider_created_at'])
    target = datetime.fromisoformat(schedule['target_at'])
    miss = (receipt_at-target).total_seconds()
    components = {}
    reasons = []
    if admitted_at is not None and draft_ready_at is not None and throttle_done_at is not None:
        created = datetime.fromisoformat(schedule['created_at'])
        components = {
            "intentional_wait_s": max(0.0, (min(admitted_at, eligible_at)-created).total_seconds()),
            "before_scheduling_s": max(0.0, (created-first).total_seconds()),
            "scheduler_queue_s": max(0.0, (admitted_at-max(created,eligible_at)).total_seconds()),
            "generation_and_shaping_s": max(0.0, (draft_ready_at-admitted_at).total_seconds()),
            "account_throttle_s": max(0.0, (throttle_done_at-draft_ready_at).total_seconds()),
            "preflight_s": max(0.0, (wire_at-throttle_done_at).total_seconds()),
            "transport_to_receipt_s": max(0.0, (receipt_at-wire_at).total_seconds()),
        }
        if miss > 0:
            reasons = [name for name in ("scheduler_queue_s", "account_throttle_s", "generation_and_shaping_s",
                                         "preflight_s", "transport_to_receipt_s") if components[name] > 0]
    return {"event":"reply-pacing-receipt","schedule_key":schedule['schedule_key'],
            "components":components,"observed_late_components":reasons,"provider_clock_ahead_of_ingest":origin>first,
            "account_id":expected['account'],"fan_id":expected['fan'],
            "target_at":schedule['target_at'],"wire_at":_dt(wire_at),"receipt_at":_dt(receipt_at),
            "provider_created_at":provider_created_at,
            "verified_receipt_to_receipt_s":(receipt_at-first).total_seconds(),
            "provider_created_to_receipt_s":(receipt_at-origin).total_seconds(),
            "intake_lag_s":(first-origin).total_seconds(),"target_miss_s":miss,
            "timing":"late" if miss>0 else "early" if miss<0 else "on-target"}
