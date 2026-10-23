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
    def __init__(self, data):
        self.status_code = 200
        self.ok = True
        self.headers = {}
        self.text = json.dumps(data)
        self.data = data

    def json(self):
        return copy.deepcopy(self.data)


class FakeHTTP:
    def __init__(self, history, account, fan, at):
        self.history = history
        self.account, self.fan, self.at = account, fan, at
        self.posts, self.gets = [], []
        self.fail = None
        self.change_result = None
        self.closed = False

    def get(self, url, **kwargs):
        assert urlsplit(url).path == f"/api2/v2/chats/{self.fan}/messages"
        self.gets.append(url)
        return FakeResponse({"list": copy.deepcopy(self.history), "hasMore": False})

    def request(self, method, url, json=None, **kwargs):
        assert method == "POST" and urlsplit(url).path == f"/api2/v2/chats/{self.fan}/messages"
        self.posts.append(copy.deepcopy(json))
        if self.fail:
            raise self.fail
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
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + (root / "bootstrap.db").as_posix()
    os.environ["TWIN_BRIDGE_ENABLED"] = "0"
    os.environ["TWIN_BRIDGE_SEND_ENABLED"] = "0"
    logging.disable(logging.CRITICAL)

    import test_native_pilot_ledger as fixtures
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

    def make(name, count=1, limit=10):
        f = fixtures.Fixture(root, name)
        f.args["limit"] = limit
        f.sql("CREATE TABLE accounts(id TEXT)")
        f.sql("INSERT INTO accounts VALUES(?)", (fixtures.ACCT,))
        f.sql("CREATE TABLE automation_rules(account_id TEXT,is_enabled INTEGER)")
        f.sql("CREATE TABLE scheduled_jobs(account_id TEXT,status TEXT,kind TEXT)")
        f.sql("CREATE TABLE account_ai_config(account_id TEXT,persona TEXT,chatter_config_json TEXT)")
        f.sql("CREATE TABLE content_offers(account_id TEXT,fan_id INTEGER,status TEXT)")
        f.sql("CREATE TABLE pending_offer(account_id TEXT,fan_id INTEGER)")
        f.sql("ALTER TABLE messages ADD COLUMN automation_kind TEXT")
        f.sql("CREATE TABLE automation_runs(id INTEGER PRIMARY KEY,account_id TEXT,kind TEXT,started_at TEXT,completed_at TEXT,status TEXT,stats_json TEXT,error_text TEXT)")
        os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + f.path.as_posix()
        p = f.activate()
        for i in range(count):
            f.source(100 + i, body=f"neutral source {i}", created_at=(fixtures.NOW + timedelta(seconds=i + 1)).isoformat(sep=" "))
        snap = f.ledger.snapshot(**f.call)
        key = f.ledger.claim_input(**f.call, snapshot=snap)
        sources = runner.read_sources(f.path, fixtures.ACCT, fixtures.FAN, snap.member_ids)
        history = [{"id": mid, "text": row["body"], "createdAt": row["created_at"].replace(" ", "T") + "Z",
                    "isFromMe": False, "fromUser": {"id": fixtures.FAN}, "withUser": {"id": fixtures.FAN}}
                   for mid, row in sorted(sources.items(), reverse=True)]
        http = FakeHTTP(history, fixtures.ACCT, fixtures.FAN, current.isoformat() + "Z")
        client = OFClient.__new__(OFClient)
        client.account_id, client.user_id, client.platform = fixtures.ACCT, fixtures.ACCT, "onlyfans"
        client.http, client.timeout_s, client.proxy_url, client.proxy_label = http, 1, None, None
        client._signed_headers = lambda url: {}
        gateway = runner.Gateway(f.ledger, p, client, asyncio.get_running_loop())
        gateway.input_key, gateway.sources = key, sources
        coordination.register_owner(fixtures.ACCT, fixtures.FAN, p.owner_token, current + timedelta(minutes=29))
        return f, p, gateway, client, http

    f, p, gateway, client, http = make("cap", count=2)
    check(runner.check_local(f.path, fixtures.ACCT, fixtures.FAN)["creator_profile"] == "native-default")
    for i in range(10):
        reply = await asyncio.to_thread(gateway.send, fixtures.FAN, "literal <hello> & neutral", {})
        check(reply["id"] == 1001 + i)
        check(http.posts[-1]["text"] == "<p>literal &lt;hello&gt; &amp; neutral</p>")
        check(http.posts[-1]["price"] == 0 and http.posts[-1]["mediaFiles"] == [])
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
            await async_refuses("delivery-unknown", lambda: g._send("neutral reply"))
        check(h.posts == [], "last-instant validation must precede physical POST")
        check(f.ledger.status(p.pilot_id)["state"] == "stopped")

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
    for case in ("content-free", "content-paid", "content-unknown", "content-bad-receipt"):
        f, p, g, c, h = make(case)
        f.sql("CREATE TABLE catalog_items(id INTEGER,account_id TEXT,kind TEXT,media_ids TEXT,price_cents INTEGER,is_free_teaser INTEGER,enabled INTEGER)")
        f.sql("INSERT INTO catalog_items VALUES(1,?,'photo','[7101]',0,1,1)", (fixtures.ACCT,))
        f.sql("INSERT INTO catalog_items VALUES(2,?,'video','[7102]',300,0,1)", (fixtures.ACCT,))
        manifest=root/(case+".json")
        manifest.write_text(json.dumps({"account_id":fixtures.ACCT,"fan_id":fixtures.FAN,"items":[
          {"catalog_id":1,"media_id":7101,"kind":"photo","price_cents":0},
          {"catalog_id":2,"media_id":7102,"kind":"video","price_cents":300}]}))
        plan=ContentPlan(manifest,fixtures.ACCT,fixtures.FAN);g.content=plan
        selected=plan.free if case=="content-free" else plan.paid
        c.vault_media_by_id=lambda mid:{"id":mid,"type":selected["kind"],"canView":True}
        def media_receipt(result):
            result.update(price=h.posts[-1]["price"],media=[{"id":selected["media_id"],"type":selected["kind"]}],mediaCount=1)
            if case=="content-bad-receipt":result["price"]=10
        h.change_result=media_receipt
        terms=plan.terms({"media_files":[selected["media_id"]],"price":selected["price_cents"]/100})
        if case=="content-unknown":
            h.fail=ProxyError("synthetic uncertainty")
        if case in ("content-unknown","content-bad-receipt"):
            await async_refuses("delivery-unknown",lambda:g._send("neutral caption",terms))
            await async_refuses("delivery-unknown",lambda:g._send("no retry",terms))
            check(len(h.posts)==1)
            check(f.ledger.status(p.pilot_id)["attempts"]=={"delivery_unknown":1})
            if case=="content-unknown":
                saved=json.loads(f.sql("SELECT evidence_json FROM native_pilot_receipts WHERE pilot_id=?",(p.pilot_id,))[0][0])
                check(saved["content_intent"]["price_cents"]==300)
        else:
            reply=await g._send("neutral caption",terms)
            check(len(h.posts)==1 and h.posts[0]["mediaFiles"]==[selected["media_id"]])
            check(h.posts[0]["price"]==selected["price_cents"]/100)
            check(h.posts[0]["lockedText"] is False and h.posts[0]["previews"]==[])
            check(f.ledger.status(p.pilot_id)["attempts"]=={"sent":1})
            check(f.sql("SELECT count(*) FROM twin_bridge_writes")[0][0]==0)
            f.source(reply["id"],direction="out",body=reply["text"],created_at=current.isoformat(sep=" "))
            f.sql("UPDATE messages SET automation_kind='ai_upseller',price_cents=? WHERE message_id=?",(selected["price_cents"],reply["id"]))
            runner.verify_attribution(f.path,fixtures.ACCT,fixtures.FAN,f.ledger.receipts(pilot_id=p.pilot_id),content=plan)

    # Default preflight executes the assembled run() import path but never
    # activates a ledger, constructs the model transport, or sends anything.
    f, p, g, c, h = make("preflight")
    c.me = lambda: {"id": int(fixtures.ACCT), "username": "synthetic-creator"}
    args = argparse.Namespace(account=fixtures.ACCT, fan=fixtures.FAN, handle="synthetic-creator",
                              twin_env=root / "nonexistent.env", pilot_id="never-activated",
                              send_limit=10, max_seconds=60, enable_send=False)
    with patch.object(OFClient, "from_account", return_value=c), patch.object(fixtures.Ledger, "activate", side_effect=AssertionError("activation forbidden")):
        await runner.run(args)
    check(h.posts == [] and h.closed)
    check(not f.sql("SELECT 1 FROM native_pilots WHERE pilot_id='never-activated'"))
    await engine.dispose()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="native-pilot-runner-") as temporary:
        asyncio.run(suite(Path(temporary)))
    print(f"all cases passed ({CHECKS} checks)")
