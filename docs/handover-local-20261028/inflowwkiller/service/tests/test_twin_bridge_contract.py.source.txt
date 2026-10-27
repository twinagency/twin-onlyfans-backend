#!/usr/bin/env python3
"""Cross-language contract smoke - the Python client against the REAL Twin adapter.

Windows:  .venv\\Scripts\\python.exe service\\tests\\test_twin_bridge_contract.py

Starts Twin's actual loopback HTTP adapter (via tsx, using Twin's own factory
test seams: a neutral creator, a stubbed model, isolated temporary data/log
directories) and drives it with the Python client from `twin_bridge.client`.

Proves the two languages agree on the wire: exactly one model invocation, the
same saved request retrieved by on identity, and NO delivery acknowledgement or
assistant-history commit. Only an ephemeral server is started, and it is stopped
in `finally`.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SERVICE_DIR = TESTS_DIR.parent
REPO_ROOT = SERVICE_DIR.parent                       # .../inflowwkiller
sys.path.insert(0, str(SERVICE_DIR))

from twin_bridge.client import TwinBridgeClient  # noqa: E402

TWIN_ROOT = Path(os.environ.get("TWIN_ROOT") or (REPO_ROOT.parent / "twin-sexbot"))
HELPER = TESTS_DIR / "helpers" / "twin_bridge_server.mjs"
TSX = TWIN_ROOT / "node_modules" / "tsx" / "dist" / "cli.mjs"

TOKEN = "cross-language-token-0123456789abcdef"   # >= 32 chars, no whitespace
ACCOUNT = "acct-synthetic-1"
CREATOR = "creator-synthetic-1"
FAN = "9001"
MESSAGE = "9001"
REPLY = "neutral stub reply"
TEXT = "hello from the python client"
# Provider METADATA is a label, not an identifier: Twin's service asserts only
# that it is a nonblank string (`src/channels/service.ts`) and its HTTP
# projection keeps it verbatim, so a value with a slash must survive the round
# trip into Python unchanged.
PROVIDER = "synthetic/provider"

EVENT = {
    "schemaVersion": 1,
    "channel": "onlyfans",
    "accountId": ACCOUNT,
    "creatorSlug": CREATOR,
    "conversationId": FAN,
    "fanId": FAN,
    "messageId": MESSAGE,
    "occurredAt": "2026-10-15T04:00:00.000Z",
    "receivedAt": "2026-10-15T04:00:01.000Z",
    "direction": "inbound",
    "text": TEXT,
}

failures: list[str] = []


def check(label, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def read_line(proc: subprocess.Popen, timeout: float = 60.0) -> str | None:
    """One stdout line, or None on timeout (so a stuck child cannot hang us)."""
    box: queue.Queue = queue.Queue()

    def target() -> None:
        box.put(proc.stdout.readline())

    threading.Thread(target=target, daemon=True).start()
    try:
        line = box.get(timeout=timeout)
    except queue.Empty:
        return None
    return line or None


def main() -> None:
    print("\ncross-language contract smoke")

    if not HELPER.exists() or not TSX.exists():
        failures.append(
            f"Twin harness unavailable (helper={HELPER.exists()}, tsx={TSX.exists()}); "
            "this check was NOT executed"
        )
        print("  FAIL Twin tooling unavailable; the cross-language check did not run")
        return

    tmp = Path(tempfile.mkdtemp(prefix="twin-bridge-xlang-"))
    data_dir = tmp / "data"
    log_dir = tmp / "logs"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update({
        "TWIN_ROOT": str(TWIN_ROOT),
        "TWIN_BRIDGE_TEST_TOKEN": TOKEN,
        "TWIN_BRIDGE_TEST_ACCOUNT": ACCOUNT,
        "TWIN_BRIDGE_TEST_CREATOR": CREATOR,
        "TWIN_BRIDGE_TEST_FAN": FAN,
        "TWIN_BRIDGE_TEST_REPLY": REPLY,
        "TWIN_BRIDGE_TEST_PROVIDER": PROVIDER,
        "TWIN_BRIDGE_TEST_DATA": str(data_dir),
        "TWIN_BRIDGE_TEST_LOG": str(log_dir),
    })
    for var in ("ONLYFANS_BRIDGE_TOKEN", "ONLYFANS_BRIDGE_PORT", "ONLYFANS_ACCOUNT_BINDINGS"):
        env.pop(var, None)

    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            ["node", str(TSX), str(HELPER)],
            cwd=str(TWIN_ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        first = read_line(proc)
        if not first:
            err = ""
            if proc.poll() is not None and proc.stderr is not None:
                err = proc.stderr.read()[-400:]
            failures.append(f"the Twin harness did not start: {err}")
            print("  FAIL the Twin harness did not start")
            return

        started = json.loads(first)
        port = int(started["port"])
        print(f"  ok   Twin's real adapter is listening on 127.0.0.1:{port}")
        check("it handed out the requested token", started["token"], TOKEN)
        check("an ephemeral port was used", port > 0, True)

        client = TwinBridgeClient(f"http://127.0.0.1:{port}", TOKEN)
        draft, again, status, unknown_error = _run(_drive(client))

        check("the draft is completed and deliverable", draft.deliverable, True)
        check("the stub reply came back verbatim", draft.text, REPLY)
        check("the lifecycle is completed", draft.status, "completed")
        check("a duplicate returns the same request", again.request_id, draft.request_id)
        check("and the same text", again.text, REPLY)
        check("status retrieves the same saved request", status.request_id, draft.request_id)
        check("status is completed too", status.status, "completed")
        check("status for an unknown request is not-found", unknown_error, "not-found")
        check("a provider label containing a slash survives into Python",
              draft.usage.get("provider"), PROVIDER)
        check("and the status read preserves it as well",
              status.usage.get("provider"), PROVIDER)

        # Ask the harness to shut down and report what actually happened.
        if proc.stdin is not None:
            proc.stdin.close()
        final_line = read_line(proc)
        if not final_line:
            failures.append("the Twin harness did not report its shutdown summary")
            print("  FAIL the Twin harness did not report its shutdown summary")
            return
        final = json.loads(final_line)
        check("exactly one model invocation", final["modelCalls"], 1)
        check("no assistant history was committed (no delivery acknowledgement)",
              final["assistantMessages"], 0)
        proc.wait(timeout=30)
        check("the harness exited cleanly", proc.returncode, 0)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)


def _run(coro):
    import asyncio

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


async def _drive(client):
    """One scripted conversation against the live adapter; returns the results."""
    try:
        draft = await client.request_draft(EVENT)
        again = await client.request_draft(EVENT)
        status = await client.read_status(EVENT)
        try:
            await client.read_status(dict(EVENT, messageId="9002"))
            unknown_error = None
        except Exception as e:  # noqa: BLE001 - the category is what matters
            unknown_error = getattr(e, "category", type(e).__name__)
        return draft, again, status, unknown_error
    finally:
        await client.aclose()


main()

print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("all cases passed (twin bridge cross-language contract)")
