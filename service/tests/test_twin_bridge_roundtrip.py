#!/usr/bin/env python3
"""FULL synthetic round trip: fastt intake -> worker -> Twin -> provider -> ack.

Windows:  .venv\\Scripts\\python.exe service\\tests\\test_twin_bridge_roundtrip.py

Starts Twin's REAL loopback HTTP adapter with a stubbed model, drives the REAL
Fastt intake/worker/coordination/ledger code against it, and fakes only the
LOWEST OnlyFans HTTP layer. No live model, no live OnlyFans, no live account.
"""
import asyncio
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import types
from datetime import datetime
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SERVICE_DIR = TESTS_DIR.parent
REPO_ROOT = SERVICE_DIR.parent
sys.path.insert(0, str(SERVICE_DIR))

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="twin-bridge-roundtrip-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP_ROOT / 'roundtrip.db').as_posix()}"
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
    Account, Fan, Message, TwinBridgeTurn,
)
from twin_bridge import worker  # noqa: E402
from twin_bridge.client import TwinBridgeClient  # noqa: E402
from twin_bridge.intake import enqueue_inbound_if_enabled  # noqa: E402
from twin_bridge.platform import OnlyFansTextAdapter  # noqa: E402
from twin_bridge.scope import load_scope  # noqa: E402

TWIN_ROOT = Path(os.environ.get("TWIN_ROOT") or (REPO_ROOT.parent / "twin-sexbot"))
HELPER = TESTS_DIR / "helpers" / "twin_bridge_server.mjs"
TSX = TWIN_ROOT / "node_modules" / "tsx" / "dist" / "cli.mjs"

TOKEN = "roundtrip-bridge-token-0123456789abcd"   # >= 32 chars, no whitespace
ACCOUNT = "acct-synthetic-1"
CREATOR = "creator-synthetic-1"
FAN = "9001"
FAN_ID = 9001
REPLY = "neutral stub reply"
TEXT_ONE = "first fan message"
TEXT_TWO = "second fan message"

ENV = {
    "TWIN_BRIDGE_ENABLED": "1",
    "TWIN_BRIDGE_ACCOUNT_ID": ACCOUNT,
    "TWIN_BRIDGE_CREATOR_SLUG": CREATOR,
    "TWIN_BRIDGE_FAN_ALLOWLIST": FAN,
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


def read_line(proc: subprocess.Popen, timeout: float = 90.0) -> str | None:
    box: queue.Queue = queue.Queue()

    def target() -> None:
        box.put(proc.stdout.readline())

    threading.Thread(target=target, daemon=True).start()
    try:
        return box.get(timeout=timeout) or None
    except queue.Empty:
        return None


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.url = "https://api.test/messages"
        self.headers = {}
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class FakeOnlyFansHTTP:
    """The lowest layer: counts message POSTs, serves a chat-history GET."""

    def __init__(self, *, history=None, created_id=555000111):
        self.posts = []
        self.gets = []
        self._history = history or []
        self._created_id = created_id

    def request(self, method, url, json=None, headers=None, timeout=None):  # noqa: A002
        self.posts.append((method, url, json))
        return FakeResponse(200, {
            "id": self._created_id, "responseType": "message", "isFromMe": True,
            "text": (json or {}).get("text", ""),
        })

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append((url, params))
        return FakeResponse(200, {"list": list(self._history), "hasMore": False})


def of_client(session):
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


async def seed_message(message_id: int, text: str, occurred: datetime) -> tuple:
    """Canonical Fastt message + the REAL intake hook for it."""
    frame = {
        "id": message_id, "fromUser": {"id": FAN_ID}, "text": f"<p>{text}</p>",
        "responseType": "message", "createdAt": occurred.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "media": [], "mediaCount": 0, "price": 0,
    }
    async with get_session() as s:
        s.add(Message(
            account_id=ACCOUNT, fan_id=FAN_ID, message_id=int(message_id), direction="in",
            body=f"<p>{text}</p>", media_ids="[]", media_count=0, price_cents=0,
            is_paid=None, is_tip=False, is_unsent=False,
            created_at=occurred, ingested_at=occurred,
            raw_json=json.dumps(frame),
        ))
    async with get_session() as s:
        return await enqueue_inbound_if_enabled(
            s, account_id=ACCOUNT, fan_id=FAN_ID, message_id=int(message_id), frame=frame,
        )


async def main() -> None:
    await init_db()
    # The real intake hook reads the PROCESS environment (as it does in
    # production), so the trusted scope has to be present, not just passed in.
    os.environ.update(ENV)
    async with get_session() as s:
        s.add(Account(id=ACCOUNT, nickname="synthetic"))
        s.add(Fan(account_id=ACCOUNT, fan_id=FAN_ID))

    if not HELPER.exists() or not TSX.exists():
        failures.append("Twin harness unavailable; the round trip did NOT run")
        print("  FAIL Twin tooling unavailable; the round trip did not run")
        return

    data_dir = _TMP_ROOT / "data"
    log_dir = _TMP_ROOT / "logs"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "TWIN_ROOT": str(TWIN_ROOT), "TWIN_BRIDGE_TEST_TOKEN": TOKEN,
        "TWIN_BRIDGE_TEST_ACCOUNT": ACCOUNT, "TWIN_BRIDGE_TEST_CREATOR": CREATOR,
        "TWIN_BRIDGE_TEST_FAN": FAN, "TWIN_BRIDGE_TEST_REPLY": REPLY,
        "TWIN_BRIDGE_TEST_DATA": str(data_dir), "TWIN_BRIDGE_TEST_LOG": str(log_dir),
    })
    for var in ("ONLYFANS_BRIDGE_TOKEN", "ONLYFANS_BRIDGE_PORT", "ONLYFANS_ACCOUNT_BINDINGS"):
        env.pop(var, None)

    proc = subprocess.Popen(
        ["node", str(TSX), str(HELPER)], cwd=str(TWIN_ROOT), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        started = read_line(proc)
        if not started:
            failures.append("the Twin harness did not start")
            print("  FAIL the Twin harness did not start")
            return
        port = int(json.loads(started)["port"])
        print(f"  ok   Twin's real adapter is listening on 127.0.0.1:{port}")

        session = FakeOnlyFansHTTP()
        adapter = OnlyFansTextAdapter(ACCOUNT, client=of_client(session))
        scope = load_scope(ENV)
        client = TwinBridgeClient(f"http://127.0.0.1:{port}", TOKEN)
        try:
            outcome = await seed_message(7001, TEXT_ONE, datetime(2026, 10, 15, 4, 0, 0))
            check("the real intake hook admits the first DM", outcome[0], "admitted")
            session._history = [
                {"id": 7001, "isFromMe": False, "fromUser": {"id": FAN_ID},
                 "createdAt": "2026-10-15T04:00:00Z"},
                {"id": 6999, "isFromMe": True, "createdAt": "2026-10-15T03:59:00Z"},
            ]

            report = await worker.run_once(now=datetime(2026, 10, 15, 4, 0, 9),
                                           scope=scope, client=client, adapter=adapter,
                                           enable_send=True)
            check("the burst freezes into one turn", report.frozen, 1)
            await worker.run_once(now=datetime(2026, 10, 15, 4, 0, 12), scope=scope,
                                  client=client, adapter=adapter, enable_send=True)

            async with get_session() as s:
                turn = (await s.execute(
                    select(TwinBridgeTurn).where(TwinBridgeTurn.account_id == ACCOUNT)
                )).scalars().first()
            check("the turn is delivered end to end", turn.state, "delivered")
            check("with the provider id from the fake transport",
                  turn.provider_message_id, "555000111")
            check("exactly one provider message POST was made", len(session.posts), 1)
            check("and it carried Twin's draft", session.posts[0][2]["text"],
                  f"<p>{REPLY}</p>")
            check("the fan-visible text is Twin's reply, not the fan's message",
                  REPLY in session.posts[0][2]["text"], True)

            outcome = await seed_message(7002, TEXT_TWO, datetime(2026, 10, 15, 4, 0, 20))
            check("the real intake hook admits the second DM", outcome[0], "admitted")
            session._history.insert(0,
                {"id": 7002, "isFromMe": False, "fromUser": {"id": FAN_ID},
                 "createdAt": "2026-10-15T04:00:20Z"})
            await worker.run_once(now=datetime(2026, 10, 15, 4, 0, 29), scope=scope,
                                  client=client, adapter=adapter, enable_send=True)
            await worker.run_once(now=datetime(2026, 10, 15, 4, 0, 32), scope=scope,
                                  client=client, adapter=adapter, enable_send=True)
            check("the second turn used the only other provider message",
                  len(session.posts), 2)
            async with get_session() as s:
                states = [t.state for t in (await s.execute(
                    select(TwinBridgeTurn).where(TwinBridgeTurn.account_id == ACCOUNT)
                )).scalars()]
            check("both turns are delivered", sorted(states), ["delivered", "delivered"])
        finally:
            await client.aclose()

        if proc.stdin is not None:
            proc.stdin.close()
        final_line = read_line(proc)
        if not final_line:
            failures.append("the Twin harness did not report its shutdown summary")
            print("  FAIL the Twin harness did not report its shutdown summary")
            return
        final = json.loads(final_line)
        check("exactly two model invocations (one per turn)", final["modelCalls"], 2)
        check("exactly one delivered transcript entry per delivered turn",
              final["assistantMessages"], 2)
        # [0, 1] is the load-bearing check: the second turn started with exactly
        # ONE committed assistant entry - the first turn's confirmed reply - and
        # the first turn started with none.
        check("the first turn committed exactly one entry, the second saw it",
              final["historyLengths"], [0, 1])
        proc.wait(timeout=30)
        check("the harness exited cleanly", proc.returncode, 0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)


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
print("all cases passed (twin bridge round trip)")
