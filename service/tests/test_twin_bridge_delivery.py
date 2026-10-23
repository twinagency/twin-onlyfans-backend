#!/usr/bin/env python3
"""Platform adapter + worker state machine - synthetic, no network, no model.

Windows:  set TEMP to a writable folder first, then
          .venv\\Scripts\\python.exe service\\tests\\test_twin_bridge_delivery.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import types
from datetime import datetime, timedelta
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SERVICE_DIR = TESTS_DIR.parent
sys.path.insert(0, str(SERVICE_DIR))

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="twin-bridge-delivery-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP_ROOT / 'delivery.db').as_posix()}"
for _var in ("TWIN_BRIDGE_ENABLED", "TWIN_BRIDGE_ACCOUNT_ID", "TWIN_BRIDGE_CREATOR_SLUG",
             "TWIN_BRIDGE_FAN_ALLOWLIST", "TWIN_BRIDGE_ACTIVATION_CUTOFF",
             "TWIN_BRIDGE_SEND_ENABLED", "TWIN_BRIDGE_SEND_LIMIT", "TWIN_BRIDGE_BASE_URL",
             "TWIN_BRIDGE_TOKEN"):
    os.environ.pop(_var, None)

_relay_cache = types.ModuleType("relay_cache")
_relay_cache.invalidate = lambda *a, **k: None
_relay_cache.invalidate_money_feeds = lambda *a, **k: None
sys.modules["relay_cache"] = _relay_cache

from sqlalchemy import select  # noqa: E402

from db.engine import engine, get_session, init_db  # noqa: E402
from db.models import (  # noqa: E402
    FanLease, Message, TwinBridgeConversation, TwinBridgeInput, TwinBridgeSendAttempt,
    TwinBridgeTurn, TwinBridgeTurnMember,
)
from twin_bridge import coordination, platform, turns, worker  # noqa: E402
from twin_bridge.client import (  # noqa: E402
    CATEGORY_UNCERTAIN, TwinClientError, TwinDecisionResult, TwinReplyResult,
)
from twin_bridge.scope import load_scope  # noqa: E402

ACCOUNT = "acct-synthetic-1"
CREATOR = "creator-synthetic-1"
FAN = 9001
BASE_TIME = datetime(2026, 10, 15, 4, 0, 0)

ENV = {
    "TWIN_BRIDGE_ENABLED": "1",
    "TWIN_BRIDGE_ACCOUNT_ID": ACCOUNT,
    "TWIN_BRIDGE_CREATOR_SLUG": CREATOR,
    "TWIN_BRIDGE_FAN_ALLOWLIST": str(FAN),
    "TWIN_BRIDGE_ACTIVATION_CUTOFF": "2026-10-15T00:00:00Z",
    "TWIN_BRIDGE_SEND_ENABLED": "1",
    "TWIN_BRIDGE_SEND_LIMIT": "5",
}

failures: list[str] = []


def check(label, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def check_true(label, got) -> None:
    check(label, bool(got), True)


sys.setrecursionlimit(50_000)
threading.stack_size(64 * 1024 * 1024)


def run(coro):
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def scope(**over):
    env = dict(ENV)
    env.update(over)
    return load_scope(env)


class FakeTwin:
    """A Twin client stand-in: records calls, scripts results."""

    def __init__(self, *, text="synthetic draft", status="completed", deliverable=True,
                 outcome="ok", ack_failures=0):
        self.drafts = 0
        self.status_reads = 0
        self.acks = 0
        self.cancels = 0
        self._text = text
        self._status = status
        self._deliverable = deliverable
        self._outcome = outcome
        self._ack_failures = ack_failures

    async def request_draft(self, event):
        self.drafts += 1
        return TwinReplyResult(
            request_id="ofm_stub", conversation_key="of_stub", status="completed",
            outcome=self._outcome, deliverable=self._deliverable, text=self._text,
            epoch=0, created_at="2026-10-15T04:00:01.000Z",
            completed_at="2026-10-15T04:00:02.000Z", delivered_at=None, committed_at=None,
            usage={},
        )

    async def read_status(self, event):
        self.status_reads += 1
        # Twin finishes the delivery decision's transcript commit itself, so a
        # status read AFTER a successful ack is what reports `delivered`.
        status = self._status
        if status == "completed" and self.acks > 0:
            status = "delivered"
        delivered = status == "delivered"
        return TwinReplyResult(
            request_id="ofm_stub", conversation_key="of_stub", status=status,
            outcome=self._outcome if status != "queued" else None,
            deliverable=self._deliverable if status == "completed" else False,
            text=self._text if (status == "completed" and self._deliverable) else "",
            epoch=0, created_at="2026-10-15T04:00:01.000Z",
            completed_at="2026-10-15T04:00:02.000Z",
            delivered_at="2026-10-15T04:00:03.000Z" if delivered else None,
            committed_at="2026-10-15T04:00:04.000Z" if delivered else None,
            usage={},
        )

    async def acknowledge_delivery(self, event):
        self.acks += 1
        if self._ack_failures > 0:
            self._ack_failures -= 1
            raise TwinClientError(CATEGORY_UNCERTAIN, "stub ack", uncertain=True)
        return TwinDecisionResult("ofm_stub", "of_stub", "delivered")

    async def cancel_request(self, event):
        self.cancels += 1
        return TwinDecisionResult("ofm_stub", "of_stub", "cancelled")


class FakeAdapter:
    """The platform adapter stand-in: counts POSTs, scripts outcomes."""

    def __init__(self, *, history=None, history_error=None, send_error=None,
                 provider_id="777000111"):
        self.posts = []
        self.history_reads = 0
        self._history = history if history is not None else []
        self._history_error = history_error
        self._send_error = send_error
        self._provider_id = provider_id

    async def preflight_history(self, chat_id, *, since=None, source_ids=None):
        self.history_reads += 1
        self.history_scope = (since, source_ids)
        if self._history_error is not None:
            raise platform.PlatformError(self._history_error)
        return [{"createdAt": "2026-10-15T04:00:01Z", **row}
                if isinstance(row, dict) else row for row in self._history]

    async def send_text(self, chat_id, text, *, dispatch_token=None):
        self.posts.append((int(chat_id), text))
        if self._send_error is not None:
            raise self._send_error
        return platform.TextSendResult(
            provider_message_id=self._provider_id,
            evidence={"id": self._provider_id},
        )


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.url = "https://api.test/messages"
        self.headers = {}
        self._payload = payload
        self.text = json.dumps(payload or {})

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class FakeSession:
    """The LOWEST HTTP layer: counts every call it is asked to make."""

    def __init__(self, *, raise_proxy=False, status=200, payload=None):
        self.calls = []
        self._raise_proxy = raise_proxy
        self._status = status
        self._payload = payload if payload is not None else {
            "id": 4242, "responseType": "message", "isFromMe": True, "text": "<p>x</p>",
        }

    def request(self, method, url, json=None, headers=None, timeout=None, allow_redirects=True):  # noqa: A002
        self.calls.append((method, url, json))
        self.redirects = getattr(self, "redirects", []) + [allow_redirects]
        if self._raise_proxy:
            # `of_client` wraps curl_cffi's requests shim, so the retry
            # classifier matches THAT ProxyError class, not the stdlib one.
            from curl_cffi import requests as curl_curl
            raise curl_curl.exceptions.ProxyError("synthetic connect refusal")
        return FakeResponse(self._status, self._payload)


def raw_client(session):
    """A real OFClient with only its transport seams replaced."""
    from of_client import OFClient
    client = OFClient.__new__(OFClient)
    client.account_id = ACCOUNT
    client.http = session
    client.timeout_s = 5
    client.proxy_url = ""
    client.proxy_label = ""
    client.x_of_rev = "stub"
    client._build_url = lambda path, params=None: f"https://api.test/{str(path).lstrip('/')}"
    client._signed_headers = lambda url: {}
    return client


async def reset_all() -> None:
    async with get_session() as s:
        for model in (TwinBridgeSendAttempt, TwinBridgeTurnMember, TwinBridgeTurn,
                      TwinBridgeConversation, TwinBridgeInput, FanLease, Message):
            await s.execute(model.__table__.delete())
    coordination._OWNERS.clear()
    coordination._RESERVED.clear()
    coordination._ACQUIRING.clear()
    coordination._OWNER_EXPIRES.clear()
    coordination._IN_FLIGHT.clear()
    coordination._DISPATCH_AUTH.clear()
    # Clear any durable cross-process in-flight markers too, so a marker left by
    # one case can never leak into the next.
    con = coordination._durable_write_conn()
    if con is not None:
        try:
            con.execute("DELETE FROM twin_bridge_writes")
            con.commit()
        except Exception:  # noqa: BLE001
            pass
        finally:
            con.close()


async def reset_turn_work() -> None:
    async with get_session() as s:
        for model in (TwinBridgeTurnMember, TwinBridgeTurn, TwinBridgeInput):
            await s.execute(model.__table__.delete())
    coordination._OWNERS.clear()
    coordination._RESERVED.clear()
    coordination._ACQUIRING.clear()
    coordination._OWNER_EXPIRES.clear()
    coordination._IN_FLIGHT.clear()
    coordination._DISPATCH_AUTH.clear()


async def seed_turn(*, fan_id=FAN, message_id=5001, text="fan says hi",
                    received_at=None) -> None:
    received_at = received_at or BASE_TIME
    async with get_session() as s:
        # A matching CANONICAL inbound message: the dispatch gate re-validates
        # that the source is still a plain inbound DM before it posts.
        s.add(Message(
            account_id=ACCOUNT, fan_id=int(fan_id), message_id=int(message_id),
            direction="in", body=text, media_ids="[]", media_count=0, price_cents=0,
            is_paid=None, is_tip=False, is_unsent=False,
            created_at=received_at, ingested_at=received_at, raw_json="{}",
        ))
        s.add(TwinBridgeInput(
            account_id=ACCOUNT, fan_id=int(fan_id), provider_message_id=int(message_id),
            creator_slug=CREATOR, activation_cutoff=datetime(2026, 10, 15, 0, 0, 0),
            conversation_id=str(fan_id), normalized_text=text,
            event_json=json.dumps({"stub": True}),
            occurred_at=received_at, received_at=received_at, state="pending",
        ))


async def freeze(*, fan_id=FAN, message_id=5001, now=None) -> None:
    now = now or BASE_TIME + timedelta(seconds=9)
    await seed_turn(fan_id=fan_id, message_id=message_id)
    await turns.form_turns(now=now, scope=scope())
    async with get_session() as s:
        conversation = await coordination.load_conversation(s, ACCOUNT, fan_id)
        if conversation is None:
            await coordination.ensure_conversation(
                s, account_id=ACCOUNT, fan_id=fan_id, creator_slug=CREATOR,
                conversation_id=str(fan_id),
                scope_fingerprint=turns.scope_fingerprint(scope()),
                now=now, send_budget_limit=5,
            )


async def turns_for(fan_id=FAN):
    async with get_session() as s:
        return list((await s.execute(
            select(TwinBridgeTurn).where(
                TwinBridgeTurn.account_id == ACCOUNT, TwinBridgeTurn.fan_id == int(fan_id)
            )
        )).scalars())


async def conv_for(fan_id=FAN):
    async with get_session() as s:
        return await coordination.load_conversation(s, ACCOUNT, fan_id)


async def _draft_then_dispatch(twin, adapter, *, started=BASE_TIME + timedelta(seconds=9)):
    """One pass drafts, the next dispatches - the worker takes one step per pass."""
    await worker.run_once(now=started, scope=scope(), client=twin, adapter=adapter,
                          enable_send=True)
    await worker.run_once(now=started + timedelta(seconds=3), scope=scope(), client=twin,
                          adapter=adapter, enable_send=True)


async def attempts_for(fan_id=FAN):
    async with get_session() as s:
        return list((await s.execute(
            select(TwinBridgeSendAttempt).where(
                TwinBridgeSendAttempt.account_id == ACCOUNT,
                TwinBridgeSendAttempt.fan_id == int(fan_id),
            )
        )).scalars())


async def main() -> None:
    await init_db()
    await _lowest_layer()
    await _validation()
    await _worker_flow()
    await _uncertainty()
    await _crash_window()
    await _takeover()
    await _history()
    await _historical_outbound_boundary()
    await _ack_retry()
    await _budget_restart()
    await _bookkeeping_failure()
    await _queued_second_turn()
    await _competing_writers()


async def _lowest_layer() -> None:
    print("\nplatform: exactly one message POST at the lowest HTTP layer")
    session = FakeSession(raise_proxy=True)
    client = raw_client(session)
    raised = None
    try:
        client.send_text_once(FAN, "hello")
    except Exception as exc:  # noqa: BLE001
        raised = type(exc).__name__
    check("a proxy failure on the bridge path is not retried", len(session.calls), 1)
    check("and it still surfaces the transport error", raised, "ProxyError")

    session = FakeSession(raise_proxy=True)
    client = raw_client(session)
    try:
        client.send_message(FAN, "hello", auto_tag=False)
    except Exception:  # noqa: BLE001
        pass
    check("another caller keeps its pre-existing proxy retry", len(session.calls), 3)

    session = FakeSession(status=400, payload={"error": "nope"})
    client = raw_client(session)
    try:
        client.send_text_once(FAN, "hello")
    except Exception:  # noqa: BLE001
        pass
    check("a 400 does not trigger a co-tag re-POST", len(session.calls), 1)

    session = FakeSession()
    client = raw_client(session)
    created = client.send_text_once(FAN, "hello & welcome")
    check("a successful send is one POST", len(session.calls), 1)
    check("and it never follows a redirect (no hidden second POST)", session.redirects, [False])
    check("and it targets the chat messages route",
          session.calls[0][1].endswith(f"/chats/{FAN}/messages"), True)
    body = session.calls[0][2]
    check("price is zero", body["price"], 0)
    check("text is unlocked", body["lockedText"], False)
    check("no media rides along", body["mediaFiles"], [])
    check("no previews ride along", body["previews"], [])
    check("no tags are written", "userTags" not in body, True)
    check("no GIF is attached", "giphyId" not in body, True)
    check("no quote is attached", "replyToMessageId" not in body, True)
    check("the created message id survives", created["id"], 4242)


async def _validation() -> None:
    print("\nplatform: a response only proves delivery when it carries a real id")
    for label, payload in (
        ("an empty object is not delivery", {}),
        ("a success flag alone is not delivery", {"success": True}),
        ("a boolean id is not delivery", {"id": True}),
        ("a float id is not delivery", {"id": 1.5}),
        ("a fractional string id is not delivery", {"id": "12.5"}),
        ("a zero id is not delivery", {"id": 0}),
        ("another chat's message is not delivery", {"id": 5, "withUser": {"id": 1234}}),
    ):
        try:
            platform.validate_created_message(payload, chat_id=FAN)
        except platform.PlatformUnknown:
            check_true(label, True)
        else:
            check_true(label, False)
    ok = platform.validate_created_message({"id": "0007"}, chat_id=FAN)
    check("a decimal-string id is accepted and canonicalised", ok.provider_message_id, "7")
    check("a bool never passes as an id", platform.normalise_provider_id(True), None)

    print("\nplatform: the adapter binds to one account and one platform")
    mismatch = types.SimpleNamespace(account_id="someone-else", platform="onlyfans")
    try:
        await platform.OnlyFansTextAdapter(ACCOUNT, client=mismatch).client()
    except platform.PlatformError as exc:
        check("a mismatched client/account is refused", exc.reason, "client-account-mismatch")
    fansly = types.SimpleNamespace(account_id=ACCOUNT, platform="fansly")
    try:
        await platform.OnlyFansTextAdapter(ACCOUNT, client=fansly).client()
    except platform.PlatformError as exc:
        check("a non-OnlyFans platform is refused", exc.reason, "unsupported-platform")


async def _worker_flow() -> None:
    print("\nworker: draft, dispatch, ack")
    await reset_all()
    await freeze()
    twin = FakeTwin(text="neutral draft reply")
    adapter = FakeAdapter()
    await worker.run_once(now=BASE_TIME + timedelta(seconds=9), scope=scope(),
                          client=twin, adapter=adapter, enable_send=False)
    check("the frozen turn is submitted once", twin.drafts, 1)
    check("nothing is dispatched while sends are disabled", len(adapter.posts), 0)
    turn = (await turns_for())[0]
    check("the draft is persisted", turn.draft_text, "neutral draft reply")
    check("and the turn waits as draft_ready", turn.state, "draft_ready")
    check("the Twin request id is recorded", turn.request_id, "ofm_stub")

    await worker.run_once(now=BASE_TIME + timedelta(seconds=12), scope=scope(),
                          client=twin, adapter=adapter, enable_send=True)
    check("exactly one provider POST happens", len(adapter.posts), 1)
    check("carrying the fan id", adapter.posts[0][0], FAN)
    check("and the draft text verbatim", adapter.posts[0][1], "neutral draft reply")
    check("with no second model call", twin.drafts, 1)
    check("the ack is attempted once", twin.acks, 1)
    turn = (await turns_for())[0]
    check("the turn completes", turn.state, "delivered")
    check("with the provider id persisted", turn.provider_message_id, "777000111")
    check("and the commit evidence", turn.committed_at is not None, True)
    attempts = await attempts_for()
    check("one attempt row exists", len(attempts), 1)
    check("recorded as sent", attempts[0].state, "sent")
    check("the budget was consumed once", (await conv_for()).send_budget_used, 1)


async def _uncertainty() -> None:
    print("\nworker: an ambiguous provider outcome pauses the conversation")
    await reset_all()
    await freeze()
    twin = FakeTwin()
    adapter = FakeAdapter(send_error=platform.PlatformUnknown("dispatch-uncertain"))
    await _draft_then_dispatch(twin, adapter)
    turn = (await turns_for())[0]
    check("the turn is not delivered", turn.state, "delivery_unknown")
    check("the provider POST did happen once", len(adapter.posts), 1)
    check("nothing was acknowledged", twin.acks, 0)
    check("the conversation is paused", (await conv_for()).state, "paused")
    await worker.run_once(now=BASE_TIME + timedelta(seconds=200), scope=scope(),
                          client=twin, adapter=adapter, enable_send=True)
    check("a later run never re-POSTs", len(adapter.posts), 1)
    check("and never acknowledges", twin.acks, 0)
    check("the budget kept the unknown attempt",
          (await conv_for()).send_budget_used, 1)


async def _crash_window() -> None:
    print("\nworker: an interrupted dispatch is never re-sent")
    await reset_all()
    # A real conversation first, then the interrupted-dispatch shape: a crash
    # after the ledger commit but before the provider POST looks exactly like this.
    await freeze()
    turn = (await turns_for())[0]
    async with get_session() as s:
        from sqlalchemy import update
        await s.execute(
            update(TwinBridgeTurn)
            .where(TwinBridgeTurn.turn_id == turn.turn_id)
            .values(state="dispatching")
        )
        s.add(TwinBridgeSendAttempt(
            account_id=ACCOUNT, fan_id=FAN, turn_id=turn.turn_id, attempt_no=1,
            state="dispatching", owner_token="crashed", revision=1,
            created_at=BASE_TIME, updated_at=BASE_TIME,
        ))
    adapter = FakeAdapter()
    twin = FakeTwin()
    await worker.run_once(now=BASE_TIME + timedelta(seconds=300), scope=scope(),
                          client=twin, adapter=adapter, enable_send=True)
    check("recovery performs no provider POST", len(adapter.posts), 0)
    check("and the attempt becomes unknown",
          (await attempts_for())[0].state, "delivery_unknown")
    check("with the conversation paused", (await conv_for()).state, "paused")


async def _takeover() -> None:
    print("\nworker: a takeover during generation fences the draft")
    await reset_all()
    await freeze()
    twin = FakeTwin()
    adapter = FakeAdapter()
    await worker.run_once(now=BASE_TIME + timedelta(seconds=9), scope=scope(),
                          client=twin, adapter=adapter, enable_send=False)
    check("the draft is ready before the takeover",
          (await turns_for())[0].state, "draft_ready")
    await coordination.take_takeover(account_id=ACCOUNT, fan_id=FAN,
                                     now=BASE_TIME + timedelta(seconds=10))
    await worker.run_once(now=BASE_TIME + timedelta(seconds=11), scope=scope(),
                          client=twin, adapter=adapter, enable_send=True)
    check("no obsolete draft is dispatched after takeover", len(adapter.posts), 0)
    check("the conversation stays paused", (await conv_for()).state, "paused")


async def _history() -> None:
    print("\nworker: observed external activity parks the draft")
    await reset_all()
    await freeze()
    twin = FakeTwin()
    adapter = FakeAdapter(history=[{"id": 999001, "isFromMe": True}])
    await _draft_then_dispatch(twin, adapter)
    check("an unknown outbound message blocks the send", len(adapter.posts), 0)
    check("the turn is parked", (await turns_for())[0].state, "parked")
    check("with a fixed reason", (await turns_for())[0].reason, "external-outbound")

    await reset_all()
    await freeze()
    twin = FakeTwin()
    adapter = FakeAdapter(history_error="history-unreadable")
    await _draft_then_dispatch(twin, adapter)
    check("an unreadable history is a refusal", len(adapter.posts), 0)
    check("and parks the turn", (await turns_for())[0].state, "parked")

    # A LOCALLY ATTRIBUTED human outbound row is still human activity: it must
    # park the draft. Only a PROVEN bridge sent id is exempt.
    await reset_all()
    await freeze()
    twin = FakeTwin()
    async with get_session() as s:
        s.add(Message(
            account_id=ACCOUNT, fan_id=FAN, message_id=777000111, direction="out",
            body="a human reply", media_ids="[]", media_count=0, price_cents=0,
            is_paid=None, is_tip=False, is_unsent=False,
            created_at=BASE_TIME, ingested_at=BASE_TIME, raw_json="{}",
        ))
    adapter = FakeAdapter(history=[{"id": 777000111, "isFromMe": True}])
    await _draft_then_dispatch(twin, adapter)
    check("a locally attributed human send still parks the draft",
          len(adapter.posts), 0)
    check("with the human-activity reason", (await turns_for())[0].reason,
          "external-outbound")

    # A PROVEN bridge sent attempt (the bridge's own ledger) IS exempt.
    await reset_all()
    await freeze()
    turn = (await turns_for())[0]
    async with get_session() as s:
        s.add(TwinBridgeSendAttempt(
            account_id=ACCOUNT, fan_id=FAN, turn_id=turn.turn_id, attempt_no=1,
            state="sent", owner_token="prior", revision=1,
            provider_message_id=777000111, created_at=BASE_TIME, updated_at=BASE_TIME,
        ))
    twin = FakeTwin()
    adapter = FakeAdapter(history=[{"id": 777000111, "isFromMe": True}])
    await _draft_then_dispatch(twin, adapter)
    check("the bridge's OWN proven sent id is not a takeover", len(adapter.posts), 1)


async def _historical_outbound_boundary() -> None:
    print("\nworker: old creator replies are context; current-turn replies block")
    for label, stamp, expected_posts, reason in (
        ("older", "2026-10-15T03:59:59Z", 1, None),
        ("older with offset", "2026-10-15T13:59:59+10:00", 1, None),
        ("same instant", "2026-10-15T04:00:00Z", 0, "external-outbound"),
        ("newer", "2026-10-15T04:00:01Z", 0, "external-outbound"),
        ("missing time", None, 0, "history-unreadable"),
        ("unreadable time", "yesterday", 0, "history-unreadable"),
        ("timezone missing", "2026-10-15T03:59:59", 0, "history-unreadable"),
    ):
        await reset_all()
        await freeze()
        adapter = FakeAdapter(history=[{"id": 4999, "isFromMe": True,
                                       "createdAt": stamp}])
        await _draft_then_dispatch(FakeTwin(), adapter)
        check(label + " creator history: POST count", len(adapter.posts), expected_posts)
        check(label + " creator history: reason", (await turns_for())[0].reason, reason)
        check(label + " uses frozen source identity", adapter.history_scope,
              (BASE_TIME, {5001}))

    # Arrival order is not provider order: the second receipt has an OLDER
    # provider instant. A creator reply between those two instants still blocks.
    await reset_all()
    await seed_turn(message_id=5001, received_at=BASE_TIME)
    await seed_turn(message_id=5002, received_at=BASE_TIME + timedelta(seconds=1))
    async with get_session() as s:
        receipt = await s.get(TwinBridgeInput, (ACCOUNT, FAN, 5002))
        receipt.occurred_at = BASE_TIME - timedelta(seconds=10)
    await turns.form_turns(now=BASE_TIME + timedelta(seconds=10), scope=scope())
    adapter = FakeAdapter(history=[{"id": 4999, "isFromMe": True,
                                   "createdAt": "2026-10-15T03:59:55Z"}])
    await _draft_then_dispatch(FakeTwin(), adapter, started=BASE_TIME + timedelta(seconds=11))
    check("minimum provider time covers every frozen member", adapter.history_scope,
          (BASE_TIME - timedelta(seconds=10), {5001, 5002}))
    check("a reply after the older member still blocks", len(adapter.posts), 0)
    check("the blocked reply spends no send budget", (await conv_for()).send_budget_used, 0)

    await reset_all()
    source_at = BASE_TIME.replace(microsecond=123456)
    await seed_turn(received_at=source_at)
    await turns.form_turns(now=BASE_TIME + timedelta(seconds=10), scope=scope())
    adapter = FakeAdapter(history=[{"id": 5000, "isFromMe": True,
                                   "createdAt": "2026-10-15T04:00:00.123999Z"}])
    await _draft_then_dispatch(FakeTwin(), adapter, started=BASE_TIME + timedelta(seconds=11))
    check("same-millisecond reply is not truncated behind its source", len(adapter.posts), 0)
    check("the source boundary floors to wire precision", adapter.history_scope,
          (BASE_TIME.replace(microsecond=123000), {5001}))


async def _ack_retry() -> None:
    print("\nworker: only the idempotent ack is retried")
    await reset_all()
    await freeze()
    twin = FakeTwin(ack_failures=1)
    adapter = FakeAdapter()
    await _draft_then_dispatch(twin, adapter)
    check("the platform POST happens once despite ack failures", len(adapter.posts), 1)
    check("the turn waits for a healthy ack", (await turns_for())[0].state, "sent")
    check("the failed ack attempt is counted", (await turns_for())[0].ack_attempts, 1)
    await worker.run_once(now=BASE_TIME + timedelta(seconds=120), scope=scope(),
                          client=twin, adapter=adapter, enable_send=True)
    check("no second platform POST on ack recovery", len(adapter.posts), 1)
    check("the ack eventually succeeds", (await turns_for())[0].state, "delivered")
    check("and the ack retry was counted", (await turns_for())[0].ack_attempts, 2)
    check("the transcript commit is recorded",
          (await turns_for())[0].committed_at is not None, True)


async def _budget_restart() -> None:
    print("\nworker: the pilot send limit survives a restart")
    await reset_all()
    os.environ["TWIN_BRIDGE_SEND_LIMIT"] = "1"
    try:
        await freeze()
        twin = FakeTwin()
        adapter = FakeAdapter(send_error=platform.PlatformUnknown("dispatch-uncertain"))
        await _draft_then_dispatch(twin, adapter)
        check("the single budget unit was consumed", len(adapter.posts), 1)
        await coordination.resume_conversation(account_id=ACCOUNT, fan_id=FAN,
                                               now=BASE_TIME)
        await reset_turn_work()
        await freeze(message_id=5002)
        twin2 = FakeTwin()
        adapter2 = FakeAdapter()
        second = await worker.run_once(now=BASE_TIME + timedelta(seconds=9), scope=scope(),
                                       client=twin2, adapter=adapter2, enable_send=True)
        check("the fresh turn is only drafted in its first pass", len(adapter2.posts), 0)
        second = await worker.run_once(now=BASE_TIME + timedelta(seconds=12), scope=scope(),
                                       client=twin2, adapter=adapter2, enable_send=True)
        check("a fresh run cannot dispatch past the exhausted cap",
              len(adapter2.posts), 0)
        check("and the reason is recorded",
              "send-budget-exhausted" in second.reasons, True)
    finally:
        os.environ["TWIN_BRIDGE_SEND_LIMIT"] = "5"


async def _competing_writers() -> None:
    print("\nworker: the shared OFClient boundary denies competing writers")
    await reset_all()
    async with get_session() as s:
        await coordination.ensure_conversation(
            s, account_id=ACCOUNT, fan_id=FAN, creator_slug=CREATOR,
            conversation_id=str(FAN), scope_fingerprint="x", now=BASE_TIME,
            send_budget_limit=1,
        )
    await coordination.reserve_ownership(account_id=ACCOUNT, fan_id=FAN,
                                         owner_token="bridge", now=BASE_TIME)
    session = FakeSession()
    client = raw_client(session)
    denied = None
    try:
        client.send_message(FAN, "a competing automation reply", auto_tag=False)
    except Exception as exc:  # noqa: BLE001
        denied = type(exc).__name__
    check("a writer to an owned chat is denied before the POST",
          denied, "BridgeOwnedError")
    check("and it made no HTTP call at all", len(session.calls), 0)

    session = FakeSession()
    client = raw_client(session)
    client.send_message(FAN + 1, "an unowned chat still works", auto_tag=False)
    check("an unowned chat on the same account still sends", len(session.calls), 1)

    broadcast = None
    try:
        client.send_message(FAN + 2, "mass blast", auto_tag=False, purpose="broadcast")
    except Exception as exc:  # noqa: BLE001
        broadcast = type(exc).__name__
    check("an ambiguous broadcast to a reserved account is refused",
          broadcast, "BroadcastRefusedError")
    await coordination.release_ownership(account_id=ACCOUNT, fan_id=FAN,
                                         owner_token="bridge", now=BASE_TIME)


async def _bookkeeping_failure() -> None:
    print("\nworker: a bookkeeping failure cannot fail a confirmed send")
    await reset_all()
    await freeze()
    twin = FakeTwin()
    adapter = FakeAdapter()

    failing = types.ModuleType("attribution")

    async def _boom(*_a, **_k):
        raise RuntimeError("synthetic attribution failure")

    failing.write_outbound_attribution = _boom
    previous = sys.modules.get("attribution")
    sys.modules["attribution"] = failing
    try:
        await _draft_then_dispatch(twin, adapter)
    finally:
        if previous is not None:
            sys.modules["attribution"] = previous
        else:
            sys.modules.pop("attribution", None)

    check("the ledger still records the send", (await attempts_for())[0].state, "sent")
    check("the turn still completes", (await turns_for())[0].state, "delivered")
    check("and exactly one provider POST was made", len(adapter.posts), 1)


async def _queued_second_turn() -> None:
    print("\nworker: a newer message queues behind the frozen turn")
    await reset_all()
    await freeze()
    twin = FakeTwin()
    adapter = FakeAdapter()
    await worker.run_once(now=BASE_TIME + timedelta(seconds=9), scope=scope(),
                          client=twin, adapter=adapter, enable_send=False)
    frozen_event = (await turns_for())[0].event_json

    await seed_turn(message_id=5100, text="a newer message",
                    received_at=BASE_TIME + timedelta(seconds=20))
    await turns.form_turns(now=BASE_TIME + timedelta(seconds=30), scope=scope())
    check("the newer message forms a second turn", len(await turns_for()), 2)
    check("the first frozen payload is unchanged",
          (await turns_for())[0].event_json, frozen_event)

    await worker.run_once(now=BASE_TIME + timedelta(seconds=31), scope=scope(),
                          client=twin, adapter=adapter, enable_send=True)
    check("the second turn is not submitted while the first is open", twin.drafts, 1)
    states = {t.turn_id: t.state for t in await turns_for()}
    check("the older turn advanced", states[(await turns_for())[0].turn_id], "delivered")
    check("the newer turn is still waiting", states[(await turns_for())[1].turn_id], "frozen")
    async with get_session() as s:
        await coordination.ensure_conversation(
            s, account_id=ACCOUNT, fan_id=FAN, creator_slug=CREATOR,
            conversation_id=str(FAN), scope_fingerprint="x", now=BASE_TIME,
            send_budget_limit=1,
        )
    await coordination.reserve_ownership(account_id=ACCOUNT, fan_id=FAN,
                                         owner_token="bridge", now=BASE_TIME)
    session = FakeSession()
    client = raw_client(session)
    denied = None
    try:
        client.send_message(FAN, "a competing automation reply", auto_tag=False)
    except Exception as exc:  # noqa: BLE001
        denied = type(exc).__name__
    check("a writer to an owned chat is denied before the POST",
          denied, "BridgeOwnedError")
    check("and it made no HTTP call at all", len(session.calls), 0)

    session = FakeSession()
    client = raw_client(session)
    client.send_message(FAN + 1, "an unowned chat still works", auto_tag=False)
    check("an unowned chat on the same account still sends", len(session.calls), 1)

    broadcast = None
    try:
        client.send_message(FAN + 2, "mass blast", auto_tag=False, purpose="broadcast")
    except Exception as exc:  # noqa: BLE001
        broadcast = type(exc).__name__
    check("an ambiguous broadcast to a reserved account is refused",
          broadcast, "BroadcastRefusedError")
    await coordination.release_ownership(account_id=ACCOUNT, fan_id=FAN,
                                         owner_token="bridge", now=BASE_TIME)


try:
    run(main())
except BaseException as exc:  # noqa: BLE001
    failures.append(f"suite crashed: {type(exc).__name__} ({exc})")
    print(f"  FAIL suite crashed: {type(exc).__name__}: {exc}")

run(engine.dispose())
shutil.rmtree(_TMP_ROOT, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("all cases passed (twin bridge delivery)")
