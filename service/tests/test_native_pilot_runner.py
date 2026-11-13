"""Synthetic assembled runner boundary: real ledger/guard/OFClient, fake HTTP only.

Run as a dedicated process. No session, live database, dotenv file, model, or
network is accessed. Every source/receipt is a neutral temporary fixture.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

CHECKS = 0


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
        raise AssertionError("missing refusal " + reason)


async def async_refuses(reason, fn):
    from native_pilot.ledger import Refused
    try:
        await fn()
    except Refused as exc:
        check(exc.reason == reason, (reason, exc.reason))
    else:
        raise AssertionError("missing refusal " + reason)


class FakeResponse:
    def __init__(self, data, status=200, headers=None):
        self.status_code = status
        self.ok = status < 400
        self.headers = headers or {}
        self.url = "https://onlyfans.invalid/api2/v2/x"
        self.text = json.dumps(data)
        self.data = data

    def json(self):
        return copy.deepcopy(self.data)


class FakeHTTP:
    def __init__(self, history, account, fan, at):
        self.history = history
        self.account, self.fan, self.at = account, fan, at
        self.posts, self.gets, self.post_kwargs = [], [], []
        self.fail = None
        self.refuse = None      # (status, headers): the provider answers the POST with that HTTP error
        self.change_result = None
        self.closed = False

    def get(self, url, **kwargs):
        if urlsplit(url).path == f"/api2/v2/chats/{self.fan}":
            return FakeResponse({"withUser":{"id":self.fan},"unreadMessagesCount":0,"hasUnreadMessages":False})
        assert urlsplit(url).path == f"/api2/v2/chats/{self.fan}/messages"
        self.gets.append(url)
        return FakeResponse({"list": copy.deepcopy(self.history), "hasMore": False})

    def request(self, method, url, json=None, **kwargs):
        assert method == "POST" and urlsplit(url).path == f"/api2/v2/chats/{self.fan}/messages"
        self.posts.append(copy.deepcopy(json))
        self.post_kwargs.append(kwargs)
        if self.fail:
            raise self.fail
        if self.refuse:
            return FakeResponse({"error": "refused"}, status=self.refuse[0], headers=self.refuse[1])
        result = {"id": 1000 + len(self.posts), "text": json["text"], "isFromMe": True,
                  "fromUser": {"id": int(self.account)}, "withUser": {"id": self.fan},
                  "createdAt": self.at, "price": 0, "media": []}
        if self.change_result:
            self.change_result(result)
        self.history.insert(0, copy.deepcopy(result))
        return FakeResponse(result)

    def close(self):
        self.closed = True


async def suite(root):
    # Import-time .env access is explicitly disabled before any app import.
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    dotenv.dotenv_values = lambda *args, **kwargs: {"OPENROUTER_API_KEY": "synthetic-unused-key"}
    import test_native_pilot_ledger as fixtures
    os.environ["DATABASE_URL"] = fixtures.database_url(root / "bootstrap.db")
    os.environ["TWIN_BRIDGE_ENABLED"] = "0"
    os.environ["TWIN_BRIDGE_SEND_ENABLED"] = "0"
    logging.disable(logging.CRITICAL)

    from native_pilot import runner
    from native_pilot.ledger import Refused
    from of_client import OFClient
    from twin_bridge import coordination
    from db.engine import engine

    fixtures.NOW = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=123456) - timedelta(seconds=20)
    current = fixtures.NOW + timedelta(seconds=10)
    runner.now = lambda: current
    events = []
    runner.emit = events.append

    def make(name, count=1, limit=10, provider=None):
        # `provider` is the OnlyFans-side id when the DB-side account is Twin-keyed.
        provider = provider or fixtures.ACCT
        f = fixtures.Fixture(root, name)
        f.args["limit"] = limit
        f.sql("CREATE TABLE accounts(id TEXT)")
        f.sql("INSERT INTO accounts VALUES(?)", (fixtures.ACCT,))
        f.sql("CREATE TABLE automation_rules(account_id TEXT,is_enabled INTEGER)")
        f.sql("CREATE TABLE scheduled_jobs(account_id TEXT,status TEXT,kind TEXT)")
        f.sql("CREATE TABLE account_ai_config(account_id TEXT,persona TEXT,chatter_config_json TEXT)")
        f.sql("CREATE TABLE content_offers(account_id TEXT,fan_id INTEGER,status TEXT,mode TEXT,item_id INTEGER)")
        f.sql("CREATE TABLE pending_offer(account_id TEXT,fan_id INTEGER)")
        f.sql("ALTER TABLE messages ADD COLUMN automation_kind TEXT")
        f.sql("CREATE TABLE automation_runs(id INTEGER PRIMARY KEY,account_id TEXT,kind TEXT,started_at TEXT,completed_at TEXT,status TEXT,stats_json TEXT,error_text TEXT)")
        os.environ["DATABASE_URL"] = fixtures.database_url(f.path)
        p = f.activate()
        for i in range(count):
            f.source(100 + i, body=f"neutral source {i}", created_at=(fixtures.NOW + timedelta(seconds=i + 1)).isoformat(sep=" "))
        snap = f.ledger.snapshot(**f.call)
        key = f.ledger.claim_input(**f.call, snapshot=snap)
        sources = runner.read_sources(f.path, fixtures.ACCT, fixtures.FAN, snap.member_ids)
        history = [{"id": mid, "text": row["body"], "createdAt": row["created_at"].replace(" ", "T") + "Z",
                    "isFromMe": False, "fromUser": {"id": fixtures.FAN}, "withUser": {"id": fixtures.FAN}}
                   for mid, row in sorted(sources.items(), reverse=True)]
        http = FakeHTTP(history, provider, fixtures.FAN, current.isoformat() + "Z")
        client = OFClient.__new__(OFClient)
        client.account_id, client.user_id, client.platform = provider, provider, "onlyfans"
        client.http, client.timeout_s, client.proxy_url, client.proxy_label = http, 1, None, None
        client._signed_headers = lambda url: {}
        gateway = runner.Gateway(f.ledger, p, client, asyncio.get_running_loop())
        gateway.input_key, gateway.sources = key, sources
        coordination.register_owner(fixtures.ACCT, fixtures.FAN, p.owner_token, current + timedelta(minutes=29))
        return f, p, gateway, client, http

    # Dashboard ownership is explicit, including same-timestamp creator history.
    from native_pilot.service import fan_hold_reason, ServiceGateway
    from types import SimpleNamespace

    def dashboard(name):
        f, p, g, c, h = make("dashboard-" + name)
        f.sql("CREATE TABLE of_conversation_state(account_id TEXT,fan_id INTEGER,mode TEXT,takeover_state TEXT)")
        f.sql("INSERT INTO of_conversation_state(account_id,fan_id,mode,takeover_state) VALUES(?,?,'automated','none')", (fixtures.ACCT, fixtures.FAN))
        g.dashboard_handover = True
        g.hold_check = lambda: fan_hold_reason(f.path, fixtures.ACCT, fixtures.FAN, True)
        return f, p, g, c, h

    for stamp_kind in ("same-second", "newer"):
        f, p, g, c, h = dashboard(stamp_kind)
        outgoing = dict(h.history[0], id=900, isFromMe=True, fromUser={"id": int(fixtures.ACCT)})
        if stamp_kind == "newer":
            outgoing["createdAt"] = current.isoformat() + "Z"
        h.history.insert(0, outgoing)
        await g._send("neutral reply")
        check(len(h.posts) == 1, "creator history cannot take over a dashboard-managed chat")
        check(f.ledger.status(p.pilot_id)["attempts"] == {"sent": 1})

    for mode, takeover, reason in (("human", "none", "fan-human"), ("paused", "none", "fan-paused"),
                                   ("automated", "requested", "fan-takeover")):
        f, p, g, c, h = dashboard(mode + takeover)
        f.sql("UPDATE of_conversation_state SET mode=?,takeover_state=?", (mode, takeover))
        await async_refuses(reason, lambda: g._send("must not send"))
        check(h.posts == [] and not f.ledger.status(p.pilot_id)["attempts"])

    f, p, g, c, h = dashboard("takeover-race")
    original_begin = f.ledger.begin_attempt
    def take_over_after_admission(**kwargs):
        attempt = original_begin(**kwargs)
        f.sql("UPDATE of_conversation_state SET mode='human'")
        return attempt
    with patch.object(f.ledger, "begin_attempt", take_over_after_admission):
        await async_refuses("fan-held-before-post", lambda: g._send("prepared before takeover"))
    check(h.posts == [] and f.ledger.status(p.pilot_id)["attempts"] == {"not_sent": 1})

    f, p, g, c, h = dashboard("resume")
    f.sql("UPDATE of_conversation_state SET mode='human'")
    check(g.hold_check() == "fan-human")
    f.sql("UPDATE of_conversation_state SET mode='automated',takeover_state='none'")
    await g._send("reply after resume")
    check(len(h.posts) == 1)

    for case in ("missing-state", "other-account", "other-fan"):
        f, p, g, c, h = dashboard(case)
        if case == "missing-state":
            f.sql("DELETE FROM of_conversation_state")
        elif case == "other-account":
            f.sql("UPDATE of_conversation_state SET account_id='foreign'")
        else:
            f.sql("UPDATE of_conversation_state SET fan_id=999")
        await async_refuses("fan-not-enabled", lambda: g._send("must not send"))
        check(h.posts == [])

    for case, mutation, reason in (
        ("new-input", lambda h: h.insert(0, dict(h[0], id=999)), "new-message-during-generation"),
        ("foreign-author", lambda h: h[0].update(fromUser={"id": 999}), "send-check-failed"),
        ("foreign-recipient", lambda h: h[0].update(withUser={"id": 999}), "history-recipient-mismatch"),
        ("changed-direction", lambda h: h[0].update(isFromMe=True, fromUser={"id": int(fixtures.ACCT)}), "source-direction-changed"),
    ):
        f, p, g, c, h = dashboard(case)
        mutation(h.history)
        await async_refuses(reason, lambda: g._send("must not send"))
        check(h.posts == [])

    f, p, g, c, h = dashboard("unknown-outcome")
    from curl_cffi.requests.exceptions import ProxyError
    h.fail = ProxyError("synthetic transport failure")
    await async_refuses("delivery-unknown", lambda: g._send("neutral reply"))
    await async_refuses("delivery-unknown", lambda: g._send("must not retry"))
    check(len(h.posts) == 1 and f.ledger.status(p.pilot_id)["attempts"] == {"delivery_unknown": 1})

    f, p, g, c, h = dashboard("wiring")
    svc = SimpleNamespace(ledger=f.ledger, client=c, path=f.path, db=fixtures.ACCT,
                          args=SimpleNamespace(fan=fixtures.FAN), strict_arg=(True,))
    check(ServiceGateway(svc, p).dashboard_handover is True)
    svc.strict_arg = ()
    check(ServiceGateway(svc, p).dashboard_handover is False, "bounded service keeps conservative history policy")

    anchor = dict(h.history[0], id=800, isFromMe=True, fromUser={"id": int(fixtures.ACCT)})
    anchors = {800: {"created_at": next(iter(g.sources.values()))["created_at"]}}
    later = dict(anchor, id=801)
    runner.check_followup_history([later, anchor], anchors, set(), account=fixtures.ACCT, fan=fixtures.FAN,
                                  dashboard_handover=True)
    refuses("new-message-during-generation", lambda: runner.check_followup_history(
        [h.history[0], anchor], anchors, set(), account=fixtures.ACCT, fan=fixtures.FAN, dashboard_handover=True))
    refuses("external-outbound", lambda: runner.check_followup_history(
        [later, anchor], anchors, set(), account=fixtures.ACCT, fan=fixtures.FAN))

    f, p, gateway, client, http = make("cap", count=2)
    check(runner.check_local(f.path, fixtures.ACCT, fixtures.FAN)["creator_profile"] == "native-default")
    for i in range(10):
        reply = await asyncio.to_thread(gateway.send, fixtures.FAN, "literal 3 < 5 & 7 > 2", {})
        check(reply["id"] == 1001 + i)
        check(http.posts[-1]["text"] == "<p>literal 3 &lt; 5 &amp; 7 &gt; 2</p>")
        check(http.posts[-1]["price"] == 0 and http.posts[-1]["mediaFiles"] == [])
        check(http.post_kwargs[-1].get("allow_redirects") is False, "a redirect must never re-POST a message")
        # Production native attribution happens AFTER the gateway returns.
        f.source(reply["id"], direction="out", body=reply["text"], created_at=current.isoformat(sep=" "))
        f.sql("UPDATE messages SET automation_kind='ai_chatter' WHERE message_id=?", (reply["id"],))
        runner.verify_attribution(f.path, fixtures.ACCT, fixtures.FAN, f.ledger.receipts(pilot_id=p.pilot_id, input_key=gateway.input_key))
        check(f.sql("SELECT count(*) FROM twin_bridge_writes")[0][0] == 0)
    await async_refuses("send-limit-reached", lambda: gateway._send("eleventh"))
    check(len(http.posts) == 10 and all(f"/{fixtures.FAN}/" in url for url in http.gets))
    check(f.sql("SELECT send_budget_used,send_budget_limit FROM twin_bridge_conversations")[0] == (13, 13))
    check(f.sql("SELECT count(*) FROM twin_bridge_send_attempts WHERE turn_id='previous-twin-turn' AND state='sent'")[0][0] == 1)
    f.ledger.stop(**f.call, reason="send-limit-reached")
    check(f.ledger.status(p.pilot_id)["attempts"] == {"sent": 10})
    check(f.sql("SELECT state,reserved,send_budget_used,send_budget_limit FROM twin_bridge_conversations")[0] == ("paused", 1, 13, 13))

    # New/changed sources and wrong identity are refused before a POST/spend.
    mutations = [
        ("new-input", lambda h: h.insert(0, dict(h[0], id=999, createdAt=current.isoformat() + "Z")), "new-message-during-generation"),
        ("source-edit", lambda h: h[-1].update(text="changed"), "source-text-changed"),
        ("source-delete", lambda h: h.pop(), "send-check-failed"),
        ("source-unsend", lambda h: h[-1].update(isUnsent=True), "source-invalidated"),
        ("source-media", lambda h: h[-1].update(media=[{"id": 7}]), "source-not-plain-text"),
        ("recipient", lambda h: h[-1].update(withUser={"id": 999}), "history-recipient-mismatch"),
        ("author", lambda h: h[-1].update(fromUser={"id": 999}), "send-check-failed"),
        ("external", lambda h: h.insert(0, dict(h[0], id=999, isFromMe=True, fromUser={"id": int(fixtures.ACCT)}, createdAt=current.isoformat() + "Z")), "external-outbound"),
    ]
    for name, mutation, reason in mutations:
        f, p, g, c, h = make(name, count=2)
        mutation(h.history)
        await async_refuses(reason, lambda: g._send("neutral reply"))
        check(h.posts == [] and f.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0] == 3)

    f, p, g, c, h = make("wrong-send")
    for fan, text, kwargs in ((fixtures.FAN + 1, "reply", {}), (str(fixtures.FAN), "reply", {}),
                               (fixtures.FAN, "reply", {"price": 1}), (fixtures.FAN, "", {})):
        refuses("send-scope-refused", lambda: g.send(fan, text, kwargs))
    check(h.posts == [])

    # An ambiguous POST consumes one slot, stops, keeps evidence, and never retries.
    f, p, g, c, h = make("unknown")
    from curl_cffi.requests.exceptions import ProxyError
    h.fail = ProxyError("synthetic transport failure")
    await async_refuses("delivery-unknown", lambda: g._send("neutral reply"))
    check(len(h.posts) == 1)
    check(f.ledger.status(p.pilot_id)["attempts"] == {"delivery_unknown": 1})
    check(f.sql("SELECT state,reserved,send_budget_used,send_budget_limit FROM twin_bridge_conversations")[0] == ("attention", 1, 4, 4))
    await async_refuses("delivery-unknown", lambda: g._send("must not retry"))
    check(len(h.posts) == 1)

    # A provider 429 on the POST is pacing evidence only (D429/NP429): OnlyFans can create the message and still
    # answer 429, so the attempt is delivery-unknown (unit kept, fan held, never resent); the 429 is kept for the
    # account cooldown. Any other 4xx but 408 created nothing: CERTAINLY not sent, it spends no budget and stops
    # nothing; timeouts, 408 and 5xx stay ambiguous (delivery unknown, stop, never resent).
    f, p, g, c, h = make("rate-limited")
    h.refuse = (429, {"retry-after": "7"})
    await async_refuses("delivery-unknown", lambda: g._send("neutral reply"))
    check(len(h.posts) == 1 and g.not_sent is None and g.throttled.retry_after == 7.0)
    check(f.ledger.status(p.pilot_id)["attempts"] == {"delivery_unknown": 1} and f.ledger.status(p.pilot_id)["state"] == "stopped")
    check(f.sql("SELECT state,reserved,send_budget_used,send_budget_limit FROM twin_bridge_conversations")[0][:3] == ("attention", 1, 4))
    await async_refuses("delivery-unknown", lambda: g._send("must not retry"))
    check(len(h.posts) == 1, "a 429 after the POST started is never resent")
    # A PRE-wire refusal is certainly not sent: no budget, nothing stopped, latched; a fresh gateway answers the input.
    f, p, g, c, h = make("pre-wire-refused")
    g._dispatch_hold = lambda terms, attempt: True
    await async_refuses("fan-held-before-post", lambda: g._send("neutral reply"))
    check(h.posts == [] and g.not_sent.retry is True and f.ledger.status(p.pilot_id)["attempts"] == {"not_sent": 1})
    check(f.sql("SELECT state,reserved,send_budget_used FROM twin_bridge_conversations")[0][:3] == ("active", 1, 3))
    check(f.sql("SELECT state,reason FROM twin_bridge_send_attempts WHERE turn_id<>'previous-twin-turn'")[0] == ("refused", "native-pilot-not-sent"))
    check(f.sql("SELECT count(*) FROM twin_bridge_writes")[0][0] == 0, "no writer marker is left behind")
    check(runner.check_local(f.path, fixtures.ACCT, fixtures.FAN)["account_id"] == fixtures.ACCT, "a refused attempt does not block the fan")
    await async_refuses("fan-held-before-post", lambda: g._send("latched"))
    g2 = runner.Gateway(f.ledger, p, c, asyncio.get_running_loop())
    g2.input_key, g2.sources, g2.bubble = g.input_key, g.sources, 1
    reply = await g2._send("neutral reply")
    check(len(h.posts) == 1 and f.ledger.status(p.pilot_id)["attempts"] == {"not_sent": 1, "sent": 1})
    f.source(reply["id"], direction="out", body=reply["text"], created_at=current.isoformat(sep=" "))
    f.sql("UPDATE messages SET automation_kind='ai_chatter' WHERE message_id=?", (reply["id"],))
    runner.verify_attribution(f.path, fixtures.ACCT, fixtures.FAN, f.ledger.receipts(pilot_id=p.pilot_id, input_key=g.input_key))
    check(f.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0] == 4, "only the sent bubble is spent")

    for status in (400, 403, 404, 422):
        f, p, g, c, h = make("refused-%d" % status)
        h.refuse = (status, {})
        await async_refuses("provider-refused", lambda: g._send("neutral reply"))
        check(len(h.posts) == 1 and g.not_sent.retry is False, status)
        check(f.ledger.status(p.pilot_id)["attempts"] == {"not_sent": 1} and f.ledger.status(p.pilot_id)["state"] == "active", status)
    for status in (408, 500, 502, 503, 504):
        f, p, g, c, h = make("ambiguous-%d" % status)
        h.refuse = (status, {})
        await async_refuses("delivery-unknown", lambda: g._send("neutral reply"))
        check(len(h.posts) == 1 and g.not_sent is None, status)
        check(f.ledger.status(p.pilot_id)["attempts"] == {"delivery_unknown": 1} and f.ledger.status(p.pilot_id)["state"] == "stopped", status)
        check(f.sql("SELECT state FROM twin_bridge_conversations")[0][0] == "attention", status)

    # A card POST (price, media) answered 429 is just as unknown.
    # (covered with the content fixtures below: see "content-rate-limited")

    # The ledger will never turn an attempt that may have been sent into "not sent".
    f, p, g, c, h = make("not-sent-guard")
    await g._send("neutral reply")
    attempt_id = f.ledger.receipts(pilot_id=p.pilot_id)[0]["attempt_id"]
    refuses("attempt-not-dispatching", lambda: f.ledger.record_not_sent(**f.call, attempt_id=attempt_id))
    check(f.ledger.status(p.pilot_id)["attempts"] == {"sent": 1})
    f, p, g, c, h = make("not-sent-guard-unknown")
    h.fail = ProxyError("synthetic transport failure")
    await async_refuses("delivery-unknown", lambda: g._send("neutral reply"))
    attempt_id = f.ledger.receipts(pilot_id=p.pilot_id)[0]["attempt_id"]
    refuses("attempt-not-dispatching", lambda: f.ledger.record_not_sent(**f.call, attempt_id=attempt_id))
    check(f.ledger.status(p.pilot_id)["attempts"] == {"delivery_unknown": 1})

    f, p, g, c, h = make("bad-receipt")
    h.change_result = lambda r: r.update(text="different provider content")
    await async_refuses("delivery-unknown", lambda: g._send("neutral reply"))
    check(len(h.posts) == 1 and f.ledger.status(p.pilot_id)["attempts"] == {"delivery_unknown": 1})

    f, p, g, c, h = make("emit-failure")
    def broken_event(event):
        raise BrokenPipeError("synthetic output closed")
    with patch.object(runner, "emit", broken_event):
        await async_refuses("send-check-failed", lambda: g._send("neutral reply"))
    check(f.ledger.status(p.pilot_id)["attempts"] == {"sent": 1}, "settled receipt must survive logging failure")
    check(f.sql("SELECT state FROM twin_bridge_send_attempts WHERE turn_id<>'previous-twin-turn'")[0][0] == "sent")

    # A source change or operator stop after durable admission but before the
    # thread reaches the wire must still prevent the physical POST.
    for case in ("source-race", "stop-race"):
        f, p, g, c, h = make(case)
        original_begin = f.ledger.begin_attempt
        def racing_begin(**kwargs):
            admitted = original_begin(**kwargs)
            if case == "source-race":
                f.sql("UPDATE messages SET body='changed before wire' WHERE direction='in'")
            else:
                f.ledger.stop(**f.call, reason="operator-stop")
            return admitted
        with patch.object(f.ledger, "begin_attempt", racing_begin):
            # Nothing reached the wire: the attempt is CERTAINLY not sent (never "delivery unknown").
            await async_refuses("source-changed" if case == "source-race" else "authority-lost-before-post",
                                lambda: g._send("neutral reply"))
        check(h.posts == [], "last-instant validation must precede physical POST")
        check(f.ledger.status(p.pilot_id)["state"] == ("active" if case == "source-race" else "stopped"))
        check(f.ledger.status(p.pilot_id)["attempts"] == {"not_sent": 1}, case)
        check(f.sql("SELECT state,reason FROM twin_bridge_send_attempts WHERE turn_id<>'previous-twin-turn'")[0] == ("refused", "native-pilot-not-sent"))
        check(g.not_sent is not None and g.not_sent.retry is True)
        # the budget unit is given back while the owner still holds the conversation (a stop already closed it)
        check(f.sql("SELECT send_budget_used FROM twin_bridge_conversations")[0][0] == (3 if case == "source-race" else 4), case)
        check(f.sql("SELECT state FROM twin_bridge_conversations")[0][0] == ("active" if case == "source-race" else "paused"), case)

    # A prior pilot/input's receipt is not permission to ignore a newer
    # unrelated outbound in the CURRENT input's provider history.
    f, p, g, c, h = make("unproven-own-id")
    h.history.insert(0, {"id": 1001, "text": "unproven outbound", "createdAt": current.isoformat() + "Z",
                         "isFromMe": True, "fromUser": {"id": int(fixtures.ACCT)}, "withUser": {"id": fixtures.FAN}})
    await async_refuses("external-outbound", lambda: g._send("neutral reply"))
    check(h.posts == [])

    # Local gates remain effective between own bubbles and for new obligations.
    f, p, g, c, h = make("obligation")
    f.sql("INSERT INTO pending_offer VALUES(?,?)", (fixtures.ACCT, fixtures.FAN))
    await async_refuses("chat-offer-open", lambda: g._send("neutral reply"))
    check(h.posts == [])

    f, p, g, c, h = make("attribution")
    reply = await g._send("neutral reply")
    refuses("native-attribution-missing", lambda: runner.verify_attribution(f.path, fixtures.ACCT, fixtures.FAN, f.ledger.receipts(pilot_id=p.pilot_id)))
    check(len(h.posts) == 1)
    rid = runner.begin_native_run(f.path, fixtures.ACCT)
    check(rid > 0)
    runner.finish_native_run(f.path, rid, {"sent": 1, "private_text": "must not persist"})
    audit = f.sql("SELECT status,stats_json,error_text FROM automation_runs WHERE id=?", (rid,))[0]
    check(audit == ("done", '{"sent": 1}', None))

    # Profile-only jobs cannot send. Sender/unknown jobs still block the POST.
    for job_kind in ("gen_info", "ai_chatter", "unknown", None):
        f, p, g, c, h = make("background-job-" + str(job_kind))
        f.sql("INSERT INTO scheduled_jobs VALUES(?,?,?)", (fixtures.ACCT, "running", job_kind))
        if job_kind == "gen_info":
            await g._send("neutral reply")
            check(len(h.posts) == 1)
        else:
            await async_refuses("account-jobs-open", lambda: g._send("neutral reply"))
            check(h.posts == [])

    from native_pilot.content import ContentPlan
    MEDIA = [{"id": 7101, "type": "photo"}, {"id": 7102, "type": "video"}]
    TYPES = {m["id"]: m["type"] for m in MEDIA}
    for case in ("content-paid", "content-unknown", "content-bad-receipt", "content-bad-previews", "content-rate-limited"):
        f, p, g, c, h = make(case)
        f.sql("CREATE TABLE catalog_items(id INTEGER,account_id TEXT,kind TEXT,media_ids TEXT,preview_media_ids TEXT,price_cents INTEGER,is_free_teaser INTEGER,enabled INTEGER,pricing_mode TEXT)")
        f.sql("INSERT INTO catalog_items VALUES(1,?,'video','[7101, 7102]','[7101]',300,0,1,'fixed')", (fixtures.ACCT,))
        manifest=root/(case+".json")
        manifest.write_text(json.dumps({"account_id":fixtures.ACCT,"fan_id":fixtures.FAN,"items":[
          {"catalog_id":1,"kind":"video","price_cents":300,"media":MEDIA,"preview_ids":[7101]}]}))
        plan=ContentPlan(manifest,fixtures.ACCT,fixtures.FAN);g.content=plan
        c.vault_media_by_id=lambda mid:{"id":mid,"type":TYPES[mid],"canView":True}
        def media_receipt(result):
            result.update(price=h.posts[-1]["price"],media=[dict(m) for m in MEDIA],mediaCount=2,previews=[7101])
            if case=="content-bad-receipt":result["price"]=10
            if case=="content-bad-previews":result["previews"]=[7102]
        h.change_result=media_receipt
        # exactly the shape FASTT's catalogue path produced in the real-model replay
        terms=plan.terms({"price":3.0,"locked_text":False,"media_files":[7101,7102],"previews":[7101]})
        if case=="content-unknown":
            h.fail=ProxyError("synthetic uncertainty")
        if case=="content-rate-limited":
            h.refuse=(429,{})
            await async_refuses("delivery-unknown",lambda:g._send("neutral caption",terms))
            check(len(h.posts)==1 and g.not_sent is None and g.throttled is not None)
            check(f.ledger.status(p.pilot_id)["attempts"]=={"delivery_unknown":1} and f.ledger.status(p.pilot_id)["state"]=="stopped")
            continue
        if case in ("content-unknown","content-bad-receipt","content-bad-previews"):
            await async_refuses("delivery-unknown",lambda:g._send("neutral caption",terms))
            await async_refuses("delivery-unknown",lambda:g._send("no retry",terms))
            check(len(h.posts)==1)
            check(f.ledger.status(p.pilot_id)["attempts"]=={"delivery_unknown":1})
            if case=="content-unknown":
                saved=json.loads(f.sql("SELECT evidence_json FROM native_pilot_receipts WHERE pilot_id=?",(p.pilot_id,))[0][0])
                check(saved["content_intent"]["price_cents"]==300 and saved["content_intent"]["preview_ids"]==[7101])
        else:
            reply=await g._send("neutral caption",terms)
            check(len(h.posts)==1 and h.posts[0]["mediaFiles"]==[7101,7102])
            check(h.posts[0]["price"]==3.0 and h.posts[0]["previews"]==[7101])
            check(h.posts[0]["lockedText"] is False and h.posts[0]["text"]=="<p>neutral caption</p>")
            check(h.post_kwargs[0].get("allow_redirects") is False, "card POST must not follow redirects")
            check(f.ledger.status(p.pilot_id)["attempts"]=={"sent":1})
            check(f.sql("SELECT count(*) FROM twin_bridge_writes")[0][0]==0)
            f.source(reply["id"],direction="out",body=reply["text"],created_at=current.isoformat(sep=" "))
            f.sql("UPDATE messages SET automation_kind='ai_upseller',price_cents=300 WHERE message_id=?",(reply["id"],))
            runner.verify_attribution(f.path,fixtures.ACCT,fixtures.FAN,f.ledger.receipts(pilot_id=p.pilot_id),content=plan)

    # Several approved items: a send of the second listed item goes out, a stale open PPV of
    # any listed item is not an obligation, an unlisted item's open PPV still is, and a card
    # carrying no listed item's media set never reaches the wire.
    MEDIA2 = [{"id": 7201, "type": "photo"}]
    TYPES.update({7201: "photo"})
    for case in ("multi-second", "multi-open-listed", "multi-open-unlisted", "multi-unlisted-card"):
        f, p, g, c, h = make(case)
        f.sql("CREATE TABLE catalog_items(id INTEGER,account_id TEXT,kind TEXT,media_ids TEXT,preview_media_ids TEXT,price_cents INTEGER,is_free_teaser INTEGER,enabled INTEGER,pricing_mode TEXT)")
        f.sql("INSERT INTO catalog_items VALUES(1,?,'video','[7101, 7102]','[7101]',300,0,1,'fixed')", (fixtures.ACCT,))
        f.sql("INSERT INTO catalog_items VALUES(3,?,'photo','[7201]','[]',300,0,1,'fixed')", (fixtures.ACCT,))
        manifest=root/(case+".json")
        manifest.write_text(json.dumps({"account_id":fixtures.ACCT,"fan_id":fixtures.FAN,"items":[
          {"catalog_id":1,"kind":"video","price_cents":300,"media":MEDIA,"preview_ids":[7101]},
          {"catalog_id":3,"kind":"photo","price_cents":300,"media":MEDIA2,"preview_ids":[]}]}))
        plan=ContentPlan(manifest,fixtures.ACCT,fixtures.FAN);g.content=plan
        c.vault_media_by_id=lambda mid:{"id":mid,"type":TYPES[mid],"canView":True}
        h.change_result=lambda result:result.update(price=h.posts[-1]["price"],media=[dict(m) for m in MEDIA2],mediaCount=1,previews=[])
        if case=="multi-open-listed":
            for item in (1, 3):
                f.sql("INSERT INTO content_offers VALUES(?,?,'open','ppv',?)", (fixtures.ACCT,fixtures.FAN,item))
        if case=="multi-open-unlisted":
            # A real (disabled, so not sellable) unlisted catalogue row: the offer's item must exist.
            f.sql("INSERT INTO catalog_items VALUES(9,?,'photo','[7301]','[]',300,0,0,'fixed')", (fixtures.ACCT,))
            f.sql("INSERT INTO content_offers VALUES(?,?,'open','ppv',9)", (fixtures.ACCT,fixtures.FAN))
        if case=="multi-unlisted-card":
            refuses_any = False
            try:
                g.send(fixtures.FAN,"caption",{"price":3.0,"locked_text":False,"media_files":[7101,7201],"previews":[]})
            except Refused:
                refuses_any = True
            check(refuses_any and h.posts==[], case)
            continue
        terms=plan.terms({"price":3.0,"locked_text":False,"media_files":[7201],"previews":[]})
        check(terms["catalog_id"]==3, case)
        if case=="multi-open-unlisted":
            await async_refuses("chat-offer-open",lambda:g._send("neutral caption",terms))
            check(h.posts==[], case)
        else:
            await g._send("neutral caption",terms)
            check(len(h.posts)==1 and h.posts[0]["mediaFiles"]==[7201] and h.posts[0]["price"]==3.0, case)

    # A card whose media cannot be viewed, or whose catalogue row drifted or is
    # disabled, never reaches the wire.
    for case, mutate, reason in (("asset-gone", lambda c, f: setattr(c, "vault_media_by_id", lambda mid: {"id": mid, "type": TYPES[mid], "canView": False}), "content-asset-unavailable"),
                                 ("asset-type", lambda c, f: setattr(c, "vault_media_by_id", lambda mid: {"id": mid, "type": "video", "canView": True}), "content-asset-unavailable"),
                                 ("row-disabled", lambda c, f: f.sql("UPDATE catalog_items SET enabled=0"), "content-item-disabled"),
                                 ("row-drift", lambda c, f: f.sql("UPDATE catalog_items SET preview_media_ids='[]'"), "content-catalog-drift")):
        f, p, g, c, h = make("content-guard-" + case)
        f.sql("CREATE TABLE catalog_items(id INTEGER,account_id TEXT,kind TEXT,media_ids TEXT,preview_media_ids TEXT,price_cents INTEGER,is_free_teaser INTEGER,enabled INTEGER,pricing_mode TEXT)")
        f.sql("INSERT INTO catalog_items VALUES(1,?,'video','[7101, 7102]','[7101]',300,0,1,'fixed')", (fixtures.ACCT,))
        manifest=root/(case+".json")
        manifest.write_text(json.dumps({"account_id":fixtures.ACCT,"fan_id":fixtures.FAN,"items":[
          {"catalog_id":1,"kind":"video","price_cents":300,"media":MEDIA,"preview_ids":[7101]}]}))
        plan=ContentPlan(manifest,fixtures.ACCT,fixtures.FAN);g.content=plan
        c.vault_media_by_id=lambda mid:{"id":mid,"type":TYPES[mid],"canView":True}
        mutate(c, f)
        terms=plan.terms({"price":3.0,"locked_text":False,"media_files":[7101,7102],"previews":[7101]})
        await async_refuses(reason,lambda:g._send("neutral caption",terms))
        check(h.posts==[], case)

    # The gateway refuses any card that is not the reviewed item before the wire.
    f, p, g, c, h = make("content-wrong-card")
    plan=ContentPlan(root/"content-paid.json",fixtures.ACCT,fixtures.FAN);g.content=plan
    for kwargs in ({"price":3.0,"media_files":[7101]},{"price":3.0,"media_files":[7101,7102]},
                   {"price":4.0,"locked_text":False,"media_files":[7101,7102],"previews":[7101]}):
        refuses_any = False
        try:
            g.send(fixtures.FAN,"caption",kwargs)
        except Refused:
            refuses_any = True
        check(refuses_any and h.posts==[], kwargs)

    # Twin-keyed account: DB side `of-<owner>`, OnlyFans side the bare owner id.
    # One scope object carries both; nothing in live data is renamed.
    from native_pilot.scope import AccountScope
    saved_acct = fixtures.ACCT
    fixtures.ACCT = "of-" + saved_acct
    try:
        scope = AccountScope.parse(fixtures.ACCT)
        check(scope.db_id == "of-" + saved_acct and scope.provider_id == saved_acct)
        f, p, g, c, h = make("twin-keyed", count=2, provider=saved_acct)
        check(g.scope == scope and g.reader.account_id == saved_acct)
        check(runner.check_local(f.path, fixtures.ACCT, fixtures.FAN)["account_id"] == fixtures.ACCT)
        reply = await g._send("neutral reply")
        check(reply["fromUser"]["id"] == int(saved_acct) and len(h.posts) == 1)
        check(f.ledger.status(p.pilot_id)["attempts"] == {"sent": 1})
        # An outbound message by that same creator that we did not send is still found
        # through the provider-side id and stops the send.
        f, p, g, c, h = make("twin-keyed-external", count=2, provider=saved_acct)
        h.history.insert(0, dict(h.history[0], id=999, isFromMe=True, fromUser={"id": int(saved_acct)}, createdAt=current.isoformat() + "Z"))
        await async_refuses("external-outbound", lambda: g._send("neutral reply"))
        check(h.posts == [])
        # Employee attribution: with the sentinel known, an AI row must carry exactly it.
        f, p, g, c, h = make("twin-keyed-employee", count=2, provider=saved_acct)
        f.sql("ALTER TABLE messages ADD COLUMN sent_by_employee_id INTEGER")
        reply = await g._send("neutral reply")
        f.source(reply["id"], direction="out", body=reply["text"], created_at=current.isoformat(sep=" "))
        f.sql("UPDATE messages SET automation_kind='ai_chatter' WHERE message_id=?", (reply["id"],))
        receipts = f.ledger.receipts(pilot_id=p.pilot_id, input_key=g.input_key)
        for employee, expect in ((None, "native-attribution-employee"), (9, "native-attribution-employee")):
            f.sql("UPDATE messages SET sent_by_employee_id=? WHERE message_id=?", (employee, reply["id"]))
            refuses(expect, lambda: runner.verify_attribution(f.path, fixtures.ACCT, fixtures.FAN, receipts, automation_employee_id=5))
        f.sql("UPDATE messages SET sent_by_employee_id=5 WHERE message_id=?", (reply["id"],))
        runner.verify_attribution(f.path, fixtures.ACCT, fixtures.FAN, receipts, automation_employee_id=5)
        # An AI row stamped with the HUMAN sentinel is refused too.
        f.sql("UPDATE messages SET sent_by_employee_id=6 WHERE message_id=?", (reply["id"],))
        refuses("native-attribution-employee", lambda: runner.verify_attribution(f.path, fixtures.ACCT, fixtures.FAN, receipts, automation_employee_id=5))
    finally:
        fixtures.ACCT = saved_acct

    # Default preflight executes the assembled run() path with the verified
    # connection (patched here: no key, no DB row) but never activates a ledger,
    # constructs the model transport, or sends anything.
    from native_pilot import attribution, connection as conn_mod
    from native_pilot.attribution import Sentinels
    f, p, g, c, h = make("preflight")
    c.me = lambda: {"id": int(fixtures.ACCT), "username": "synthetic-creator"}
    fake_conn = conn_mod.PilotConnection(scope=AccountScope.parse(fixtures.ACCT), tenant_id="synthetic-tenant", revision=3,
        owner_id=int(fixtures.ACCT), expires_at=datetime.now(timezone.utc) + timedelta(hours=1), capture={}, x_hash="x",
        proxy_url=None, model_id="openrouter/deepseek-v4.1-flash", model_api_key="synthetic-key", daily_cost_cap_cents=100)
    async def fake_load(scope):
        check(scope == AccountScope.parse(fixtures.ACCT))
        return fake_conn
    async def fake_read():
        return Sentinels(automation=None, human=None)
    args = argparse.Namespace(account=fixtures.ACCT, fan=fixtures.FAN, handle="synthetic-creator",
                              pilot_id="never-activated", send_limit=10, max_seconds=60, enable_send=False)
    events.clear()
    with patch.object(conn_mod, "load_verified_connection", fake_load), patch.object(conn_mod, "build_client", return_value=c), \
            patch.object(attribution, "read_sentinels", fake_read), \
            patch.object(fixtures.Ledger, "activate", side_effect=AssertionError("activation forbidden")):
        await runner.run(args)
    check(h.posts == [] and h.closed)
    check(not f.sql("SELECT 1 FROM native_pilots WHERE pilot_id='never-activated'"))
    ok = [e for e in events if e.get("event") == "preflight-ok"][0]
    check(ok["model_id"] == "openrouter/deepseek-v4.1-flash" and ok["proxy"] == "none"
          and ok["attribution"] == {"automation": False, "human": False} and "synthetic-key" not in json.dumps(events))
    # Send mode refuses (before any ledger activation) until the attribution rows exist.
    send_args = argparse.Namespace(**{**vars(args), "enable_send": True})
    async def fake_require():
        from native_pilot.ledger import Refused
        raise Refused("attribution-sentinels-missing")
    with patch.object(conn_mod, "load_verified_connection", fake_load), patch.object(conn_mod, "build_client", return_value=c), \
            patch.object(attribution, "read_sentinels", fake_read), patch.object(attribution, "require_sentinels", fake_require), \
            patch.object(fixtures.Ledger, "activate", side_effect=AssertionError("activation forbidden")):
        await async_refuses("attribution-sentinels-missing", lambda: runner.run(send_args))
    check(h.posts == [])
    await engine.dispose()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="native-pilot-runner-") as temporary:
        asyncio.run(suite(Path(temporary)))
    print(f"all cases passed ({CHECKS} checks)")

