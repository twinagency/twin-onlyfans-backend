"""Post-sale follow-ups of the native runner (discounted resend + post-purchase upsell): synthetic SQLite, no network.

Covers the whole chain with real assertions:
* ``postsale.settings``: both switches default off, only a literal JSON ``true`` turns one on;
* ``followups.job_kind`` / ``FollowupQueue`` / ``NativeEngine.enqueue_job``: exactly three job shapes are accepted;
* ``Ledger.followup_add`` / ``followups_retire``: offer ownership re-proof, post_buy dedupe forever, discount re-queue
  only after a NEW inbound since a dropped job;
* ``postsale.purchases`` / ``unpaid_cards`` and ``Service.scan_postsale`` (objection -> beat -> queue, decline and
  distress block, price floor skipped);
* ``Service.postsale_block`` (the run-time re-check);
* ``Gateway.arm_unsend`` / ``_remove_card`` and the ``_send`` outcome mapping of the armed DELETE.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

CHECKS = 0
KNOWN_BUGS = []     # real defects in non-test code that a probe found; reported at the end, never fixed here


def check(value, label=""):
    global CHECKS
    assert value, label
    CHECKS += 1


def refuses(reason, fn):
    from native_pilot.ledger import Refused
    try:
        fn()
    except Refused as exc:
        check(exc.reason == reason, (reason, exc.reason))
    else:
        raise AssertionError("expected refusal: " + reason)


def ts(value):
    return value.isoformat(sep=" ")


LANE = '["rung:vault-ask"]'
HOOK = '["hook_upsell"]'
CATALOGUE = '["catalogue","bundle"]'


def build(root, name):
    """A ledger fixture plus the application tables the post-sale code reads."""
    import test_native_pilot_ledger as L
    f = L.Fixture(root, name)
    f.sql("ALTER TABLE messages ADD COLUMN is_paid INTEGER")
    f.sql("ALTER TABLE messages ADD COLUMN automation_kind TEXT")
    f.sql("CREATE TABLE content_offers(id INTEGER PRIMARY KEY,account_id TEXT,fan_id INTEGER,item_id INTEGER,mode TEXT,"
          "price_cents INTEGER,offer_message_id INTEGER,status TEXT,resolved_by TEXT,resolved_at TEXT,offered_at TEXT)")
    f.sql("CREATE TABLE catalog_items(id INTEGER,account_id TEXT,tags TEXT)")
    f.sql("CREATE TABLE account_ai_config(account_id TEXT,ai_chatter_config_json TEXT)")
    for item, tags in ((1, LANE), (2, HOOK), (3, CATALOGUE)):
        f.sql("INSERT INTO catalog_items VALUES(?,?,?)", (item, L.ACCT, tags))
    return L, f


def config(f, L, value):
    f.sql("DELETE FROM account_ai_config")
    if value is not None:
        f.sql("INSERT INTO account_ai_config VALUES(?,?)", (L.ACCT, value if isinstance(value, str) else json.dumps(value)))


def offer(f, L, oid, *, fan=None, item=1, mode="ppv", price=1250, msg="auto", status="open", by=None, resolved=None,
          offered=None, account=None):
    f.sql("INSERT INTO content_offers VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (oid, account or L.ACCT, fan or L.FAN, item, mode, price, 5000 + oid if msg == "auto" else msg, status, by,
           None if resolved is None else (resolved if isinstance(resolved, str) else ts(resolved)),
           None if offered is None else (offered if isinstance(offered, str) else ts(offered))))


def inbound(f, L, mid, body, at, fan=None):
    f.sql("INSERT INTO messages(account_id,fan_id,message_id,direction,body,media_ids,media_count,price_cents,is_tip,is_unsent,"
          "created_at,ingested_at,raw_json) VALUES(?,?,?,'in',?,'[]',0,0,0,0,?,?,'{}')",
          (L.ACCT, fan or L.FAN, mid, body, ts(at), ts(at)))


# ── 1. settings ──────────────────────────────────────────────────────────────

def test_settings(root):
    from native_pilot import postsale
    L, f = build(root, "settings")
    off = {"discount": False, "post_buy": False}
    check(postsale.settings(f.path, L.ACCT) == off, "no config row: both off")
    config(f, L, {})
    check(postsale.settings(f.path, L.ACCT) == off, "empty config: both off")
    config(f, L, {postsale.SETTING_DISCOUNT: True})
    check(postsale.settings(f.path, L.ACCT) == {"discount": True, "post_buy": False}, "discount alone")
    config(f, L, {postsale.SETTING_POST_BUY: True})
    check(postsale.settings(f.path, L.ACCT) == {"discount": False, "post_buy": True}, "post_buy alone")
    config(f, L, {postsale.SETTING_DISCOUNT: True, postsale.SETTING_POST_BUY: True})
    check(postsale.settings(f.path, L.ACCT) == {"discount": True, "post_buy": True}, "both literal true")
    for bad in ("true", "True", 1, "yes", "on", 1.0, [True], {"x": True}, None, False, 0, ""):
        config(f, L, {postsale.SETTING_DISCOUNT: bad, postsale.SETTING_POST_BUY: bad})
        check(postsale.settings(f.path, L.ACCT) == off, ("non-true ignored", bad))
    for raw in ("not json", "[true]", '"true"', "null", "5", "{"):
        config(f, L, raw)
        check(postsale.settings(f.path, L.ACCT) == off, ("unreadable config is off", raw))
    config(f, L, {postsale.SETTING_DISCOUNT: True, postsale.SETTING_POST_BUY: True})
    check(postsale.settings(f.path, "someone-else") == off, "another account's config does not apply")
    check(postsale.settings(Path(root) / "missing.db", L.ACCT) == off, "unreadable database is off")
    f2 = L.Fixture(root, "settings-notable")      # the config table does not exist at all
    check(postsale.settings(f2.path, L.ACCT) == off, "no config table is off")


# ── 2. job kinds ─────────────────────────────────────────────────────────────

def test_job_kind(root):
    from native_pilot.followups import job_kind, offer_of, FollowupQueue
    from native_pilot.engine import NativeEngine
    FAN = 318022380
    check(job_kind("ai_chatter", {"nudge_fan_ids": [FAN]}, FAN) == "nudge")
    check(job_kind("ai_chatter", {"discount_offer": 7}, FAN) == "discount")
    check(job_kind("ai_chatter", {"post_buy": 7}, FAN) == "post_buy")
    check(offer_of("discount", {"discount_offer": 7}) == 7 and offer_of("post_buy", {"post_buy": 7}) == 7)
    check(offer_of("nudge", {"nudge_fan_ids": [FAN]}) is None and offer_of("post_buy", {"discount_offer": 7}) is None)
    forged = [
        ("ai_chatter", {"discount_offer": True}), ("ai_chatter", {"post_buy": True}),
        ("ai_chatter", {"discount_offer": "7"}), ("ai_chatter", {"post_buy": "7"}),
        ("ai_chatter", {"discount_offer": 7.0}), ("ai_chatter", {"post_buy": None}),
        ("ai_chatter", {"discount_offer": 0}), ("ai_chatter", {"post_buy": -3}),
        ("ai_chatter", {"discount_offer": [7]}),
        ("ai_chatter", {"discount_offer": 7, "post_buy": 7}), ("ai_chatter", {"post_buy": 7, "only_fan_ids": [FAN]}),
        ("ai_chatter", {"discount_offer": 7, "force": True}), ("ai_chatter", {}),
        ("ai_chatter", {"nudge_fan_ids": [FAN], "discount_offer": 7}),
        ("ai_chatter", {"nudge_fan_ids": [FAN], "max_replies": 9}), ("ai_chatter", {"nudge_fan_ids": [FAN + 1]}),
        ("ai_chatter", {"nudge_fan_ids": [FAN, FAN + 1]}), ("ai_chatter", {"nudge_fan_ids": []}),
        ("ai_chatter", {"nudge_fan_ids": [str(FAN)]}), ("ai_chatter", {"nudge_fan_ids": FAN}),
        ("ai_chatter", {"aftercare": 3}), ("ai_chatter", {"rhythm_resume": True, "only_fan_ids": [FAN]}),
        ("ai_chatter", None), ("ai_chatter", [7]), ("ai_chatter", "post_buy"), ("ai_chatter", 7),
        ("make_right", {"post_buy": 7}), ("gen_info", {"discount_offer": 7}), ("", {"post_buy": 7}), (None, {"post_buy": 7}),
    ]
    for kind, payload in forged:
        check(job_kind(kind, payload, FAN) is None, ("forged payload accepted", kind, payload))
    check(job_kind("ai_chatter", {"nudge_fan_ids": [True]}, 1) is None, "bool fan id in a nudge")

    # FollowupQueue.add forwards only the three exact shapes, and only a datetime run_at.
    calls = []

    class StubLedger:
        def followup_add(self, **kw):
            calls.append(kw)
            return "job-1"

    q = FollowupQueue(StubLedger(), "acct", lambda: datetime(2026, 10, 16, 12))
    when = datetime(2026, 10, 16, 12, 5)
    check(q.add(FAN, "ai_chatter", {"nudge_fan_ids": [FAN]}, when) == "job-1" and calls[-1]["kind"] == "nudge"
          and calls[-1]["payload"] == {"nudge_fan_ids": [FAN]}, "nudge")
    check(q.add(FAN, "ai_chatter", {"discount_offer": 7}, when) == "job-1" and calls[-1]["kind"] == "discount"
          and calls[-1]["payload"] == {"discount_offer": 7}, "discount")
    check(q.add(FAN, "ai_chatter", {"post_buy": 7}, when) == "job-1" and calls[-1]["kind"] == "post_buy", "post_buy")
    n = len(calls)
    check(q.add(FAN, "ai_chatter", {"post_buy": True}, when) is None, "bool offer id")
    check(q.add(FAN, "ai_chatter", {"post_buy": 7}, "2026-10-16T12:05:00") is None, "run_at must be a datetime")
    check(q.add(FAN, "gen_info", {"post_buy": 7}, when) is None, "other job kind")
    check(len(calls) == n, "nothing reached the ledger")

    # The engine's enqueue_job (what ai_chatter calls) queues ONLY the stock nudge, even though the queue accepts more.
    queue_calls = []

    class RecQueue:
        def add(self, fan, kind, payload, run_at):
            queue_calls.append((fan, kind, payload))
            return "j"

    events = []
    engine = NativeEngine.__new__(NativeEngine)
    engine.fan_id, engine.followups, engine._emit, engine._nudge_queued = FAN, RecQueue(), events.append, False
    engine._account = lambda account: None

    async def drive():
        for kind, payload in (("ai_chatter", {"post_buy": 7}), ("ai_chatter", {"discount_offer": 7}),
                              ("ai_chatter", {"nudge_fan_ids": [FAN + 1]}), ("make_right", {"nudge_fan_ids": [FAN]})):
            assert await engine.enqueue_job("acct", kind, payload=payload, run_at=when) is None
        check(not queue_calls, "post_buy / discount / other fan's nudge never reach the queue from ai_chatter")
        check(len([e for e in events if e["event"] == "native-background-job-suppressed"]) == 4, "all four suppressed")
        await engine.enqueue_job("acct", "ai_chatter", payload={"nudge_fan_ids": [FAN]}, run_at=when)
        check(queue_calls == [(FAN, "ai_chatter", {"nudge_fan_ids": [FAN]})], "the stock nudge is queued")
        check(events[-1]["event"] == "native-followup-queued" and events[-1]["kind"] == "nudge")
        engine.followups = None
        queue_calls.clear()
        await engine.enqueue_job("acct", "ai_chatter", payload={"nudge_fan_ids": [FAN]}, run_at=when)
        check(not queue_calls and events[-1]["event"] == "native-background-job-suppressed", "flag off: suppressed")

    asyncio.run(drive())


# ── 3. ledger ────────────────────────────────────────────────────────────────

def test_ledger(root):
    from native_pilot.ledger import Ledger
    L, f = build(root, "ledger")
    NOW, ACCT, FAN = L.NOW, L.ACCT, L.FAN
    OTHER = FAN + 1
    offer(f, L, 1, offered=NOW)                             # this fan's, this account's
    offer(f, L, 2, fan=OTHER, offered=NOW)                  # another fan's
    offer(f, L, 3, account="other-account", offered=NOW)    # another account's
    run_at = NOW + timedelta(minutes=2)

    ticks = [0]

    def add(kind, payload, *, fan=FAN, account=ACCT, led=None, now=NOW):
        # job ids are a hash of (account, fan, kind, run_at, now): give every call its own run_at (see test_job_id_collision)
        ticks[0] += 1
        return (led or f.ledger).followup_add(account_id=account, fan_id=fan, kind=kind, payload=payload,
                                              run_at=run_at + timedelta(seconds=ticks[0]), now=now)

    def rows(kind=None):
        if not f.sql("SELECT 1 FROM sqlite_master WHERE name='native_pilot_followups'"):
            return []
        sql = "SELECT kind,state,reason,payload_json FROM native_pilot_followups"
        return f.sql(sql + (" WHERE kind=? ORDER BY created_at,job_id" if kind else ""), (kind,) if kind else ())

    # ownership re-proof: wrong fan, wrong account, missing offer, bad id types -> nothing queued
    for kind, key in (("post_buy", "post_buy"), ("discount", "discount_offer")):
        refuses("followup-offer", lambda: add(kind, {key: 2}))                  # another fan's offer
        refuses("followup-offer", lambda: add(kind, {key: 3}))                  # another account's offer
        refuses("followup-offer", lambda: add(kind, {key: 99}))                 # no such offer
        refuses("followup-offer", lambda: add(kind, {key: 1}, account="other-account"))   # right offer, wrong account
        refuses("followup-offer", lambda: add(kind, {key: 1}, fan=OTHER))       # right offer, wrong fan
        refuses("followup-offer", lambda: add(kind, {key: True}))
        refuses("followup-offer", lambda: add(kind, {key: "1"}))
        refuses("followup-offer", lambda: add(kind, {key: 0}))
        refuses("followup-offer", lambda: add(kind, {}))
        refuses("followup-offer", lambda: add(kind, None))
    refuses("followup-kind", lambda: add("aftercare", {"aftercare": 1}))
    refuses("invalid-identity", lambda: add("post_buy", {"post_buy": 1}, fan=True))
    refuses("invalid-identity", lambda: add("post_buy", {"post_buy": 1}, fan="1"))
    refuses("followup-run-at", lambda: f.ledger.followup_add(account_id=ACCT, fan_id=FAN, kind="nudge", payload={"nudge_fan_ids": [FAN]},
                                                            run_at=NOW + timedelta(hours=25), now=NOW))
    check(rows() == [], "every refused add left no row")

    # post_buy: one opportunity per purchase, forever
    first = add("post_buy", {"post_buy": 1})
    check(isinstance(first, str) and first, "post_buy queued")
    check(add("post_buy", {"post_buy": 1}) is None, "duplicate purchase event: not queued twice")
    check(add("post_buy", {"post_buy": 1}, led=Ledger(f.path)) is None, "restart (fresh ledger object): not queued twice")
    offer(f, L, 4, offered=NOW)
    check(add("post_buy", {"post_buy": 4}) is not None, "a different purchase is its own opportunity")
    for state in ("done", "dropped", "failed"):
        f.ledger.followup_finish(job_id=first, state=state, reason="x", now=NOW)
        check(f.sql("SELECT state FROM native_pilot_followups WHERE job_id=?", (first,))[0][0] == state)
        check(add("post_buy", {"post_buy": 1}) is None, "post_buy settled as " + state + " is never queued again")
        f.sql("UPDATE native_pilot_followups SET state='queued' WHERE job_id=?", (first,))
    f.sql("UPDATE native_pilot_followups SET state='dropped' WHERE job_id=?", (first,))
    inbound(f, L, 700, "hello again", NOW + timedelta(minutes=1))
    check(add("post_buy", {"post_buy": 1}) is None, "post_buy: a new inbound never re-opens a dropped purchase")
    check(len(rows("post_buy")) == 2, rows())

    # discount: queued job is not doubled; a dropped one returns only after a NEW inbound
    f.sql("DELETE FROM native_pilot_followups")
    f.sql("DELETE FROM messages")
    job = add("discount", {"discount_offer": 1})
    check(job, "discount queued")
    check(add("discount", {"discount_offer": 1}) is None, "queued discount is not doubled")
    check(f.ledger.followup_finish(job_id=job, state="dropped", reason="not-eligible", now=NOW))
    check(add("discount", {"discount_offer": 1}) is None, "dropped, fan said nothing new (no inbound at all): not re-queued")
    inbound(f, L, 800, "can you do it cheaper?", NOW + timedelta(minutes=5))
    job2 = add("discount", {"discount_offer": 1})
    check(job2 and job2 != job, "a NEW inbound since the dropped job allows one re-queue")
    check(add("discount", {"discount_offer": 1}) is None, "...and it is not doubled while queued")
    f.ledger.followup_finish(job_id=job2, state="dropped", reason="not-eligible", now=NOW)
    check(add("discount", {"discount_offer": 1}) is None, "dropped again with the same newest inbound: not re-queued")
    inbound(f, L, 801, "still too pricey", NOW + timedelta(minutes=6))
    job3 = add("discount", {"discount_offer": 1})
    check(job3, "another new inbound: allowed again")
    for n, state in enumerate(("done", "failed")):
        f.ledger.followup_finish(job_id=job3, state=state, reason=None, now=NOW)
        inbound(f, L, 810 + n, "hello", NOW + timedelta(minutes=7 + n))
        check(add("discount", {"discount_offer": 1}) is None, "discount settled as %s is never queued again" % state)
        f.sql("UPDATE native_pilot_followups SET state='queued' WHERE job_id=?", (job3,))
    check(all(r[3] == '{"discount_offer":1}' for r in rows("discount")), rows())

    # followups_retire: a purchase drops this fan's nudge + discount, never post_buy, never another fan
    f.sql("DELETE FROM native_pilot_followups")
    nud = add("nudge", {"nudge_fan_ids": [FAN]})
    dis = add("discount", {"discount_offer": 1})
    buy = add("post_buy", {"post_buy": 4})
    other_nud = add("nudge", {"nudge_fan_ids": [OTHER]}, fan=OTHER)
    other_dis = add("discount", {"discount_offer": 2}, fan=OTHER)
    check(nud and dis and buy and other_nud and other_dis)
    check(f.ledger.followups_retire(account_id=ACCT, fan_id=FAN, kinds=("nudge", "discount"), reason="purchased", now=NOW) == 2)
    state = {r[0]: (r[1], r[2]) for r in f.sql("SELECT job_id,state,reason FROM native_pilot_followups")}
    check(state[nud] == ("dropped", "purchased") and state[dis] == ("dropped", "purchased"), state)
    check(state[buy] == ("queued", None), "post_buy survives its own retire")
    check(state[other_nud][0] == "queued" and state[other_dis][0] == "queued", "another fan's jobs untouched")
    check(f.ledger.followups_retire(account_id=ACCT, fan_id=FAN, kinds=("nudge", "discount"), reason="purchased", now=NOW) == 0, "idempotent")
    check(f.ledger.followups_retire(account_id="other-account", fan_id=OTHER, kinds=("nudge",), reason="x", now=NOW) == 0, "account scoped")
    refuses("followup-kind", lambda: f.ledger.followups_retire(account_id=ACCT, fan_id=FAN, kinds=("bogus",), reason="x", now=NOW))
    refuses("followup-kind", lambda: f.ledger.followups_retire(account_id=ACCT, fan_id=FAN, kinds=(), reason="x", now=NOW))
    # due list shows only what is still queued
    due = {j["job_id"] for j in f.ledger.followups_due(account_id=ACCT, now=run_at + timedelta(minutes=1))}
    check(due == {buy, other_nud, other_dis}, due)


def test_job_id_collision(root):
    """PROBE: two offer-keyed jobs of one fan queued with the same run_at and the same clock tick.

    ``job_id`` hashes (account, fan, kind, run_at, now) but not the payload, so two different unpaid cards of one
    fan that become eligible in the same scan (discount run_at == now) collide on the primary key."""
    L, f = build(root, "collision")
    offer(f, L, 1, offered=L.NOW)
    offer(f, L, 2, offered=L.NOW)
    run_at = L.NOW + timedelta(seconds=100)
    first = f.ledger.followup_add(account_id=L.ACCT, fan_id=L.FAN, kind="discount", payload={"discount_offer": 1}, run_at=run_at, now=L.NOW)
    check(first, "first job queued")
    try:
        second = f.ledger.followup_add(account_id=L.ACCT, fan_id=L.FAN, kind="discount", payload={"discount_offer": 2}, run_at=run_at, now=L.NOW)
    except Exception as exc:
        KNOWN_BUGS.append("ledger.followup_add: job_id omits the payload; a second offer-keyed job of the same fan/kind/run_at/now "
                          "raises %s instead of queueing (or refusing)" % type(exc).__name__)
    else:
        check(second and second != first, "distinct offers get distinct jobs")


# ── 4 + 5 + 6. purchases, unpaid cards, scan, block ───────────────────────────

def test_purchases_and_cards(root):
    from native_pilot import postsale
    from automations import _price_ask
    L, f = build(root, "scan-sql")
    NOW, FAN = L.NOW, L.FAN
    fans = {10: FAN, 11: FAN + 1, 19: FAN + 2}
    # --- purchases
    offer(f, L, 10, fan=fans[10], item=1, status="delivered", by="ppv_ledger", resolved=NOW - timedelta(minutes=5))
    offer(f, L, 11, fan=fans[11], item=2, status="delivered", by="ppv_ledger", resolved=(NOW - timedelta(minutes=1)).isoformat() + "Z")
    offer(f, L, 12, item=1, status="delivered", by="ppv_ledger", resolved=NOW - timedelta(minutes=16))      # history
    offer(f, L, 13, item=1, status="delivered", by="fan_claim", resolved=NOW - timedelta(minutes=2))        # "i bought it"
    offer(f, L, 14, item=1, status="open", resolved=None)                                                    # still open
    offer(f, L, 15, item=3, status="delivered", by="ppv_ledger", resolved=NOW - timedelta(minutes=2))       # catalogue, not lane
    offer(f, L, 16, item=1, mode="dm", status="delivered", by="ppv_ledger", resolved=NOW - timedelta(minutes=2))
    offer(f, L, 17, item=1, msg=None, status="delivered", by="ppv_ledger", resolved=NOW - timedelta(minutes=2))
    offer(f, L, 18, item=1, status="delivered", by="ppv_ledger", resolved=NOW + timedelta(minutes=2))       # clock-skew future
    offer(f, L, 19, fan=fans[19], item=1, status="delivered", by="ppv_ledger", resolved=NOW - timedelta(minutes=15))   # edge
    offer(f, L, 20, item=1, status="delivered", by="ppv_ledger", resolved=NOW - timedelta(minutes=2), account="other-account")
    offer(f, L, 21, item=1, status="delivered", by=None, resolved=NOW - timedelta(minutes=2))               # local status, nobody confirmed
    offer(f, L, 22, item=1, status="expired", by="ppv_ledger", resolved=NOW - timedelta(minutes=2))
    # a plain outgoing paid card with its message but NO offer row, and one fan message saying "i bought it"
    f.sql("INSERT INTO messages(account_id,fan_id,message_id,direction,body,media_ids,media_count,price_cents,is_tip,is_unsent,"
          "created_at,ingested_at,raw_json,is_paid) VALUES(?,?,9001,'out','card','[]',2,1250,0,0,?,?,'{}',1)",
          (L.ACCT, FAN, ts(NOW), ts(NOW)))
    inbound(f, L, 9002, "i bought it", NOW)
    with closing(sqlite3.connect(f.path)) as con:
        con.row_factory = sqlite3.Row
        got = postsale.purchases(con, L.ACCT, NOW)
    check(got == [(10, fans[10]), (11, fans[11]), (19, fans[19])], got)
    check(all(isinstance(a, int) and isinstance(b, int) for a, b in got))

    # --- unpaid_cards
    f.sql("DELETE FROM content_offers")
    f.sql("DELETE FROM messages")
    old, young = NOW - timedelta(minutes=100), NOW - timedelta(minutes=30)
    offer(f, L, 30, fan=FAN, item=1, price=1250, offered=old)                                   # due
    offer(f, L, 31, fan=FAN + 1, item=2, price=900, offered=(old - timedelta(minutes=5)).isoformat() + "Z")  # due (hook lane, Z)
    offer(f, L, 32, item=1, offered=young)                                                      # too young
    offer(f, L, 33, item=1, offered=old, status="delivered", by="ppv_ledger", resolved=old)     # already paid
    offer(f, L, 34, item=3, offered=old)                                                        # catalogue
    offer(f, L, 35, item=1, mode="dm", offered=old)
    offer(f, L, 36, item=1, msg=None, offered=old)
    offer(f, L, 37, fan=FAN + 2, item=1, offered=old)                                           # message flagged paid
    offer(f, L, 38, item=1, offered=old, account="other-account")
    offer(f, L, 39, item=1, offered=old, status="expired")
    f.sql("INSERT INTO messages(account_id,fan_id,message_id,direction,body,media_ids,media_count,price_cents,is_tip,is_unsent,"
          "created_at,ingested_at,raw_json,is_paid) VALUES(?,?,?,'out','card','[]',2,1250,0,0,?,?,'{}',1)",
          (L.ACCT, FAN + 2, 5037, ts(old), ts(old)))
    with closing(sqlite3.connect(f.path)) as con:
        con.row_factory = sqlite3.Row
        cards = postsale.unpaid_cards(con, L.ACCT, NOW, 90)
    check([c[:2] for c in cards] == [(30, FAN), (31, FAN + 1)], cards)
    check(cards[0][2] == old and cards[0][3] == 1250 and cards[1][2] == old - timedelta(minutes=5) and cards[1][3] == 900, cards)

    # --- inbound_since only returns the fan's own later messages, oldest first
    f.sql("DELETE FROM messages")
    inbound(f, L, 1, "before", old - timedelta(minutes=1))
    inbound(f, L, 3, "later two", old + timedelta(minutes=9))
    inbound(f, L, 2, "later one", old + timedelta(minutes=2))
    inbound(f, L, 4, "someone else", old + timedelta(minutes=3), fan=FAN + 1)
    f.sql("INSERT INTO messages(account_id,fan_id,message_id,direction,body,media_ids,media_count,price_cents,is_tip,is_unsent,"
          "created_at,ingested_at,raw_json) VALUES(?,?,5,'out','mine','[]',0,0,0,0,?,?,'{}')", (L.ACCT, FAN, ts(old + timedelta(minutes=4)), ts(old)))
    with closing(sqlite3.connect(f.path)) as con:
        con.row_factory = sqlite3.Row
        said = postsale.inbound_since(con, L.ACCT, FAN, old)
    check([b for _, b in said] == ["later one", "later two"], said)

    # --- objection_state / beat_after (pure)
    t0 = NOW
    at = lambda m: t0 + timedelta(minutes=m)
    objected, blocked = _price_ask.objection_state([])
    check(objected is None and blocked is None and not _price_ask.beat_after([], objected), "silence: no objection, no beat")
    s = [(at(1), "hey"), (at(2), "lol ok")]
    objected, blocked = _price_ask.objection_state(s)
    check(objected is None and blocked is None and not _price_ask.beat_after(s, objected), "chatter without an objection")
    s = [(at(1), "can you do it cheaper?")]
    objected, blocked = _price_ask.objection_state(s)
    check(objected == at(1) and blocked is None, "price ask is an objection")
    check(not _price_ask.beat_after(s, objected), "objection without a later message: no beat")
    s = [(at(1), "can you do it cheaper?"), (at(3), "hello?")]
    objected, blocked = _price_ask.objection_state(s)
    check(objected == at(1) and _price_ask.beat_after(s, objected), "objection then later message: beat")
    s = [(at(1), "can you do 15"), (at(2), "?")]
    objected, blocked = _price_ask.objection_state(s)
    check(objected == at(1) and _price_ask.beat_after(s, objected), "a stated cap is an objection")
    s = [(at(1), "thats a bit steep"), (at(2), "ok"), (at(3), "can you do it cheaper?")]
    objected, blocked = _price_ask.objection_state(s)
    check(objected == at(3) and not _price_ask.beat_after(s, objected), "the NEWEST objection counts: nothing after it")
    s = [(at(1), "no thanks"), (at(2), "can you do it cheaper?"), (at(3), "?")]
    objected, blocked = _price_ask.objection_state(s)
    check(blocked == _price_ask.BLOCK_DECLINED, "a decline anywhere blocks")
    s = [(at(1), "can you do it cheaper?"), (at(2), "stop messaging me")]
    check(_price_ask.objection_state(s)[1] == _price_ask.BLOCK_DECLINED, "hard stop blocks")
    s = [(at(1), "can you do it cheaper?"), (at(2), "its too much for me right now")]
    check(_price_ask.objection_state(s)[1] == _price_ask.BLOCK_DISTRESS, "money trouble blocks")
    s = [(at(1), "can you do it cheaper?"), (at(2), "i cant afford this i have no money")]
    check(_price_ask.objection_state(s)[1] == _price_ask.BLOCK_DECLINED, "a soft decline that is also distress is judged a decline first")
    s = [(at(1), "its too much for me right now"), (at(2), "no thanks")]
    check(_price_ask.objection_state(s)[1] == _price_ask.BLOCK_DISTRESS, "the first blocker wins")


def make_service(L, f, events):
    from native_pilot import service as S, runner
    from native_pilot.followups import FollowupQueue
    svc = S.Service.__new__(S.Service)
    svc.args = SimpleNamespace(fan=L.FAN)
    svc.all_fans = False
    svc.path, svc.db, svc.ledger = f.path, L.ACCT, f.ledger
    svc.followups = FollowupQueue(f.ledger, L.ACCT, lambda: L.NOW)
    svc._outcomes = set()
    return svc


def test_unlinked_payment_blocks_the_fans_discount(root):
    """A paid ppv_message with no message link makes that fan's card look unpaid: no discount may be queued for them."""
    from native_pilot import postsale
    L, f = build(root, "unlinked")
    NOW, FAN = L.NOW, L.FAN
    old = NOW - timedelta(minutes=100)
    bound = NOW - timedelta(days=2)
    offer(f, L, 1, fan=FAN, offered=old)
    offer(f, L, 2, fan=FAN + 1, offered=old)
    with closing(sqlite3.connect(f.path)) as con:
        con.row_factory = sqlite3.Row
        check([c[:2] for c in postsale.unpaid_cards(con, L.ACCT, NOW, 90)] == [(1, FAN), (2, FAN + 1)], "no transactions table: nothing blocked")
        check(postsale.unlinked_paid_fans(con, L.ACCT) == set(), "no transactions table")
    f.sql("CREATE TABLE transactions(id INTEGER PRIMARY KEY,account_id TEXT,fan_id INTEGER,kind TEXT,message_id INTEGER,"
          "occurred_at TEXT,source TEXT,status TEXT)")
    f.sql("CREATE TABLE of_runtime(account_id TEXT,binding_id TEXT)")
    f.sql("CREATE TABLE of_creator_bindings(id TEXT,created_at TEXT)")
    f.sql("INSERT INTO of_runtime VALUES(?,?)", (L.ACCT, "b1"))
    f.sql("INSERT INTO of_creator_bindings VALUES(?,?)", ("b1", ts(bound)))

    def tx(i, *, fan=FAN, kind="ppv_message", msg=None, at=None, source="ledger", status="cleared", account=None):
        f.sql("INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?)",
              (i, account or L.ACCT, fan, kind, msg, ts(at or NOW - timedelta(hours=3)), source, status))

    def cards():
        with closing(sqlite3.connect(f.path)) as con:
            con.row_factory = sqlite3.Row
            return [c[1] for c in postsale.unpaid_cards(con, L.ACCT, NOW, 90)]

    tx(1, fan=FAN + 1, msg=777)                                   # linked
    tx(2, fan=FAN + 1, status="refunded")                         # not paid
    tx(3, fan=FAN + 1, at=bound - timedelta(hours=1))             # before the current binding
    tx(4, fan=FAN + 1, kind="tip")
    tx(5, fan=FAN + 1, account="other-account")
    tx(6, fan=FAN + 1, source="ws")
    check(cards() == [FAN, FAN + 1], "linked, refunded, historical, other-kind, other-account and non-ledger rows block no one")
    tx(7, fan=FAN, status="pending")
    check(cards() == [FAN + 1], cards())
    with closing(sqlite3.connect(f.path)) as con:
        con.row_factory = sqlite3.Row
        check(postsale.unlinked_paid_fans(con, L.ACCT) == {FAN}, "only the unlinked paid fan")
    svc = make_service(L, f, [])
    config(f, L, {postsale.SETTING_DISCOUNT: True})
    job = lambda fan: {"kind": "discount", "fan_id": fan, "payload_json": json.dumps({"discount_offer": 1 if fan == FAN else 2})}
    check(svc.postsale_block(job(FAN)) == "unlinked-payment", "a queued discount is refused at run time too")
    check(svc.postsale_block(job(FAN + 1)) is None, "another fan's discount still runs")
    # The post-purchase upsell also puts a priced card on the wire: same guard.
    offer(f, L, 3, fan=FAN, status="delivered", by="ppv_ledger", resolved=NOW)
    offer(f, L, 4, fan=FAN + 1, status="delivered", by="ppv_ledger", resolved=NOW)
    config(f, L, {postsale.SETTING_POST_BUY: True})
    buy = lambda fan: {"kind": "post_buy", "fan_id": fan, "payload_json": json.dumps({"post_buy": 3 if fan == FAN else 4})}
    check(svc.postsale_block(buy(FAN)) == "unlinked-payment", "post-buy upsell is refused for the unlinked fan")
    check(svc.postsale_block(buy(FAN + 1)) is None, "post-buy upsell still runs for another fan")
    check(svc.fan_has_unlinked_payment(FAN) is True and svc.fan_has_unlinked_payment(FAN + 1) is False, "per-fan read")


def test_scan(root):
    from native_pilot import postsale, runner
    L, f = build(root, "scan")
    NOW, FAN, ACCT = L.NOW, L.FAN, L.ACCT
    events = []
    with patch.object(runner, "now", lambda: NOW), patch.object(runner, "emit", events.append):
        svc = make_service(L, f, events)
        both = {postsale.SETTING_DISCOUNT: True, postsale.SETTING_POST_BUY: True}

        def has_table():
            return bool(f.sql("SELECT 1 FROM sqlite_master WHERE name='native_pilot_followups'"))

        def queued(kind=None):
            if not has_table():
                return []
            sql = "SELECT kind,payload_json,state,reason,run_at FROM native_pilot_followups"
            return f.sql(sql + (" WHERE kind=?" if kind else ""), (kind,) if kind else ())

        def reset():
            if has_table():
                f.sql("DELETE FROM native_pilot_followups")
            f.sql("DELETE FROM messages")
            f.sql("DELETE FROM content_offers")
            svc._outcomes.clear()
            events.clear()

        offered = NOW - timedelta(minutes=100)

        # switches off (default): nothing, even with a perfect candidate
        reset()
        config(f, L, None)
        offer(f, L, 1, price=1250, offered=offered)
        inbound(f, L, 1, "can you do it cheaper?", offered + timedelta(minutes=5))
        inbound(f, L, 2, "hello?", offered + timedelta(minutes=9))
        offer(f, L, 2, status="delivered", by="ppv_ledger", resolved=NOW - timedelta(minutes=3))
        svc.scan_postsale()
        check(queued() == [] and not events, "default off: nothing queued, nothing emitted")
        config(f, L, {postsale.SETTING_DISCOUNT: "true", postsale.SETTING_POST_BUY: 1})
        svc.scan_postsale()
        check(queued() == [], "non-literal-true: nothing queued")
        followups, svc.followups = svc.followups, None
        config(f, L, both)
        svc.scan_postsale()
        check(queued() == [], "no --followups queue: nothing queued")
        svc.followups = followups

        # only the discount switch: the purchase is ignored, the objection+beat card is queued
        config(f, L, {postsale.SETTING_DISCOUNT: True})
        svc.scan_postsale()
        check([q[0] for q in queued()] == ["discount"] and queued()[0][1] == '{"discount_offer":1}' and queued()[0][2] == "queued", queued())
        check(events == [{"event": "discount-queued", "fan_id": FAN, "offer_id": 1}], events)
        svc.scan_postsale()
        check(len(queued("discount")) == 1, "a second scan does not double the queued discount")

        # only the post_buy switch: the card is ignored, the ledger-settled purchase is queued
        reset()
        config(f, L, {postsale.SETTING_POST_BUY: True})
        offer(f, L, 1, price=1250, offered=offered)
        inbound(f, L, 1, "can you do it cheaper?", offered + timedelta(minutes=5))
        inbound(f, L, 2, "hello?", offered + timedelta(minutes=9))
        offer(f, L, 2, status="delivered", by="ppv_ledger", resolved=NOW - timedelta(minutes=3))
        svc.scan_postsale()
        check([q[0] for q in queued()] == ["post_buy"] and queued()[0][1] == '{"post_buy":2}', queued())

        # discount rules, one case at a time (price 1250 unless stated)
        config(f, L, {postsale.SETTING_DISCOUNT: True})

        def case(label, msgs, *, price=1250, want=None, outcome=None, offered_at=offered):
            reset()
            offer(f, L, 1, price=price, offered=offered_at)
            for i, (minutes, body) in enumerate(msgs, 1):
                inbound(f, L, i, body, offered_at + timedelta(minutes=minutes))
            svc.scan_postsale()
            check([q[0] for q in queued()] == (["discount"] if want else []), (label, queued()))
            skipped = [e for e in events if e["event"] == "discount-skipped"]
            check([e["reason"] for e in skipped] == ([outcome] if outcome else []), (label, events))
            return skipped

        case("silence alone", [], want=False)
        case("unrelated chatter", [(5, "hey"), (9, "lol ok")], want=False)
        case("objection, nothing after", [(5, "can you do it cheaper?")], want=False)
        case("objection then more", [(5, "can you do it cheaper?"), (9, "hello?")], want=True)
        case("stated cap then more", [(5, "can you do 15"), (9, "??")], want=True)
        case("message BEFORE the card does not count", [(-9, "can you do it cheaper?"), (-5, "hello?")], want=False)
        case("decline blocks", [(5, "can you do it cheaper?"), (9, "no thanks")], want=False, outcome="fan-declined")
        case("hard stop blocks", [(5, "can you do it cheaper?"), (9, "stop messaging me")], want=False, outcome="fan-declined")
        case("distress blocks", [(5, "can you do it cheaper?"), (9, "its too much for me right now")], want=False, outcome="fan-distress")
        sk = case("price at the floor ($3)", [(5, "can you do it cheaper?"), (9, "hello?")], price=300, want=False, outcome="price-at-minimum")
        check(sk[0]["offer_id"] == 1 and sk[0]["fan_id"] == FAN, sk)
        case("below the floor", [(5, "can you do it cheaper?"), (9, "hello?")], price=100, want=False, outcome="price-at-minimum")
        case("one cent over the floor", [(5, "can you do it cheaper?"), (9, "hello?")], price=301, want=True)
        case("card too young", [(5, "can you do it cheaper?"), (9, "hello?")], want=False, offered_at=NOW - timedelta(minutes=60))
        # the outcome is reported once per (kind, offer, reason)
        case("decline blocks (again)", [(5, "can you do it cheaper?"), (9, "no thanks")], want=False, outcome="fan-declined")
        svc.scan_postsale(); svc.scan_postsale()
        check(len([e for e in events if e["event"] == "discount-skipped"]) == 1, "outcome reported once")

        # a fan outside this runner's scope is never queued for
        reset()
        offer(f, L, 1, fan=FAN + 5, price=1250, offered=offered)
        inbound(f, L, 1, "can you do it cheaper?", offered + timedelta(minutes=5), fan=FAN + 5)
        inbound(f, L, 2, "hello?", offered + timedelta(minutes=9), fan=FAN + 5)
        svc.scan_postsale()
        check(queued() == [], "another fan is out of scope")

        # post-buy: queued once with FASTT's own 90-240 s delay; nudge + discount retired as 'purchased'
        config(f, L, both)
        reset()
        offer(f, L, 1, price=1250, offered=offered, status="delivered", by="ppv_ledger", resolved=NOW - timedelta(minutes=2))
        nud = f.ledger.followup_add(account_id=ACCT, fan_id=FAN, kind="nudge", payload={"nudge_fan_ids": [FAN]},
                                    run_at=NOW + timedelta(minutes=10), now=NOW)
        offer(f, L, 2, price=1250, offered=offered)
        dis = f.ledger.followup_add(account_id=ACCT, fan_id=FAN, kind="discount", payload={"discount_offer": 2},
                                    run_at=NOW + timedelta(minutes=1), now=NOW)
        check(nud and dis)
        svc.scan_postsale()
        buys = queued("post_buy")
        check(len(buys) == 1 and buys[0][1] == '{"post_buy":1}' and buys[0][2] == "queued", queued())
        delay = (datetime.fromisoformat(buys[0][4]) - NOW).total_seconds()
        check(90 <= delay <= 240, delay)
        check(queued("nudge")[0][2:4] == ("dropped", "purchased") and queued("discount")[0][2:4] == ("dropped", "purchased"), queued())
        check(any(e["event"] == "post-buy-queued" and e["offer_id"] == 1 and e["fan_id"] == FAN for e in events), events)
        events.clear()
        svc.scan_postsale()
        check(len(queued("post_buy")) == 1 and not [e for e in events if e["event"] == "post-buy-queued"],
              "a repeat scan (restart) cannot queue the purchase twice")
        # a purchase older than 15 minutes is history
        reset()
        offer(f, L, 1, status="delivered", by="ppv_ledger", resolved=NOW - timedelta(minutes=16))
        svc.scan_postsale()
        check(queued() == [], "stale purchase")
        # all-fans scope uses the owner/hold fences: an unowned fan is skipped
        reset()
        offer(f, L, 1, fan=FAN + 9, status="delivered", by="ppv_ledger", resolved=NOW - timedelta(minutes=2))
        svc.all_fans, svc._owned = True, (lambda fan: fan == FAN)
        with patch("native_pilot.service.fan_hold_reason", lambda *a: None):
            svc.scan_postsale()
        check(queued() == [], "all-fans: unowned fan skipped")
        f.sql("UPDATE content_offers SET fan_id=?", (FAN,))
        with patch("native_pilot.service.fan_hold_reason", lambda *a: "operator-hold"):
            svc.scan_postsale()
        check(queued() == [], "all-fans: held fan skipped")
        with patch("native_pilot.service.fan_hold_reason", lambda *a: None):
            svc.scan_postsale()
        check([q[0] for q in queued()] == ["post_buy"], "all-fans: owned, unheld fan queued")
        svc.all_fans = False


def test_block(root):
    from native_pilot import postsale
    L, f = build(root, "block")
    NOW, FAN = L.NOW, L.FAN
    svc = make_service(L, f, [])
    offer(f, L, 1, status="open", offered=NOW)
    offer(f, L, 2, status="delivered", by="ppv_ledger", resolved=NOW)
    offer(f, L, 3, status="delivered", by="fan_claim", resolved=NOW)
    offer(f, L, 4, status="expired", resolved=NOW)
    offer(f, L, 5, status="cancelled", resolved=NOW)
    offer(f, L, 6, fan=FAN + 1, status="open", offered=NOW)
    offer(f, L, 7, status="open", offered=NOW, account="other-account")

    def job(kind, oid, fan=FAN):
        key = {"discount": "discount_offer", "post_buy": "post_buy"}[kind]
        return {"kind": kind, "fan_id": fan, "payload_json": json.dumps({key: oid})}

    both = {postsale.SETTING_DISCOUNT: True, postsale.SETTING_POST_BUY: True}
    for value in (None, {}, {postsale.SETTING_DISCOUNT: "true", postsale.SETTING_POST_BUY: 1}):
        config(f, L, value)
        for kind in ("discount", "post_buy"):
            check(svc.postsale_block(job(kind, 1)) == "feature-disabled", (kind, value))
    config(f, L, {postsale.SETTING_DISCOUNT: True})
    check(svc.postsale_block(job("discount", 1)) is None, "discount on, discount job runs")
    check(svc.postsale_block(job("post_buy", 2)) == "feature-disabled", "post_buy switch is separate")
    config(f, L, {postsale.SETTING_POST_BUY: True})
    check(svc.postsale_block(job("discount", 1)) == "feature-disabled", "discount switch is separate")
    check(svc.postsale_block(job("post_buy", 2)) is None, "post_buy on, settled purchase runs")

    config(f, L, both)
    check(svc.postsale_block(job("discount", 1)) is None, "open offer: discount may run")
    check(svc.postsale_block(job("discount", 2)) == "already-purchased", "delivered: already purchased")
    check(svc.postsale_block(job("discount", 3)) == "already-purchased", "delivered by anything: already purchased")
    check(svc.postsale_block(job("discount", 4)) == "offer-resolved", "expired")
    check(svc.postsale_block(job("discount", 5)) == "offer-resolved", "cancelled")
    check(svc.postsale_block(job("discount", 99)) == "offer-mismatch", "missing offer")
    check(svc.postsale_block(job("discount", 6)) == "offer-mismatch", "another fan's offer")
    check(svc.postsale_block(job("discount", 1, fan=FAN + 1)) == "offer-mismatch", "job fan differs from the offer's fan")
    check(svc.postsale_block(job("discount", 7)) == "offer-mismatch", "another account's offer")
    check(svc.postsale_block(job("post_buy", 2)) is None, "ledger-resolved purchase: upsell may run")
    check(svc.postsale_block(job("post_buy", 3)) == "not-purchased", "delivered but not by the ledger")
    check(svc.postsale_block(job("post_buy", 1)) == "not-purchased", "open offer")
    check(svc.postsale_block(job("post_buy", 4)) == "not-purchased", "expired offer")
    check(svc.postsale_block(job("post_buy", 99)) == "offer-mismatch", "missing offer")
    check(svc.postsale_block(job("post_buy", 6)) == "offer-mismatch", "another fan's offer")
    check(svc.postsale_block(job("post_buy", 7)) == "offer-mismatch", "another account's offer")
    check(svc.offer_row(True) is None and svc.offer_row("1") is None and svc.offer_row(None) is None, "offer_row: ints only")
    row = svc.offer_row(2)
    check(row["status"] == "delivered" and row["resolved_by"] == "ppv_ledger" and row["fan_id"] == FAN, row)


# ── 7. gateway ───────────────────────────────────────────────────────────────

class HttpError(Exception):
    def __init__(self, status, headers=None):
        super().__init__("http %s" % status)
        self.response = SimpleNamespace(status_code=status, headers=headers or {})


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error
        self.deletes, self.posts = [], []

    def delete_json(self, url, **kw):
        self.deletes.append((url, kw))
        if self.error is not None:
            raise self.error
        return self.result

    def post_json(self, url, **kw):
        self.posts.append((url, kw))
        body = kw["json_body"]
        return {"id": 4242, "text": body["text"], "isFromMe": True, "fromUser": {"id": 176774528}, "withUser": {"id": 318022380},
                "createdAt": datetime.utcnow().isoformat() + "Z", "price": body["price"], "media": []}

    def vault_media_by_id(self, media_id):
        return {"id": media_id, "type": "photo", "canView": True}


def bare_gateway(client, expected=None):
    from native_pilot import runner
    g = runner.Gateway.__new__(runner.Gateway)
    g.client, g.pilot = client, SimpleNamespace(fan_id=318022380)
    g.expected_unsend, g.unsend_id, g.unsent_ids, g.stopped_reason = expected, None, set(), None
    return g


def test_gateway_unit():
    from native_pilot import runner
    from native_pilot.ledger import Refused
    # arm_unsend: only the one expected id, once
    for expected, bad in ((777, 778), (777, "777"), (777, True), (777, 777.0), (777, None), (None, 777), (None, None)):
        g = bare_gateway(FakeClient(), expected)
        refuses("unsend-scope-refused", lambda: g.arm_unsend(bad))
        check(g.unsend_id is None and g.stopped_reason == "unsend-scope-refused", (expected, bad))
    g = bare_gateway(FakeClient(), 777)
    g.arm_unsend(777)
    check(g.unsend_id == 777)
    refuses("unsend-scope-refused", lambda: g.arm_unsend(777))      # a second arming is refused, not stacked
    check(g.unsend_id == 777, "the first arming is kept")

    # _remove_card: success is only a literal True
    c = FakeClient(result={"success": True})
    g = bare_gateway(c)
    g._remove_card(777)
    check(g.unsent_ids == {777}, "success true = removed")
    url, kw = c.deletes[0]
    check(url.endswith("/messages/777") and kw["json_body"] == {"withUserId": 318022380} and kw["proxy_tries"] == 1
          and kw["allow_redirects"] is False, (url, kw))
    # 404 = FASTT's "already removed"
    g = bare_gateway(FakeClient(error=HttpError(404)))
    g._remove_card(777)
    check(g.unsent_ids == {777}, "404 counts as removed")
    # anything else is not "removed" and never lets a replacement go out
    for result in ({"success": False}, {"success": "true"}, {"success": 1}, {"success": None}, {}, {"error": "x"}, None, [], [True], "ok", True, 1, 0):
        g = bare_gateway(FakeClient(result=result))
        try:
            g._remove_card(777)
        except RuntimeError as exc:
            check(str(exc) == "unsend-unconfirmed", (result, exc))
        else:
            raise AssertionError(("unconfirmed unsend was accepted", result))
        check(g.unsent_ids == set(), result)
    for status in (400, 401, 403, 410, 422, 429, 408, 500, 502, 503):
        g = bare_gateway(FakeClient(error=HttpError(status)))
        try:
            g._remove_card(777)
        except HttpError as exc:
            check(exc.response.status_code == status)
        else:
            raise AssertionError(("not raised", status))
        check(g.unsent_ids == set(), status)
    g = bare_gateway(FakeClient(error=ConnectionError("down")))
    try:
        g._remove_card(777)
    except ConnectionError:
        check(g.unsent_ids == set())
    else:
        raise AssertionError("transport error swallowed")
    # how _send classifies those failures
    for status, reason in ((400, "provider-refused"), (401, "account-auth-failed"), (403, "provider-refused"), (410, "provider-refused"),
                           (422, "provider-refused"), (429, "provider-rate-limited")):
        ns = runner._refusal(HttpError(status))
        check(ns is not None and ns.reason == reason, (status, ns))
    check(runner._refusal(HttpError(429, {"retry-after": "7"})).retry_after == 7.0)
    check(runner._refusal(HttpError(404)).reason == "provider-refused", "a 404 on the DELETE is handled before; any other 404 is a refusal")
    for status in (408, 500, 502, 503, 504):
        check(runner._refusal(HttpError(status)) is None, status)
    check(runner._refusal(ConnectionError("down")) is None and runner._refusal(RuntimeError("unsend-unconfirmed")) is None,
          "unconfirmed / transport failures are unknown, not not-sent")


async def test_gateway_send(root):
    """The armed DELETE through the real Gateway._send: ledger, guard, dispatch token and attempt accounting are real."""
    from native_pilot import runner
    from native_pilot.ledger import Refused
    from twin_bridge import coordination
    import test_native_pilot_ledger as L

    class Content:
        catalog_ids = None

        def check_catalog(self, con, account): pass
        def check_send(self, con, account, terms): return terms
        def evidence(self, raw, terms): return {"price_cents": terms["price_cents"]}

    class Reader:
        def __init__(self): pass
        async def preflight_history(self, fan, **kw): return []

    terms = {"media_ids": [2001], "media_types": {"2001": "photo"}, "preview_ids": [2001], "price_cents": 900}
    current = datetime.utcnow().replace(microsecond=0)

    def make(name):
        f = L.Fixture(root, name)
        L.NOW = current - timedelta(seconds=20)
        f.args.update(now=L.NOW, cutoff=L.NOW, deadline=L.NOW + timedelta(minutes=30))
        f.call = dict(pilot_id="synthetic-test", owner_token=L.OWNER, now=L.NOW + timedelta(seconds=10))
        os.environ["DATABASE_URL"] = L.database_url(f.path)
        p = f.activate()
        f.source(100, created_at=(L.NOW + timedelta(seconds=1)).isoformat(sep=" "))
        snap = f.ledger.snapshot(**f.call)
        key = f.ledger.claim_input(**f.call, snapshot=snap)
        coordination.register_owner(L.ACCT, L.FAN, p.owner_token, current + timedelta(minutes=29))
        return f, p, key

    def gateway(f, p, key, client, *, expected=777):
        g = runner.Gateway.__new__(runner.Gateway)
        g.ledger, g.pilot, g.client, g.loop, g.content = f.ledger, p, client, asyncio.get_running_loop(), Content()
        g.scope = runner.AccountScope.parse(p.account_id)
        g.reader = Reader()
        g.input_key, g.followup, g.bubble = key, False, 0
        g.sources = {100: {"created_at": (L.NOW + timedelta(seconds=1)).isoformat(sep=" ")}}
        g.stopped_reason, g.deadline, g.hold_check, g.not_sent = None, p.deadline, None, None
        g.expected_unsend, g.unsend_id, g.unsent_ids = expected, None, set()
        return g

    patches = (patch.object(runner, "now", lambda: current), patch.object(runner, "emit", lambda e: None),
               patch.object(runner, "check_provider_history", lambda *a, **k: None),
               patch.object(runner, "check_followup_history", lambda *a, **k: None),
               patch.object(runner, "check_local", lambda *a, **k: {}))
    for p_ in patches:
        p_.start()
    try:
        # armed but the replacement is a plain text: refused before any attempt, nothing deleted, arming cleared
        f, p, key = make("send-needs-card")
        c = FakeClient(result={"success": True})
        g = gateway(f, p, key, c)
        g.arm_unsend(777)
        try:
            await g._send("just text", None)
        except Refused as exc:
            check(exc.reason == "unsend-needs-card", exc.reason)
        else:
            raise AssertionError("text send with an armed unsend")
        check(c.deletes == [] and c.posts == [] and g.unsend_id is None, "nothing at the wire; arming not carried over")
        check(f.ledger.status(p.pilot_id)["attempts"] == {}, "no attempt was spent")

        # definite 4xx on the DELETE: NotSent path, replacement never posted, attempt is 'not_sent', pilot stays active
        for status, reason, retry in ((403, "provider-refused", False), (429, "provider-rate-limited", True)):
            f, p, key = make("send-4xx-%d" % status)
            c = FakeClient(error=HttpError(status))
            g = gateway(f, p, key, c)
            g.arm_unsend(777)
            try:
                await g._send("here is a cheaper one", terms)
            except Refused as exc:
                check(exc.reason == reason, (status, exc.reason))
            else:
                raise AssertionError("expected refusal")
            check(len(c.deletes) == 1 and c.posts == [] and g.unsent_ids == set(), "DELETE tried, card NOT posted")
            check(g.not_sent is not None and g.not_sent.retry is retry, status)
            check(f.ledger.status(p.pilot_id)["attempts"] == {"not_sent": 1} and f.ledger.status(p.pilot_id)["state"] == "active", status)

        # unknown outcomes on the DELETE: delivery-unknown, replacement never posted, pilot stopped
        for label, client in (("5xx", FakeClient(error=HttpError(502))), ("timeout", FakeClient(error=TimeoutError("t"))),
                              ("success-false", FakeClient(result={"success": False})), ("non-dict", FakeClient(result=["ok"]))):
            f, p, key = make("send-unknown-" + label)
            g = gateway(f, p, key, client)
            g.arm_unsend(777)
            try:
                await g._send("here is a cheaper one", terms)
            except Refused as exc:
                check(exc.reason == "delivery-unknown", (label, exc.reason))
            else:
                raise AssertionError("expected refusal")
            check(len(client.deletes) == 1 and client.posts == [] and g.unsent_ids == set() and g.not_sent is None, label)
            check(f.ledger.status(p.pilot_id)["attempts"] == {"delivery_unknown": 1} and f.ledger.status(p.pilot_id)["state"] == "stopped", label)

        # 404 = already removed: the replacement goes out in the SAME attempt
        for label, client in (("404", FakeClient(error=HttpError(404))), ("success", FakeClient(result={"success": True}))):
            f, p, key = make("send-ok-" + label)
            g = gateway(f, p, key, client)
            g.arm_unsend(777)
            raw = await g._send("here is a cheaper one", terms)
            check(raw["id"] == 4242 and len(client.deletes) == 1 and len(client.posts) == 1 and g.unsent_ids == {777}, label)
            check(client.deletes[0][0].endswith("/messages/777"), label)
            check(f.ledger.status(p.pilot_id)["attempts"] == {"sent": 1}, label)
            check(g.unsend_id is None, "armed for one send only")
    finally:
        for p_ in reversed(patches):
            p_.stop()


def main():
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    dotenv.dotenv_values = lambda *a, **kw: {"OPENROUTER_API_KEY": "synthetic-unused-key"}
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="native-postsale-") as tmp:
        root = Path(tmp)
        env = {"DATABASE_URL": "sqlite+aiosqlite:///" + (root / "bootstrap.db").as_posix(), "TWIN_BRIDGE_ENABLED": "0",
               "TWIN_BRIDGE_SEND_ENABLED": "0", "PYTHON_DOTENV_DISABLED": "1"}
        with patch.dict(os.environ, env):
            test_settings(root)
            test_job_kind(root)
            test_ledger(root)
            test_job_id_collision(root)
            test_purchases_and_cards(root)
            test_scan(root)
            test_unlinked_payment_blocks_the_fans_discount(root)
            test_block(root)
            test_gateway_unit()
            asyncio.run(test_gateway_send(root))
    print(f"{CHECKS} checks; all cases passed")
    for bug in KNOWN_BUGS:
        print("KNOWN BUG (probe, not fixed):", bug)


if __name__ == "__main__":
    main()
