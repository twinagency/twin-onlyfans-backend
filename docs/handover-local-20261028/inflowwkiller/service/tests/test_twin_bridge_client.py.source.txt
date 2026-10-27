#!/usr/bin/env python3
"""Twin bridge HTTP client tests - plain asserts, no network, no live Twin.

Windows:  .venv\\Scripts\\python.exe service\\tests\\test_twin_bridge_client.py

Every request runs against an httpx MockTransport that records what was sent and
returns a scripted response. Nothing here opens a socket.
"""
import asyncio
import json
import sys
import threading
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SERVICE_DIR = TESTS_DIR.parent
sys.path.insert(0, str(SERVICE_DIR))

import httpx  # noqa: E402

from twin_bridge import contract  # noqa: E402
from twin_bridge.client import (  # noqa: E402
    CATEGORY_AUTH,
    CATEGORY_BAD_REQUEST,
    CATEGORY_CONFIG,
    CATEGORY_CONNECTION,
    CATEGORY_DEADLINE,
    CATEGORY_FORBIDDEN,
    CATEGORY_INTERNAL,
    CATEGORY_MALFORMED,
    CATEGORY_NOT_FOUND,
    CATEGORY_OVERLOADED,
    CATEGORY_STORAGE,
    CATEGORY_UNCERTAIN,
    ROUTE_REPLIES,
    ROUTE_STATUS,
    TwinBridgeClient,
    TwinClientError,
    validate_base_url,
    validate_token,
)
from twin_bridge.errors import BridgeConfigError  # noqa: E402

TOKEN = "test-bridge-token-0123456789abcdef"   # 36 chars, no whitespace
BASE = "http://127.0.0.1:4311"
CHANNEL, ACCOUNT, CREATOR, FAN, MSG = (
    "onlyfans", "acct-synthetic-1", "creator-synthetic-1", "9001", "msg-synthetic-1",
)

EVENT = {
    "schemaVersion": 1,
    "channel": CHANNEL,
    "accountId": ACCOUNT,
    "creatorSlug": CREATOR,
    "conversationId": FAN,
    "fanId": FAN,
    "messageId": MSG,
    "occurredAt": "2026-10-15T04:00:00.000Z",
    "receivedAt": "2026-10-15T04:00:01.000Z",
    "direction": "inbound",
    "text": "secret fan text that must never be logged",
}
CONV = contract.conversation_key(CHANNEL, ACCOUNT, CREATOR, FAN, FAN)
REQ = contract.message_key(CHANNEL, ACCOUNT, FAN, MSG)

CREATED = "2026-10-15T04:00:01.000Z"
COMPLETED = "2026-10-15T04:00:02.000Z"
DELIVERED = "2026-10-15T04:00:03.000Z"

failures: list[str] = []


def check(label, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def check_true(label, got) -> None:
    check(label, bool(got), True)


def run(coro):
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    worker = threading.Thread(target=target)
    worker.start()
    worker.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def error_for(label, coro_factory, want_category, want_uncertain=None) -> None:
    """Run a call and assert it failed with one fixed category."""
    try:
        run(coro_factory())
    except TwinClientError as e:
        check(label, e.category, want_category)
        if want_uncertain is not None:
            check(f"and {label} reports uncertainty={want_uncertain}", e.uncertain, want_uncertain)
        check_true(f"and {label} leaks no token", TOKEN not in str(e))
        check_true(f"and {label} leaks no fan text", "secret fan text" not in str(e))
        return
    except Exception as e:  # noqa: BLE001
        failures.append(f"{label}: raised {type(e).__name__} ({e})")
        print(f"  FAIL {label}: raised {type(e).__name__}")
        return
    failures.append(f"{label}: did not raise")
    print(f"  FAIL {label}: did not raise")


def envelope(result: dict) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json; charset=utf-8"},
        json={"schemaVersion": 1, "ok": True, "result": result},
    )


def result_for(status="completed", **over) -> dict:
    """A VALID version-1 projection for the given lifecycle status."""
    base = {
        "requestId": REQ, "conversationKey": CONV, "status": status, "outcome": "ok",
        "deliverable": True, "text": "stub draft", "epoch": 0,
        "createdAt": CREATED, "completedAt": COMPLETED, "deliveredAt": None, "committedAt": None,
        "usage": {"provider": "stub", "tokensIn": 1, "tokensOut": 2, "cost": 0, "ms": 3, "attempts": 1},
    }
    if status in ("queued", "in_progress"):
        base.update(outcome=None, deliverable=False, text="", completedAt=None)
    elif status == "failed":
        base.update(outcome="failed", deliverable=False, text="")
    elif status in ("cancelled", "interrupted"):
        base.update(outcome=None, deliverable=False, text="")
    elif status == "delivered":
        base.update(deliverable=False, text="", deliveredAt=DELIVERED, committedAt=DELIVERED)
    base.update(over)
    return base


class Recorder:
    """A MockTransport handler that records every request it is asked to serve."""

    def __init__(self, respond):
        self.calls: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self._respond(request)


def client(respond, **over) -> tuple[TwinBridgeClient, Recorder]:
    rec = Recorder(respond)
    kwargs = dict(base_url=BASE, token=TOKEN, transport=httpx.MockTransport(rec))
    kwargs.update(over)
    return TwinBridgeClient(**kwargs), rec


# ── response builders ─────────────────────────────────────────────────
#
# Every scripted reply is delivered as a STREAM. A buffered `httpx.Response`
# (one built with `content=`/`json=`) is already consumed, so reading it through
# the client's streaming path would raise `StreamConsumed` - which is also why
# these helpers exist rather than inline `httpx.Response(...)` literals.

JSON = "application/json; charset=utf-8"


class Body(httpx.AsyncByteStream):
    """One in-memory body, handed over as a stream so the client can read it."""

    def __init__(self, payload: bytes = b""):
        self.payload = payload
        self.closed = False

    async def __aiter__(self):
        yield self.payload

    async def aclose(self) -> None:
        self.closed = True


def json_bytes(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


def response(status: int, payload: bytes, content_type: str = JSON, **extra) -> httpx.Response:
    headers = {"content-type": content_type, **extra}
    return httpx.Response(status, headers=headers, stream=Body(payload))


def ok_response(result) -> httpx.Response:
    return response(200, json_bytes({"schemaVersion": 1, "ok": True, "result": result}))


def error_response(status, code, **error_extra) -> httpx.Response:
    return response(
        status,
        json_bytes({"schemaVersion": 1, "ok": False, "error": {"code": code, **error_extra}}),
    )


class Session:
    """A client plus its recorder, closed on exit."""

    def __init__(self, respond, **over):
        self.client, self.rec = client(respond, **over)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.client.aclose()


class CountingStream(httpx.AsyncByteStream):
    """A body delivered in chunks, counting exactly how many were pulled."""

    def __init__(self, payload: bytes, size: int = 64, content_type: str = "application/json"):
        self.payload = payload
        self.size = size
        self.pulled = 0
        self.closed = False
        self.headers = {"content-type": content_type}

    @property
    def chunks(self) -> int:
        return -(-len(self.payload) // self.size)

    async def __aiter__(self):
        for start in range(0, len(self.payload), self.size):
            self.pulled += 1
            yield self.payload[start:start + self.size]

    async def aclose(self) -> None:
        self.closed = True

    def response(self, **extra) -> httpx.Response:
        return httpx.Response(200, headers={**self.headers, **extra}, stream=self)


class FailingStream(httpx.AsyncByteStream):
    """A body that dies part-way through, as a dropped connection would."""

    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield b'{"schemaVersion": 1,'
        raise httpx.ReadError("synthetic mid-body read failure")

    async def aclose(self) -> None:
        self.closed = True


async def expect_error(label, coro, want_category, want_uncertain=None):
    """Await one call and assert it failed with ONE fixed, sanitised category."""
    try:
        await coro
    except TwinClientError as e:
        check(label, e.category, want_category)
        if want_uncertain is not None:
            check(f"{label}: uncertainty", e.uncertain, want_uncertain)
        check_true(f"{label}: leaks no token", TOKEN not in str(e))
        check_true(f"{label}: leaks no fan text", "secret fan text" not in str(e))
        return e
    except Exception as e:  # noqa: BLE001 - a raw escape is itself the failure
        failures.append(f"{label}: raised {type(e).__name__} ({e})")
        print(f"  FAIL {label}: raised {type(e).__name__}")
        return None
    failures.append(f"{label}: did not raise")
    print(f"  FAIL {label}: did not raise")
    return None


def config_failure(label, call) -> None:
    """A trusted-config value that must be refused before any request is built."""
    try:
        call()
    except BridgeConfigError:
        check_true(label, True)
        return
    except Exception as e:  # noqa: BLE001
        failures.append(f"{label}: raised {type(e).__name__}")
        print(f"  FAIL {label}: raised {type(e).__name__}")
        return
    failures.append(f"{label}: accepted")
    print(f"  FAIL {label}: accepted")


# ── the suite ─────────────────────────────────────────────────────────

async def main() -> None:
    # ---- trusted configuration is refused before a request exists ----
    print("-- configuration --")
    check("loopback http is canonical", validate_base_url("http://127.0.0.1:4311"), BASE)
    check("an IPv6 loopback is canonical", validate_base_url("http://[::1]:4311"), "http://[::1]:4311")
    config_failure("https is refused", lambda: validate_base_url("https://127.0.0.1:4311"))
    config_failure("a remote host is refused", lambda: validate_base_url("http://10.0.0.5:4311"))
    config_failure("localhost is not assumed to be loopback",
                   lambda: validate_base_url("http://localhost:4311"))
    config_failure("embedded credentials are refused",
                   lambda: validate_base_url("http://user:pw@127.0.0.1:4311"))
    config_failure("a path is refused", lambda: validate_base_url("http://127.0.0.1:4311/v1"))
    config_failure("a query is refused", lambda: validate_base_url("http://127.0.0.1:4311/?x=1"))
    config_failure("a missing port is refused", lambda: validate_base_url("http://127.0.0.1"))
    config_failure("an out-of-range port is refused", lambda: validate_base_url("http://127.0.0.1:70000"))
    config_failure("a short token is refused", lambda: validate_token("tooshort"))
    config_failure("a whitespace-bearing token is refused",
                   lambda: validate_token("a" * 20 + " " + "b" * 20))
    check("a valid token is accepted", validate_token(TOKEN), TOKEN)

    # ---- a valid draft comes back whole ----
    print("-- accepted projections --")
    async with Session(lambda req: ok_response(result_for())) as s:
        draft = await s.client.request_draft(EVENT)
        check("a completed draft is deliverable", draft.deliverable, True)
        check("its text is handed over", draft.text, "stub draft")
        check("its status is completed", draft.status, "completed")
        check("its outcome is ok", draft.outcome, "ok")
        check("a returned result is never uncertain", draft.uncertain, False)
        check("exactly one HTTP attempt was made", len(s.rec.calls), 1)
        sent = s.rec.calls[0]
        check("the draft went to the replies route", sent.url.path, ROUTE_REPLIES)
        check("the bearer token is attached", sent.headers.get("authorization"), f"Bearer {TOKEN}")
        check("the body is the event, compactly encoded", json.loads(sent.content.decode()), EVENT)

    async with Session(lambda req: ok_response(result_for())) as s:
        await s.client.read_status(EVENT)
        check("status reads the status route", s.rec.calls[0].url.path, ROUTE_STATUS)

    for status in ("queued", "in_progress"):
        async with Session(lambda req, st=status: ok_response(result_for(st))) as s:
            pending = await s.client.request_draft(EVENT)
            check(f"a {status} request is not deliverable", pending.deliverable, False)
            check(f"a {status} request carries no text", pending.text, "")
            check(f"a {status} request carries a null outcome", pending.outcome, None)
            check(f"a {status} request has no completion stamp", pending.completed_at, None)

    async with Session(lambda req: ok_response(result_for("completed", outcome="blocked"))) as s:
        blocked = await s.client.request_draft(EVENT)
        check("a blocked reply may still be a draft", blocked.deliverable, True)
        check("and keeps its blocked outcome", blocked.outcome, "blocked")

    async with Session(lambda req: ok_response(
            result_for("failed", outcome="blocked", deliverable=False, text=""))) as s:
        undrafted = await s.client.request_draft(EVENT)
        check("a blocked turn with no draft is failed", undrafted.status, "failed")
        check("and offers nothing to deliver", undrafted.deliverable, False)
        check("a failed turn records its outcome", undrafted.outcome, "blocked")

    for status in ("cancelled", "interrupted"):
        async with Session(lambda req, st=status: ok_response(result_for(st))) as s:
            stopped = await s.client.request_draft(EVENT)
            check(f"a {status} request reads back as {status}", stopped.status, status)
            check(f"a {status} request is undeliverable", stopped.deliverable, False)

    async with Session(lambda req: ok_response(result_for("delivered"))) as s:
        done = await s.client.request_draft(EVENT)
        check("a delivered request keeps its decision", done.delivered_at, DELIVERED)
        check("a delivered request is never offered again", done.deliverable, False)
        check("and hands over no text", done.text, "")

    async with Session(lambda req: ok_response(dict(result_for("delivered"), committedAt=None))) as s:
        decision_only = await s.client.request_draft(EVENT)
        check("a delivery decision without a commit is valid", decision_only.status, "delivered")
        check("and its commit stamp stays null", decision_only.committed_at, None)

    # ---- R3: a malformed success is never offered as a draft ----
    print("-- R3: strict success validation --")
    for label, override in (
        ("a failed outcome cannot be deliverable", {"outcome": "failed"}),
        ("delivery evidence cannot be deliverable", {"deliveredAt": DELIVERED}),
        ("a commit without a decision is refused", {"deliveredAt": None, "committedAt": DELIVERED}),
        ("delivery evidence needs a delivered status", {"deliveredAt": DELIVERED, "status": "completed"}),
        ("delivered needs delivery evidence", {"status": "delivered", "deliveredAt": None}),
        ("failed needs an outcome", {"status": "failed", "outcome": None}),
        ("finished needs a completion stamp",
         {"status": "failed", "outcome": "failed", "completedAt": None}),
        ("unfinished keeps no completion stamp", {"status": "queued", "outcome": None,
                                                 "deliverable": False, "text": "",
                                                 "completedAt": COMPLETED}),
        ("unfinished keeps no outcome", {"status": "queued", "outcome": "ok",
                                         "deliverable": False, "text": "", "completedAt": None}),
        ("undeliverable text is refused", {"deliverable": False}),
        ("a boolean is not an epoch", {"epoch": True}),
        ("a null epoch is refused", {"epoch": None}),
        ("a float is not an epoch", {"epoch": 1.5}),
        ("an epoch cannot be negative", {"epoch": -1}),
        ("a malformed usage is refused",
         {"usage": {"provider": "stub", "tokensIn": "bad", "cost": -1}}),
        ("a boolean token count is refused",
         {"usage": {"provider": "stub", "tokensIn": True, "tokensOut": 1, "cost": 0,
                    "ms": 1, "attempts": 1}}),
        ("a missing usage is refused", {"usage": None}),
        ("a list-valued usage is refused", {"usage": []}),
        ("an invalid creation stamp is refused", {"createdAt": "invalid-date"}),
        ("a naive creation stamp is refused", {"createdAt": "2026-10-15T04:00:01"}),
        ("a missing creation stamp is refused", {"createdAt": None}),
        ("an unknown status is refused", {"status": "weird"}),
        ("a list-valued status is refused", {"status": []}),
        ("a null status is refused", {"status": None}),
        ("an unknown outcome is refused", {"outcome": "weird"}),
        ("an object outcome is refused", {"outcome": {}}),
        ("a non-boolean deliverable flag is refused", {"deliverable": "yes"}),
        ("a numeric text is refused", {"text": 5}),
        ("a null request identity is refused", {"requestId": None}),
    ):
        async with Session(lambda req, o=override: ok_response(dict(result_for(), **o))) as s:
            await expect_error(label, s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)

    async with Session(lambda req: ok_response(dict(result_for(), requestId="ofm_someone_else"))) as s:
        await expect_error("another request's id is refused",
                           s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)
    async with Session(lambda req: ok_response(dict(result_for(), conversationKey="of_someone_else"))) as s:
        await expect_error("another conversation's key is refused",
                           s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)

    for label, scripted in (
        ("an unsupported wire version is refused",
         response(200, json_bytes({"schemaVersion": 2, "ok": True, "result": result_for()}))),
        ("an ok:false body in a 200 is refused",
         response(200, json_bytes({"schemaVersion": 1, "ok": False, "result": result_for()}))),
        ("a result that is not an object is refused",
         response(200, json_bytes({"schemaVersion": 1, "ok": True, "result": []}))),
        ("a non-JSON body is refused",
         response(200, b"not json", "text/plain")),
        ("a malformed JSON body is refused",
         response(200, b"{not json")),
        ("a JSON array envelope is refused",
         response(200, b"[1,2,3]")),
        ("invalid UTF-8 is refused",
         response(200, b"\xff\xfe{}")),
        ("a redirect is refused, never followed",
         response(302, b"", "text/plain", location="/v1/onlyfans/replies")),
    ):
        async with Session(lambda req, r=scripted: r) as s:
            await expect_error(label, s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)
            check(f"{label}: exactly one attempt", len(s.rec.calls), 1)

    # ---- R4: the byte budget is enforced while streaming ----
    print("-- R4: bounded streaming --")
    huge = CountingStream(b" " * (20 * 64), size=64)
    async with Session(lambda req: huge.response(), max_response_bytes=128) as s:
        await expect_error("an oversized stream is refused",
                           s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)
    check("the reader stopped at the limit, it did not drain 1280 bytes", huge.pulled <= 3, True)
    check("and the response was closed", huge.closed, True)

    declared = CountingStream(b" " * (20 * 64), size=64)
    async with Session(
            lambda req: declared.response(**{"content-length": "400"}), max_response_bytes=128) as s:
        await expect_error("a declared oversize is refused",
                           s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)
    check("a declared oversize is refused before a byte is read", declared.pulled, 0)
    check("and its response is closed", declared.closed, True)

    bad_length = CountingStream(b"{}\n", size=8)
    async with Session(
            lambda req: bad_length.response(**{"content-length": "not-a-number"}),
            max_response_bytes=128) as s:
        await expect_error("an unreadable declared length is refused",
                           s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)
    check("and it is not read either", bad_length.pulled, 0)

    plain = CountingStream(b"not json at all", size=64, content_type="text/plain")
    async with Session(lambda req: plain.response(), max_response_bytes=128) as s:
        await expect_error("a non-JSON stream is refused",
                           s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)
    check("a malformed stream is closed too", plain.closed, True)

    broken = FailingStream()
    async with Session(lambda req: httpx.Response(
            200, headers={"content-type": JSON}, stream=broken)) as s:
        await expect_error("a body that fails mid-read is uncertain",
                           s.client.request_draft(EVENT), CATEGORY_UNCERTAIN, True)
        check("a failed read is still one attempt", len(s.rec.calls), 1)
    check("a failed stream is closed", broken.closed, True)

    good_body = json_bytes({"schemaVersion": 1, "ok": True, "result": result_for()})
    small = CountingStream(good_body, size=64)
    async with Session(lambda req: small.response(), max_response_bytes=64 * 1024) as s:
        check("a chunked reply within the limit still parses",
              (await s.client.request_draft(EVENT)).status, "completed")
    check("and its body was read to the end", small.pulled, small.chunks)

    big = CountingStream(b"x" * (80 * 1024), size=1024)
    async with Session(lambda req: big.response(), max_response_bytes=64 * 1024) as s:
        await expect_error("a chunked body above the default limit is still refused",
                           s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)
    check("and it stopped inside the limit", big.pulled < big.chunks, True)

    # ---- R5: categories and admission uncertainty ----
    print("-- R5: categories and uncertainty --")
    for label, exc, category, uncertain in (
        ("a refused connect is not uncertainty",
         httpx.ConnectError("synthetic connect failure"), CATEGORY_CONNECTION, False),
        ("a connect timeout is not uncertainty",
         httpx.ConnectTimeout("synthetic connect timeout"), CATEGORY_CONNECTION, False),
        ("a pool timeout is not uncertainty",
         httpx.PoolTimeout("synthetic pool timeout"), CATEGORY_CONNECTION, False),
        ("a write error leaves admission open",
         httpx.WriteError("synthetic write failure"), CATEGORY_UNCERTAIN, True),
        ("a write timeout leaves admission open",
         httpx.WriteTimeout("synthetic write timeout"), CATEGORY_UNCERTAIN, True),
        ("a read timeout leaves admission open",
         httpx.ReadTimeout("synthetic read timeout"), CATEGORY_UNCERTAIN, True),
        ("a protocol cut leaves admission open",
         httpx.RemoteProtocolError("synthetic protocol cut"), CATEGORY_UNCERTAIN, True),
    ):
        def raiser(req, err=exc):
            raise err

        async with Session(raiser) as s:
            await expect_error(label, s.client.request_draft(EVENT), category, uncertain)
            check(f"{label}: exactly one attempt", len(s.rec.calls), 1)

    for label, scripted, category, uncertain in (
        ("an internal failure is internal", error_response(500, "internal"), CATEGORY_INTERNAL, True),
        ("a storage failure is storage",
         error_response(500, "storage-unavailable"), CATEGORY_STORAGE, True),
        ("Twin's own deadline is the deadline",
         error_response(503, "deadline-exceeded"), CATEGORY_DEADLINE, True),
        ("an overloaded bridge is a documented refusal",
         error_response(429, "overloaded"), CATEGORY_OVERLOADED, False),
        ("a bad request is a documented refusal",
         error_response(400, "bad-request"), CATEGORY_BAD_REQUEST, False),
        ("invalid JSON is a documented refusal",
         error_response(400, "invalid-json"), CATEGORY_BAD_REQUEST, False),
        ("an oversized body is a documented refusal",
         error_response(413, "oversized-body"), CATEGORY_BAD_REQUEST, False),
        ("a rejected acknowledgement is a documented refusal",
         error_response(409, "ack-rejected"), CATEGORY_BAD_REQUEST, False),
        ("an unauthenticated call is an auth failure",
         error_response(401, "unauthorized"), CATEGORY_AUTH, False),
        ("a forbidden call is a forbidden failure",
         error_response(403, "forbidden"), CATEGORY_FORBIDDEN, False),
        ("an unknown request is not found",
         error_response(404, "not-found"), CATEGORY_NOT_FOUND, False),
    ):
        async with Session(lambda req, r=scripted: r) as s:
            await expect_error(label, s.client.request_draft(EVENT), category, uncertain)
            check(f"{label}: exactly one attempt", len(s.rec.calls), 1)

    async with Session(lambda req: error_response(500, "internal", retryable=True)) as s:
        check_true("an advertised retry flag does not change the category",
                   await expect_error("an advertised retry flag does not change the category",
                                      s.client.request_draft(EVENT), CATEGORY_INTERNAL, True) is not None)

    for label, scripted in (
        ("an arbitrary non-JSON 503 is not a deadline",
         response(503, b"synthetic unrelated server failure", "text/plain")),
        ("an unknown error code is not a bridge outcome", error_response(500, "make-it-up")),
        ("a status that contradicts its code is not trusted",
         error_response(500, "deadline-exceeded")),
        ("a 503 with the internal code is not the deadline", error_response(503, "internal")),
        ("an error envelope without a code is refused",
         response(500, json_bytes({"schemaVersion": 1, "ok": False, "error": {}}))),
        ("an ok:true error body is refused",
         response(500, json_bytes({"schemaVersion": 1, "ok": True, "error": {"code": "internal"}}))),
        ("an unsupported error schema version is refused",
         response(500, json_bytes({"schemaVersion": 2, "ok": False, "error": {"code": "internal"}}))),
        ("an error body that is not JSON is refused",
         response(500, b"<html>oops</html>", "text/html")),
    ):
        async with Session(lambda req, r=scripted: r) as s:
            await expect_error(label, s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)
            check(f"{label}: exactly one attempt", len(s.rec.calls), 1)

    # ---- R3a: the envelope and every projected field are required ----
    print("-- R3a: envelope and required projection fields --")
    for route, label in (("request_draft", "a draft"),
                         ("read_status", "a status read")):
        async with Session(lambda req: response(
                200, json_bytes({"schemaVersion": True, "ok": True, "result": result_for()}))) as s:
            await expect_error(f"a boolean success version is refused for {label}",
                               getattr(s.client, route)(EVENT), CATEGORY_MALFORMED, True)

    async with Session(lambda req: response(
            429, json_bytes({"schemaVersion": True, "ok": False,
                             "error": {"code": "overloaded"}}))) as s:
        await expect_error("a boolean error version is not a proven refusal",
                           s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)
        check("and the malformed overload still made one attempt", len(s.rec.calls), 1)

    for field in ("requestId", "conversationKey", "status", "outcome", "deliverable",
                  "text", "epoch", "createdAt", "completedAt", "deliveredAt", "committedAt",
                  "usage"):
        incomplete = {k: v for k, v in result_for().items() if k != field}
        async with Session(lambda req, v=incomplete: ok_response(v)) as s:
            await expect_error(f"a projection without {field} is refused",
                               s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)

    for field in ("deliveredAt", "committedAt", "status"):
        incomplete = {k: v for k, v in result_for().items() if k != field}
        async with Session(lambda req, v=incomplete: ok_response(v)) as s:
            await expect_error(f"the status route also refuses a projection without {field}",
                               s.client.read_status(EVENT), CATEGORY_MALFORMED, True)

    for key in ("provider", "tokensIn", "tokensOut", "cost", "ms", "attempts"):
        partial = dict(result_for())
        partial["usage"] = {k: v for k, v in partial["usage"].items() if k != key}
        async with Session(lambda req, v=partial: ok_response(v)) as s:
            await expect_error(f"a usage object without {key} is refused",
                               s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)

    async with Session(lambda req: ok_response(
            dict(result_for(), deliveredAt=None, committedAt=None))) as s:
        explicit_null = await s.client.request_draft(EVENT)
        check("explicit null delivery evidence is still a valid draft",
              explicit_null.deliverable, True)
        check("and both stamps stay null",
              (explicit_null.delivered_at, explicit_null.committed_at), (None, None))

    # ---- R3b: provider metadata is a label, not an identifier ----
    print("-- R3b: provider metadata --")
    base_usage = result_for()["usage"]
    async with Session(lambda req: ok_response(
            dict(result_for(), usage={**base_usage, "provider": "synthetic/provider"}))) as s:
        labelled = await s.client.request_draft(EVENT)
        check("a provider label containing a slash is accepted",
              labelled.usage["provider"], "synthetic/provider")
        check("and it is preserved verbatim, not normalised", labelled.deliverable, True)

    for label, badge in (("a blank provider label is refused", "   "),
                         ("an empty provider label is refused", ""),
                         ("a null provider label is refused", None),
                         ("a numeric provider label is refused", 5),
                         ("a boolean provider label is refused", True)):
        async with Session(lambda req, b=badge: ok_response(
                dict(result_for(), usage={**base_usage, "provider": b}))) as s:
            await expect_error(label, s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)

    # ---- R3c: conversion failures stay inside the sanitised contract ----
    print("-- R3c: numeric and datetime conversion failures --")
    for label, override in (
        ("a year-zero creation stamp is refused", {"createdAt": "0000-01-01T00:00:00.000Z"}),
        ("an offset that underflows year one is refused",
         {"createdAt": "0001-01-01T00:00:00+01:00"}),
        ("a year-zero completion stamp is refused",
         {"completedAt": "0000-01-01T00:00:00.000Z"}),
        ("a year-zero delivery stamp is refused",
         {"status": "delivered", "deliveredAt": "0000-01-01T00:00:00.000Z", "committedAt": None}),
    ):
        async with Session(lambda req, o=override: ok_response(dict(result_for(), **o))) as s:
            err = await expect_error(label, s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)
            check_true(f"{label}: no raw exception text", "out of range" not in str(err))

    async with Session(lambda req: ok_response(
            dict(result_for(), usage={**base_usage, "cost": 10 ** 400}))) as s:
        err = await expect_error("a usage cost too large for a float is refused",
                                 s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)
        check_true("and it does not leak the conversion failure",
                   "int too large" not in str(err))

    async with Session(lambda req: ok_response(
            dict(result_for(), usage={**base_usage, "ms": 10 ** 400}))) as s:
        await expect_error("a usage duration too large for a float is refused",
                           s.client.request_draft(EVENT), CATEGORY_MALFORMED, True)

    # ---- R5: a failure AFTER the body is not proof of non-admission ----
    print("-- R5: post-response transport failure --")

    class CloseAfterBody(httpx.AsyncByteStream):
        """Yields a complete, valid body, then fails while being closed."""

        def __init__(self, payload: bytes):
            self.payload = payload

        async def __aiter__(self):
            yield self.payload

        async def aclose(self) -> None:
            raise httpx.CloseError("synthetic close failure after a complete response body")

    complete = json_bytes({"schemaVersion": 1, "ok": True, "result": result_for()})
    async with Session(lambda req: httpx.Response(
            200, headers={"content-type": JSON}, stream=CloseAfterBody(complete))) as s:
        err = await expect_error("a close failure after a complete body is uncertain",
                                 s.client.request_draft(EVENT), CATEGORY_UNCERTAIN, True)
        check("the close failure is exactly one attempt", len(s.rec.calls), 1)
        check_true("and it does not retry", len(s.rec.calls) == 1)
        check_true("and the transport message is not echoed",
                   "synthetic close failure" not in str(err))

    async with Session(lambda req: httpx.Response(
            200, headers={"content-type": JSON}, stream=CloseAfterBody(complete))) as s:
        await expect_error("the status route reports the same uncertainty",
                           s.client.read_status(EVENT), CATEGORY_UNCERTAIN, True)

    # ---- boundaries: one attempt, and no delivery path at all ----
    print("-- boundaries --")
    async with Session(lambda req: ok_response(result_for())) as s:
        # Stage 1C part 2 authorises exactly two more routes: the idempotent
        # delivery acknowledgement and the cancellation. Everything else - a
        # reset, a platform send, a delivery inference - stays absent.
        check("the ack route exists", hasattr(s.client, "acknowledge_delivery"), True)
        check("the cancel route exists", hasattr(s.client, "cancel_request"), True)
        for name in ("reset", "reset_conversation", "send_message", "deliver"):
            check(f"the client exposes no {name} route", hasattr(s.client, name), False)

    # ---- the small ack/cancel decision bodies ----
    print("-- ack and cancel decisions --")

    def decision(status, **over):
        payload = {"requestId": REQ, "conversationKey": CONV, "status": status}
        payload.update(over)
        return response(200, json_bytes({"schemaVersion": 1, "ok": True, "result": payload}))

    async with Session(lambda req: decision("delivered")) as s:
        ack = await s.client.acknowledge_delivery(EVENT)
        check("an ack returns the decision", ack.status, "delivered")
        check("with the requested identity", ack.request_id, REQ)
        check("and no uncertainty", ack.uncertain, False)
        check("the ack goes to the ack route", s.rec.calls[0].url.path, "/v1/onlyfans/replies/ack")
    async with Session(lambda req: decision("already-delivered")) as s:
        check("an idempotent ack is accepted",
              (await s.client.acknowledge_delivery(EVENT)).status, "already-delivered")
    async with Session(lambda req: decision("cancelled")) as s:
        cancelled = await s.client.cancel_request(EVENT)
        check("a cancel returns the decision", cancelled.status, "cancelled")
        check("the cancel goes to the cancel route",
              s.rec.calls[0].url.path, "/v1/onlyfans/replies/cancel")
    async with Session(lambda req: decision("already-cancelled")) as s:
        check("an idempotent cancel is accepted",
              (await s.client.cancel_request(EVENT)).status, "already-cancelled")

    for label, scripted, route in (
        ("a draft status is not an ack status", decision("completed"),
         "acknowledge_delivery"),
        ("a cancel status is not an ack status", decision("cancelled"),
         "acknowledge_delivery"),
        ("an ack status is not a cancel status", decision("delivered"), "cancel_request"),
        ("an acknowledgement without a request id is refused",
         response(200, json_bytes({"schemaVersion": 1, "ok": True,
                                   "result": {"conversationKey": CONV, "status": "delivered"}})),
         "acknowledge_delivery"),
        ("a decision for another request is refused",
         decision("delivered", requestId="ofm_someone_else"), "acknowledge_delivery"),
        ("a decision for another conversation is refused",
         decision("delivered", conversationKey="of_someone_else"), "acknowledge_delivery"),
        ("a boolean schema version is not accepted for a decision",
         response(200, json_bytes({"schemaVersion": True, "ok": True,
                                   "result": {"requestId": REQ, "conversationKey": CONV,
                                              "status": "delivered"}})),
         "acknowledge_delivery"),
        ("an ok:false decision body is refused",
         response(200, json_bytes({"schemaVersion": 1, "ok": False,
                                   "result": {"requestId": REQ, "conversationKey": CONV,
                                              "status": "delivered"}})),
         "acknowledge_delivery"),
    ):
        async with Session(lambda req, r=scripted: r) as s:
            await expect_error(label, getattr(s.client, route)(EVENT),
                               CATEGORY_MALFORMED, True)

    async with Session(lambda req: error_response(404, "not-found")) as s:
        missing = await expect_error("a missing request is not-found, not uncertainty",
                                     s.client.cancel_request(EVENT), CATEGORY_NOT_FOUND, False)
        check_true("and the not-found detail is sanitised",
                   "fan" not in str(missing).lower() or "secret" not in str(missing))


try:
    run(main())
except BaseException as exc:  # noqa: BLE001
    failures.append(f"suite crashed: {type(exc).__name__} ({exc})")
    print(f"  FAIL suite crashed: {type(exc).__name__}")

print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("all cases passed (twin bridge client)")
