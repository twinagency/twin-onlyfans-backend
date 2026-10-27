#!/usr/bin/env python3
"""Twin bridge intake tests - plain asserts, no pytest, no network, no live data.

Windows:  set TEMP to a writable folder first, then
          .venv\\Scripts\\python.exe service\\tests\\test_twin_bridge_intake.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import types
from datetime import datetime
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SERVICE_DIR = TESTS_DIR.parent
sys.path.insert(0, str(SERVICE_DIR))

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="twin-bridge-intake-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP_ROOT / 'bridge.db').as_posix()}"
for _var in (
    "TWIN_BRIDGE_ENABLED",
    "TWIN_BRIDGE_ACCOUNT_ID",
    "TWIN_BRIDGE_CREATOR_SLUG",
    "TWIN_BRIDGE_FAN_ALLOWLIST",
    "TWIN_BRIDGE_ACTIVATION_CUTOFF",
):
    os.environ.pop(_var, None)

_relay_cache = types.ModuleType("relay_cache")
_relay_cache.invalidate = lambda *a, **k: None
_relay_cache.invalidate_money_feeds = lambda *a, **k: None
sys.modules["relay_cache"] = _relay_cache

_webhook = types.ModuleType("webhook_dispatch")


async def _noop_hook(*_a, **_k):
    return None


_webhook.on_inbound_message = _noop_hook
_webhook.on_inbound_tip = _noop_hook
_webhook.on_inbound_image = _noop_hook
sys.modules["webhook_dispatch"] = _webhook

# `event_transcoder` also reaches into `automations._common` for one on-conflict
# helper. The real module imports `llm_client` -> `llm_models`, which cannot be
# imported on Python 3.11 at all (it defaults a dataclass field to an unhashable
# MappingProxyType; CPython only made mappingproxy hashable in 3.12+). The
# assignment authorises stubbing the transcoder's automation hooks.
_automations = types.ModuleType("automations")
_automations.__path__ = []
_automations_common = types.ModuleType("automations._common")


def _source_self_heal_set(subscribed_on, subscribed_by) -> dict:
    return {"source": "fan"} if subscribed_on else {}


_automations_common.source_self_heal_set = _source_self_heal_set
_automations._common = _automations_common
sys.modules["automations"] = _automations
sys.modules["automations._common"] = _automations_common

from sqlalchemy import create_engine as _sa_create_engine  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy import text as _sa_text  # noqa: E402

from db.engine import engine, get_session, init_db  # noqa: E402
from db.models import Account, Message  # noqa: E402
from db.onlyfans_pilot_models import TwinBridgeInput  # noqa: E402
from twin_bridge import contract, normalize  # noqa: E402
from twin_bridge.errors import ContractError, IntakeConflictError  # noqa: E402
from twin_bridge.intake import enqueue_inbound_if_enabled, fetch_row_view  # noqa: E402
from twin_bridge.reconcile import reconcile_pending  # noqa: E402
from twin_bridge.scope import BridgeScope, load_scope  # noqa: E402
import event_transcoder  # noqa: E402

ACCOUNT = "acct-synthetic-1"
OTHER_ACCOUNT = "acct-synthetic-2"
CREATOR = "creator-synthetic-1"
FAN = "9001"
OTHER_FAN = "9002"
CUTOFF = "2026-10-15T00:00:00Z"

ENV = {
    "TWIN_BRIDGE_ENABLED": "1",
    "TWIN_BRIDGE_ACCOUNT_ID": ACCOUNT,
    "TWIN_BRIDGE_CREATOR_SLUG": CREATOR,
    "TWIN_BRIDGE_FAN_ALLOWLIST": FAN,
    "TWIN_BRIDGE_ACTIVATION_CUTOFF": CUTOFF,
}

failures: list[str] = []


def scope(**over) -> BridgeScope:
    env = dict(ENV)
    env.update(over)
    return load_scope(env)


def check(label, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def check_true(label, got) -> None:
    check(label, bool(got), True)


def _raises(fn) -> bool:
    try:
        fn()
    except ContractError:
        return True
    return False


sys.setrecursionlimit(50_000)
threading.stack_size(64 * 1024 * 1024)


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


def set_env(**over):
    for k, v in over.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ── fixtures ──────────────────────────────────────────────────────────

_T0 = datetime(2026, 10, 15, 4, 0, 0)      # provider time
_T1 = datetime(2026, 10, 15, 4, 0, 1)      # Fastt receipt


def frame_for(message_id, fan_id, *, text="<p>hey there</p>", created="2026-10-15T04:00:00+00:00", **over):
    """A representative provider frame - the evidence the bridge decides on."""
    payload = {
        "id": message_id,
        "fromUser": {"id": fan_id},
        "text": text,
        "createdAt": created,
        "responseType": "message",
        "lockedText": False,
        "media": [],
        "mediaCount": 0,
        "price": 0,
        "isTip": False,
    }
    payload.update(over)
    return payload


async def add_message(account_id, fan_id, message_id, *, frame=None, created_at=_T0,
                      ingested_at=_T1, raw_json=None, **row_over):
    """Persist a canonical message row plus the frame that produced it."""
    payload = frame if frame is not None else frame_for(message_id, fan_id)
    fields = dict(
        account_id=account_id, fan_id=fan_id, message_id=message_id,
        direction="in", body="<p>hey there</p>", media_ids="[]", media_count=0,
        price_cents=0, is_paid=None, is_tip=False, is_unsent=False,
        created_at=created_at, ingested_at=ingested_at,
        raw_json=raw_json if raw_json is not None else json.dumps(payload),
    )
    fields.update(row_over)
    async with get_session() as s:
        s.add(Message(**fields))


async def add_account(account_id):
    """`fans` and `chats` FK to `accounts.id`, so the account must exist."""
    async with get_session() as s:
        s.add(Account(id=account_id, nickname="synthetic"))


async def enqueue(account_id, fan_id, message_id, *, frame=None, sc=None):
    async with get_session() as s:
        return await enqueue_inbound_if_enabled(
            s, account_id=account_id, fan_id=fan_id, message_id=message_id,
            frame=frame, scope=sc if sc is not None else scope(),
        )


async def bridge_rows():
    async with get_session() as s:
        return (await s.execute(select(TwinBridgeInput))).scalars().all()


async def _messages(message_id):
    async with get_session() as s:
        return (await s.execute(select(Message).where(Message.message_id == message_id))).scalars().all()


async def transcode(frame, account_id=ACCOUNT):
    """Run the REAL transcoder against whatever scope is currently configured."""
    await event_transcoder._transcode_chat_message(account_id, frame)


# ── R1: trusted scope -------------------------------------------------

print("\ntrusted scope")
check("an empty environment is disabled", load_scope({}).enabled, False)
check("a disabled flag with full config is still disabled",
      load_scope({**ENV, "TWIN_BRIDGE_ENABLED": "0"}).enabled, False)
partial = load_scope({"TWIN_BRIDGE_ENABLED": "1", "TWIN_BRIDGE_ACCOUNT_ID": ACCOUNT})
check("a missing creator/allowlist/cutoff is disabled, not defaulted", partial.enabled, False)
check_true("and the reason names the missing cutoff",
           any("ACTIVATION_CUTOFF" in p for p in partial.problems))
check("a naive cutoff is refused (no timezone)",
      load_scope({**ENV, "TWIN_BRIDGE_ACTIVATION_CUTOFF": "2026-10-15T00:00:00"}).enabled, False)
check("an impossible cutoff date is refused",
      load_scope({**ENV, "TWIN_BRIDGE_ACTIVATION_CUTOFF": "2026-02-30T00:00:00Z"}).enabled, False)
ok_scope = scope()
check("a complete scope is enabled", ok_scope.enabled, True)
check("the cutoff is the configured instant, never now",
      ok_scope.activation_cutoff, datetime(2026, 10, 15, 0, 0, 0))
check("the allowlisted fan is covered", ok_scope.covers(ACCOUNT, FAN), True)
check("another account is not covered", ok_scope.covers(OTHER_ACCOUNT, FAN), False)
check("another fan is not covered", ok_scope.covers(ACCOUNT, OTHER_FAN), False)
check("a wildcard fan is never accepted", ok_scope.covers(ACCOUNT, "*"), False)

# R1: the allowlist is validated against the SQL it actually feeds.
check("leading zeros normalise to the same fan", scope(TWIN_BRIDGE_FAN_ALLOWLIST="09001").covers(ACCOUNT, 9001), True)
check("a non-numeric fan disables scope", scope(TWIN_BRIDGE_FAN_ALLOWLIST="abc").enabled, False)
check_true("and says why",
           any("decimal fan id" in p for p in scope(TWIN_BRIDGE_FAN_ALLOWLIST="abc").problems))
check("a fractional fan disables scope", scope(TWIN_BRIDGE_FAN_ALLOWLIST="9001.5").enabled, False)
check("a year-zero cutoff disables scope instead of raising",
      scope(TWIN_BRIDGE_ACTIVATION_CUTOFF="0000-01-01T00:00:00.000Z").enabled, False)
check("an offset-underflowing cutoff disables scope instead of raising",
      scope(TWIN_BRIDGE_ACTIVATION_CUTOFF="0001-01-01T00:00:00+01:00").enabled, False)
check("an out-of-range fan disables scope", scope(TWIN_BRIDGE_FAN_ALLOWLIST="99999999999999999999").enabled, False)
check("a valid list still converts for SQL", scope(TWIN_BRIDGE_FAN_ALLOWLIST="9001,09002").fan_ids_for_sql(), (9001, 9002))
check("a disabled scope never reaches int()", load_scope({"TWIN_BRIDGE_ENABLED": "1", "TWIN_BRIDGE_FAN_ALLOWLIST": "abc"}).fan_ids_for_sql(), ())

# ── R1/R3-style contract helpers --------------------------------------

print("\nstrict typing")
check("a bool is not an integer", _raises(lambda: contract.strict_int(True, "epoch")), True)
check("a float is not an integer", _raises(lambda: contract.strict_int(1.0, "epoch")), True)
check("a negative integer is refused when bounded", _raises(lambda: contract.strict_int(-1, "epoch", minimum=0)), True)
check("a bool is not a number", _raises(lambda: contract.strict_number(True, "cost")), True)
check("a negative cost is refused", _raises(lambda: contract.strict_number(-1, "cost", minimum=0)), True)
check("a provider id must be an integer", _raises(lambda: contract.provider_id(1002.9, "id")), True)
check("a boolean provider id is refused", _raises(lambda: contract.provider_id(True, "id")), True)
check("a string provider id is refused", _raises(lambda: contract.provider_id("1002", "id")), True)
check("a zero provider id is refused", _raises(lambda: contract.provider_id(0, "id")), True)
check("a large integer id survives exactly", contract.provider_id(9007199254740993, "id"), 9007199254740993)
check("a canonical stamp is recognised", contract.is_canonical_timestamp("2026-10-15T04:00:00.000Z"), True)
check("a junk stamp is not", contract.is_canonical_timestamp("invalid-date"), False)
check("a null stamp is not", contract.is_canonical_timestamp(None), False)

# ── normalization + shared key fixtures ───────────────────────────────

print("\nnormalization")
check("tags are stripped and entities decoded",
      normalize.html_to_text("<p>hi &amp; bye</p>"), "hi & bye")
check("a literal entity is text, not markup",
      normalize.html_to_text("<p>i said &lt;b&gt; no</p>"), "i said <b> no")
check("br becomes a line break", normalize.html_to_text("a<br>b<br/>c"), "a\nb\nc")
check("block ends become line breaks", normalize.html_to_text("<p>one</p><p>two</p>"), "one\ntwo")
check("blank runs collapse", normalize.html_to_text("<p>a</p><p></p><p></p><p>b</p>"), "a\n\nb")
check("nbsp becomes a space", normalize.html_to_text("<p>a&nbsp;b</p>"), "a b")
check("an empty body normalizes to empty", normalize.html_to_text("<p></p>"), "")
check("a None body normalizes to empty", normalize.html_to_text(None), "")

check("utf16_len counts a non-BMP char as two units", contract.utf16_len("\U0001F600"), 2)
check("a lone high surrogate is detected", contract.has_unpaired_surrogate("\ud800"), True)
check("a valid surrogate pair is fine", contract.has_unpaired_surrogate("\U0001F600"), False)
check("a bool id is refused (never coerced)", _raises(lambda: contract.validate_id(True, "fanId")), True)
check("a padded id is refused", _raises(lambda: contract.validate_id(" x ", "fanId")), True)
check("text over 4000 UTF-16 units is refused", _raises(lambda: contract.validate_text("a" * 4001)), True)
check("an emoji-only 2001-char text is over the limit",
      _raises(lambda: contract.validate_text("\U0001F600" * 2001)), True)
check("a 2000-emoji text is within the limit",
      contract.validate_text("\U0001F600" * 2000) == "\U0001F600" * 2000, True)

check("a +02:00 timestamp converts to UTC",
      contract.canonical_timestamp(normalize.provider_timestamp_to_utc("2026-10-15T12:00:00+02:00")),
      "2026-10-15T10:00:00.000Z")
check("a negative offset converts to UTC",
      contract.canonical_timestamp(normalize.provider_timestamp_to_utc("2026-10-15T02:00:00-05:00")),
      "2026-10-15T07:00:00.000Z")
check("a provider timestamp with no offset is refused",
      normalize.provider_timestamp_to_utc("2026-10-15T04:00:00"), None)
check("an impossible calendar date is refused",
      normalize.provider_timestamp_to_utc("2026-02-30T00:00:00+00:00"), None)
check("a fractional second keeps millisecond precision",
      contract.canonical_timestamp(contract.parse_canonical_timestamp("2026-10-15T04:00:00.123456Z", "t")),
      "2026-10-15T04:00:00.123Z")
check("a stored naive datetime is read as UTC",
      normalize.stored_utc(datetime(2026, 10, 15, 4, 0, 1)), datetime(2026, 10, 15, 4, 0, 1))
check("a stored junk stamp is refused", normalize.stored_utc("invalid-date"), None)
check("a stored ISO string without an offset is read as UTC",
      normalize.stored_utc("2026-10-15 04:00:01.000000"), datetime(2026, 10, 15, 4, 0, 1))

print("\nshared key fixtures (must match Twin's own hashes)")
check("conversationKey matches Twin",
      contract.conversation_key("onlyfans", ACCOUNT, CREATOR, "fan-synthetic-1", "fan-synthetic-1"),
      "of_26af976e69b8e73d61a1fbd336e73f21e38654c52bb5e09998c2ffdd1d52b487")
check("messageKey matches Twin",
      contract.message_key("onlyfans", ACCOUNT, "fan-synthetic-1", "msg-synthetic-1"),
      "ofm_6e4fb12f9795411544a39ce9fb8727e3ec1ab1ab6d2b1f17770fbf93d3310e78")
check("an id above 2**53 still hashes as a string",
      contract.conversation_key("onlyfans", ACCOUNT, CREATOR, "9007199254740993", "9007199254740993"),
      "of_bf6ff559fe0d78e4509ada995bc37c3672f2f51c7a655108dcc9e2675e029a7f")
check("and its messageKey too",
      contract.message_key("onlyfans", ACCOUNT, "9007199254740993", "9007199254740993"),
      "ofm_559b65f54d08d536c4331d234592685bfe3f9e2051e4e5f9c293cb9c56e45a22")
check("a non-BMP id hashes like JavaScript",
      contract.conversation_key("onlyfans", "acct-\U0001F600", "creator-\U0001F600", "conv-\U0001F600", "fan-\U0001F600"),
      "of_fc0fa05d3669d4ea06f92d3725e5d0a99d951202399d7204e083664df2a5f789")
check("a pipe inside a field cannot alias another tuple",
      contract.conversation_key("onlyfans", "a|b", "c", "d", "e"),
      "of_5499b4b18dc9d3534157f52c7767071292f626a0371de1bf8092a98d43333186")


async def evidence_1_and_2():
    await init_db()
    await add_account(ACCOUNT)
    await add_account(OTHER_ACCOUNT)

    # ── disabled: ordinary ingestion is unchanged ─────────────────────
    print("\nexcluded categories (no bridge work)")
    set_env(TWIN_BRIDGE_ENABLED="0")
    await transcode(frame_for(5001, 9001, text="<p>disabled path</p>"))
    check("disabled ingestion still persists the message", len(await _messages(5001)), 1)
    check("but creates no bridge work", len(await bridge_rows()), 0)
    set_env(**ENV)  # back to the enabled scope for every case below

    # ── every excluded shape, with a representative frame each ────────
    excluded = [
        ("outbound echo", dict(direction="out"), {}),
        ("a tip", dict(is_tip=True), dict(isTip=True)),
        ("a priced message", dict(price_cents=900), dict(price=9)),
        ("an unlocked PPV", dict(is_paid=True), {}),
        ("media even if captioned", dict(media_count=1, media_ids="[77]"),
         dict(media=[{"id": 77}], mediaCount=1)),
        ("an unsent message", dict(is_unsent=True), {}),
        ("empty text", dict(body=""), dict(text="")),
        ("markup-only text", dict(body="<p></p>"), dict(text="<p></p>")),
        ("oversized text", dict(body="a" * 4001), dict(text="a" * 4001)),
        ("locked content", {}, dict(lockedText=True)),
        ("a reaction shape", {}, dict(responseType="reaction")),
        ("a system shape", {}, dict(responseType="system")),
        ("a frame with an unknown shape", {}, dict(responseType="something-new")),
        ("a bare Giphy GIF", dict(body=""), dict(giphyId="synthetic-gif", media=[], mediaCount=0)),
        ("a captioned Giphy GIF", {}, dict(giphyId="synthetic-gif", media=[], mediaCount=0)),
    ]
    for i, (label, row_over, frame_over) in enumerate(excluded):
        mid = 6000 + i
        await add_message(ACCOUNT, 9001, mid,
                          frame=frame_for(mid, 9001, **frame_over), **row_over)
        outcome, reason = await enqueue(ACCOUNT, 9001, mid)
        check(f"{label} is not admitted", outcome, "ineligible")
        check_true(f"{label} names a reason", bool(reason))
    check("no excluded row reached the table", len(await bridge_rows()), 0)

    # A row with NO readable provider frame is refused rather than guessed at.
    await add_message(ACCOUNT, 9001, 6100, raw_json="not json at all")
    check("an unreadable frame is refused",
          (await enqueue(ACCOUNT, 9001, 6100))[1], "missing-provider-evidence")

    now_scope = scope()

    class _Row:
        account_id, fan_id, message_id = ACCOUNT, 9001, 6300
        direction, body, media_ids, media_count = "in", "<p>hi</p>", "[]", 0
        price_cents, is_paid, is_tip, is_unsent = 0, None, False, False
        ingested_at = _T1
        raw_json = None

    no_time = normalize.build_candidate(_Row(), evidence=normalize.frame_evidence(frame_for(6300, 9001, created=None)))
    check("a missing provider timestamp is ineligible",
          normalize.eligibility_problem(no_time, now_scope), "missing-provider-timestamp")

    class _NoReceipt(_Row):
        ingested_at = None

    no_receipt = normalize.build_candidate(_NoReceipt(), evidence=normalize.frame_evidence(frame_for(6300, 9001)))
    check("a missing receipt is ineligible",
          normalize.eligibility_problem(no_receipt, now_scope), "missing-receipt")

    class _EarlyReceipt(_Row):
        ingested_at = datetime(2026, 10, 14, 23)

    early = normalize.build_candidate(_EarlyReceipt(), evidence=normalize.frame_evidence(frame_for(6300, 9001)))
    check("a receipt before the cutoff is ineligible",
          normalize.eligibility_problem(early, now_scope), "receipt-before-cutoff")

    offset = normalize.build_candidate(_Row(), evidence=normalize.frame_evidence(
        frame_for(6300, 9001, created="2026-10-15T04:00:00+02:00")))
    check("a raw provider offset is converted, not stripped",
          normalize.build_event(offset, now_scope)["occurredAt"], "2026-10-15T02:00:00.000Z")

    no_evidence = normalize.build_candidate(_Row(), evidence=None)
    check("a candidate with no evidence at all is ineligible",
          normalize.eligibility_problem(no_evidence, now_scope), "missing-provider-evidence")

    # Unknown account / fan are outside the designated scope.
    await add_message(OTHER_ACCOUNT, 9001, 6200)
    check("an unknown account is refused", (await enqueue(OTHER_ACCOUNT, 9001, 6200))[0], "ineligible")
    await add_message(ACCOUNT, 9999, 6201)
    check("a non-allowlisted fan is refused", (await enqueue(ACCOUNT, 9999, 6201))[0], "ineligible")
    check("still nothing admitted", len(await bridge_rows()), 0)

    # ── R1: the REAL transcoder must reject lossy or excluded input ───
    print("\nR1: original evidence through the transcoder")
    await transcode(frame_for(1002.9, 9001))
    check("a float message id is not admitted",
          [r for r in await bridge_rows() if r.provider_message_id == 1002], [])
    await transcode(frame_for(True, 9001))
    check("a boolean message id is not admitted",
          [r for r in await bridge_rows() if r.provider_message_id == 1], [])
    await transcode(frame_for(1100, 9001.5))
    check("a fractional fan id does not alias the allowlisted fan",
          [r for r in await bridge_rows() if r.provider_message_id == 1100], [])
    await transcode(frame_for(1200, 9001, lockedText=True))
    check("locked content is not admitted",
          [r for r in await bridge_rows() if r.provider_message_id == 1200], [])
    await transcode(frame_for(1300, 9001, responseType="reaction"))
    check("a reaction shape is not admitted",
          [r for r in await bridge_rows() if r.provider_message_id == 1300], [])
    await transcode(frame_for(1400, 9001, created=None))
    check("a missing provider timestamp is not admitted",
          [r for r in await bridge_rows() if r.provider_message_id == 1400], [])
    check("none of the rejected transcoder input reached the table", len(await bridge_rows()), 0)

    # ── one immutable pending row from the canonical transaction ──────
    print("\nadmission")
    await transcode(frame_for(7001, 9001, text="<p>hello &amp; welcome</p>"))
    rows = await bridge_rows()
    check("the canonical transaction produced exactly one input", len(rows), 1)
    row = rows[0]
    check("it is pending", row.state, "pending")
    check("it keeps the frozen creator", row.creator_slug, CREATOR)
    check("it keeps the frozen cutoff", row.activation_cutoff, datetime(2026, 10, 15, 0, 0, 0))
    snapshot = json.loads(row.event_json)
    check("the snapshot is Twin's version-1 event", snapshot["schemaVersion"], 1)
    check("conversationId is the fan id for this chat shape", snapshot["conversationId"], "9001")
    check("accountId is the Fastt account, kept separate", snapshot["accountId"], ACCOUNT)
    check("provider offset converted to UTC", snapshot["occurredAt"], "2026-10-15T04:00:00.000Z")
    stored_receipt = (await _messages(7001))[0].ingested_at
    check("receivedAt is Fastt's first receipt (the persisted ingested_at)",
          snapshot["receivedAt"], contract.canonical_timestamp(stored_receipt))
    check("HTML was normalized for the model", snapshot["text"], "hello & welcome")

    check("a repeat admission is a duplicate", (await enqueue(ACCOUNT, 9001, 7001))[0], "duplicate")
    await asyncio.gather(*[enqueue(ACCOUNT, 9001, 7001) for _ in range(5)])
    check("racing duplicates still leave one row", len(await bridge_rows()), 1)

    # A later edit of the canonical row must not rewrite the frozen snapshot,
    # and a CONTRADICTORY same-id payload must be an explicit conflict.
    await add_message(ACCOUNT, 9001, 7002, frame=frame_for(7002, 9001, text="<p>first text</p>"))
    await enqueue(ACCOUNT, 9001, 7002)
    async with get_session() as s:
        existing = await s.get(Message, (ACCOUNT, 9001, 7002))
        existing.body = "<p>edited much later</p>"
    frozen = [r for r in await bridge_rows() if r.provider_message_id == 7002][0]
    check("an edited canonical row cannot rewrite the snapshot",
          json.loads(frozen.event_json)["text"], "first text")
    async with get_session() as s:
        await s.execute(_sa_text(
            "UPDATE messages SET raw_json=:r WHERE account_id=:a AND fan_id=9001 AND message_id=7002"
        ), {"r": json.dumps(frame_for(7002, 9001, text="<p>changed payload</p>")), "a": ACCOUNT})
    conflict = False
    try:
        await enqueue(ACCOUNT, 9001, 7002)
    except IntakeConflictError:
        conflict = True
    check("and a contradictory replay is an explicit conflict, never new work", conflict, True)
    check("the snapshot still holds the original text",
          json.loads([r for r in await bridge_rows() if r.provider_message_id == 7002][0].event_json)["text"],
          "first text")

    # Two accounts with the same fan/message ids stay separate.
    set_env(TWIN_BRIDGE_ACCOUNT_ID=OTHER_ACCOUNT)
    await add_message(OTHER_ACCOUNT, 9001, 7001, frame=frame_for(7001, 9001, text="<p>other account</p>"))
    other_scope = scope(TWIN_BRIDGE_ACCOUNT_ID=OTHER_ACCOUNT)
    check("the other account admits its own row",
          (await enqueue(OTHER_ACCOUNT, 9001, 7001, sc=other_scope))[0], "admitted")
    set_env(**ENV)
    both = [r for r in await bridge_rows() if r.provider_message_id == 7001]
    check("same ids in two accounts are two rows", len(both), 2)
    check("and they carry different accounts", {r.account_id for r in both}, {ACCOUNT, OTHER_ACCOUNT})


async def evidence_3():
    print("\natomicity + reconciliation")
    set_env(**ENV)

    # A failure while publishing the intake row must roll the WHOLE transaction
    # back: neither the new message nor a partial input may remain.
    original = event_transcoder._enqueue_bridge_input

    async def _boom(*_a, **_k):
        raise RuntimeError("simulated intake write failure")

    event_transcoder._enqueue_bridge_input = _boom
    crashed = False
    try:
        await event_transcoder._transcode_chat_message(ACCOUNT, frame_for(8001, 9001, text="<p>doomed</p>"))
    except RuntimeError:
        crashed = True
    finally:
        event_transcoder._enqueue_bridge_input = original
    check("the intake failure propagated (not swallowed)", crashed, True)
    check("the new message was rolled back", len(await _messages(8001)), 0)
    check("and no bridge input survived",
          [r for r in await bridge_rows() if r.provider_message_id == 8001], [])

    # ── R1: hook and reconciliation agree on the SAME evidence ────────
    set_env(TWIN_BRIDGE_ACCOUNT_ID=OTHER_ACCOUNT)
    other = scope(TWIN_BRIDGE_ACCOUNT_ID=OTHER_ACCOUNT, TWIN_BRIDGE_FAN_ALLOWLIST="9002")
    gif_frame = frame_for(2001, 9002, text="", giphyId="synthetic-gif", media=[], mediaCount=0)
    await add_message(OTHER_ACCOUNT, 9002, 2001, frame=gif_frame)
    hook_outcome, hook_reason = await enqueue(OTHER_ACCOUNT, 9002, 2001, frame=gif_frame, sc=other)
    check("the hook refuses a GIF", (hook_outcome, hook_reason), ("ineligible", "gif"))
    gif_scan = await reconcile_pending(scope=other)
    check("and the reconciliation refuses the SAME row", gif_scan.admitted, 0)
    check("so the GIF never became work",
          [r for r in await bridge_rows() if r.provider_message_id == 2001], [])

    # ── R2: a converted offset time must not be pre-filtered out ──────
    await add_message(OTHER_ACCOUNT, 9003, 3001,
                      frame=frame_for(3001, 9003, created="2026-10-14T23:00:00-05:00"),
                      created_at=datetime(2026, 10, 14, 23), ingested_at=datetime(2026, 10, 15, 5))
    offset_scope = scope(TWIN_BRIDGE_ACCOUNT_ID=OTHER_ACCOUNT, TWIN_BRIDGE_FAN_ALLOWLIST="9003")
    recovered = await reconcile_pending(scope=offset_scope)
    check("a -05:00 provider time after the cutoff is admitted", recovered.admitted, 1)
    check("and its frozen occurredAt is the converted UTC instant",
          json.loads([r for r in await bridge_rows() if r.provider_message_id == 3001][0].event_json)["occurredAt"],
          "2026-10-15T04:00:00.000Z")

    # The OTHER boundary still applies: a receipt before the cutoff is refused.
    await add_message(OTHER_ACCOUNT, 9004, 4001, frame=frame_for(4001, 9004),
                      ingested_at=datetime(2026, 10, 14, 23))
    receipt_scope = scope(TWIN_BRIDGE_ACCOUNT_ID=OTHER_ACCOUNT, TWIN_BRIDGE_FAN_ALLOWLIST="9004")
    recovered = await reconcile_pending(scope=receipt_scope)
    check("a receipt before the cutoff is refused", recovered.admitted, 0)
    set_env(**ENV)

    # ── R2: dirty timestamps do not abort a page or strand progress ───
    dirty_scope = scope(TWIN_BRIDGE_FAN_ALLOWLIST="9005")
    await add_message(ACCOUNT, 9005, 5001, frame=frame_for(5001, 9005))
    async with get_session() as s:
        # The only way a DateTime column can hold junk: written around the ORM,
        # exactly as the review's probe does.
        await s.execute(_sa_text("UPDATE messages SET created_at='invalid-date' WHERE fan_id=9005"))
    recovered = await reconcile_pending(scope=dirty_scope)
    check("a dirty created_at does not abort the page", recovered.admitted, 1)

    # A dirty ingested_at must be refused WITHOUT stranding the page: a row that
    # sorts after it must still be reached, one page at a time.
    await add_message(ACCOUNT, 9006, 5002, frame=frame_for(5002, 9006), ingested_at=datetime(2026, 10, 15, 4))
    await add_message(ACCOUNT, 9006, 5003, frame=frame_for(5003, 9006), ingested_at=datetime(2026, 10, 15, 6))
    async with get_session() as s:
        await s.execute(_sa_text(
            "UPDATE messages SET ingested_at='2026-10-15 05:00:00.zzz' "
            "WHERE fan_id=9006 AND message_id=5002"
        ))
    dirty_scan_scope = scope(TWIN_BRIDGE_FAN_ALLOWLIST="9006")
    admitted_ids: set[int] = set()
    refused = 0
    cursor = None
    guard = 0
    while guard < 20:
        report = await reconcile_pending(scope=dirty_scan_scope, batch_size=1, after=cursor)
        admitted_ids |= {r.provider_message_id for r in await bridge_rows()
                         if r.account_id == ACCOUNT and r.fan_id == 9006}
        refused += report.ineligible
        cursor = report.next_cursor
        guard += 1
        if report.done:
            break
    check("paging terminated well inside the guard", guard < 20, True)
    check("a dirty ingested_at did not strand the page", admitted_ids, {5003})
    check("the dirty row was refused rather than admitted", refused >= 1, True)

    # ── P3: a near-the-edge receipt string must not raise ──────────────
    # `9999-12-31T23:59:59-01:00` normalises past the representable range, so
    # converting it raises OverflowError. The string branch of `stored_utc` must
    # refuse the cell instead of aborting the page, and a later valid row must
    # still be reached.
    check("an overflowing receipt string is refused",
          normalize.stored_utc("9999-12-31T23:59:59-01:00"), None)
    check("a year-zero receipt string is refused",
          normalize.stored_utc("0000-01-01T00:00:00+00:00"), None)
    check("an ordinary offset receipt still converts",
          normalize.stored_utc("2026-10-15T04:00:00+00:00"),
          datetime(2026, 10, 15, 4, 0, 0))
    check("and a naive one is still read as UTC",
          normalize.stored_utc("2026-10-15 04:00:00"), datetime(2026, 10, 15, 4, 0, 0))

    await add_message(ACCOUNT, 9008, 7001, frame=frame_for(7001, 9008),
                      ingested_at=datetime(2026, 10, 15, 5))
    await add_message(ACCOUNT, 9008, 7002, frame=frame_for(7002, 9008),
                      ingested_at=datetime(2026, 10, 15, 6))
    async with get_session() as s:
        await s.execute(_sa_text(
            "UPDATE messages SET ingested_at='9999-12-31T23:59:59-01:00' "
            "WHERE fan_id=9008 AND message_id=7001"
        ))
    overflow_scope = scope(TWIN_BRIDGE_FAN_ALLOWLIST="9008")
    seen_ids: set[int] = set()
    cursor = None
    guard = 0
    while guard < 20:
        report = await reconcile_pending(scope=overflow_scope, batch_size=1, after=cursor)
        seen_ids |= {r.provider_message_id for r in await bridge_rows()
                     if r.account_id == ACCOUNT and r.fan_id == 9008}
        cursor = report.next_cursor
        guard += 1
        if report.done:
            break
    check("an overflowing receipt does not abort the scan", guard < 20, True)
    check("and the following valid row is still reached", seen_ids, {7002})

    # ── R2: ties in the ordering still advance deterministically ──────
    same = datetime(2026, 10, 15, 7, 0, 0)
    tie_scope = scope(TWIN_BRIDGE_FAN_ALLOWLIST="9007")
    for mid in (6001, 6002, 6003):
        await add_message(ACCOUNT, 9007, mid, frame=frame_for(mid, 9007), ingested_at=same)
    seen: set[int] = set()
    cursor = None
    guard = 0
    while guard < 10:
        report = await reconcile_pending(scope=tie_scope, batch_size=1, after=cursor)
        seen |= {r.provider_message_id for r in await bridge_rows()
                 if r.account_id == ACCOUNT and r.fan_id == 9007}
        cursor = report.next_cursor
        guard += 1
        if report.done:
            break
    check("equal timestamps do not stall paging", seen, {6001, 6002, 6003})

    # ── bounded, idempotent, racing-safe ──────────────────────────────
    # Drain the primary scope first: 5001 was persisted while the bridge was
    # DISABLED, so reconciliation must restore exactly that committed input.
    cursor = None
    for _ in range(10):
        report = await reconcile_pending(scope=scope(), batch_size=100, after=cursor)
        cursor = report.next_cursor
        if report.done:
            break
    restored = {r.provider_message_id for r in await bridge_rows()
                if r.account_id == ACCOUNT and r.fan_id == 9001}
    check("reconciliation restored the row written while disabled", 5001 in restored, True)

    before = len(await bridge_rows())
    again = await reconcile_pending(scope=scope(), batch_size=100)
    check("a repeated scan admits nothing", again.admitted, 0)
    check("and adds no rows", len(await bridge_rows()), before)
    races = await asyncio.gather(*[reconcile_pending(scope=scope(), batch_size=100) for _ in range(3)])
    check("racing scans stay deduplicated", len(await bridge_rows()), before)
    check("and report no conflicts", sum(r.conflicts for r in races), 0)
    check("a batch size above the cap is still bounded",
          (await reconcile_pending(scope=scope(), batch_size=10_000)).scanned <= 500, True)
    check("a disabled scope reconciles nothing",
          (await reconcile_pending(scope=BridgeScope(enabled=False), batch_size=10)).done, True)


def _py(script: str, db_url: str) -> tuple[int, str, str]:
    """Run a snippet in a fresh interpreter against its own SQLite file."""
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("TWIN_BRIDGE_")}
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{Path(db_url).as_posix()}"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", script], env=env, cwd=str(SERVICE_DIR),
        capture_output=True, text=True, timeout=120,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _json_line(out: str) -> dict:
    """The last JSON object the child printed (its own logs may follow on stderr)."""
    for line in reversed(out.strip().splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return {}


_READ_SCRIPT = """
import asyncio, json, sys, threading
sys.setrecursionlimit(50000)
threading.stack_size(64 * 1024 * 1024)
from sqlalchemy import select
from db.engine import get_session
from db.onlyfans_pilot_models import TwinBridgeInput

async def main():
    async with get_session() as s:
        rows = (await s.execute(select(TwinBridgeInput))).scalars().all()
    hit = [r for r in rows if r.provider_message_id == 7001 and r.account_id == 'acct-synthetic-1']
    print(json.dumps({
        "count": len(rows),
        "text": json.loads(hit[0].event_json)["text"] if hit else None,
        "state": hit[0].state if hit else None,
        "creator": hit[0].creator_slug if hit else None,
    }))

_t = threading.Thread(target=lambda: asyncio.run(main()))
_t.start(); _t.join()
"""

_UPGRADE_SCRIPT = """
import asyncio, json, sys, threading
sys.setrecursionlimit(50000)
threading.stack_size(64 * 1024 * 1024)
from db.engine import engine, get_session, init_db
from sqlalchemy import text

async def main():
    await init_db()
    await init_db()  # repeatable
    async with get_session() as s:
        tables = (await s.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"))).scalars().all()
        legacy = (await s.execute(text(
            "SELECT body FROM messages WHERE message_id = 1"))).scalars().all()
    print(json.dumps({"has_bridge": "twin_bridge_inputs" in tables, "legacy": legacy}))

_t = threading.Thread(target=lambda: asyncio.run(main()))
_t.start(); _t.join()
"""


async def evidence_4():
    print("\nrestart visibility + schema upgrade")
    await engine.dispose()  # release the file before another interpreter reads it

    db_path = _TMP_ROOT / "bridge.db"
    rc, out, err = _py(_READ_SCRIPT, str(db_path))
    check("a fresh process starts cleanly", rc, 0)
    payload = _json_line(out) if rc == 0 else {}
    if rc != 0:
        print("    child stderr:", err.strip()[:300])
    check("it sees the same frozen snapshot", payload.get("text"), "hello & welcome")
    check("still pending", payload.get("state"), "pending")
    check("bound to the same creator", payload.get("creator"), CREATOR)

    # Built from the CURRENT `messages` definition, which this change does not
    # touch, so it is exactly the schema a box created before the bridge table
    # existed would have.
    legacy_path = _TMP_ROOT / "legacy.db"
    legacy_engine = _sa_create_engine(f"sqlite:///{legacy_path.as_posix()}")
    Message.__table__.create(bind=legacy_engine)
    with legacy_engine.begin() as conn:
        conn.execute(_sa_text(
            "INSERT INTO messages (account_id, fan_id, message_id, direction, sender_name, "
            "body, media_ids, media_count, price_cents, is_tip, is_unsent, is_promise, "
            "created_at, ingested_at) VALUES ('acct-synthetic-1', 9001, 1, 'in', 'Fan', "
            "'<p>legacy</p>', '[]', 0, 0, 0, 0, 0, '2026-10-15 04:00:00', '2026-10-15 04:00:01')"
        ))
    legacy_engine.dispose()
    rc2, out2, err2 = _py(_UPGRADE_SCRIPT, str(legacy_path))
    check("startup on an existing schema succeeds", rc2, 0)
    payload2 = _json_line(out2) if rc2 == 0 else {}
    if rc2 != 0:
        print("    child stderr:", err2.strip()[:300])
    check("the bridge table exists after startup", payload2.get("has_bridge"), True)
    check("the pre-existing row is unchanged", payload2.get("legacy"), ["<p>legacy</p>"])


async def main() -> None:
    try:
        await evidence_1_and_2()
        await evidence_3()
        await evidence_4()
    finally:
        await engine.dispose()


try:
    run(main())
finally:
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("all cases passed (twin bridge intake)")
