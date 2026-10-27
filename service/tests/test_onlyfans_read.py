#!/usr/bin/env python3
"""OF-B1 — the OnlyFans read service, its refusals, and its read models.

Plain asserts, no pytest, no network, no live database, no provider. Every rule
in `service/onlyfans_read/` is driven here through the same entry points the
router uses — a fake repository stands in for storage, and the package's own
router is mounted on a throwaway FastAPI app — so the suite asserts the answers
a caller gets rather than the queries that produce them.

The list it exists to pin down, because each one is a shape that fails quietly:

  * a valid scoped read answers with the documented envelope and version;
  * an account bound to ANOTHER tenant is refused, and so is a creator that is
    not this credential's;
  * an unknown account is REFUSED (404), never an empty success — an empty list
    would be a false statement about a real account;
  * a legitimate empty inbox (a bound account with no conversations) IS an empty
    success, because those two must stay distinguishable;
  * a missing or wrong bearer token is refused, and no token configured at all
    fails closed with 503 instead of serving anything;
  * a missing or unimplemented schema version is refused, in the request header
    and in the answer body;
  * paging keeps its cursor, its order and its account, and a cursor from
    another account is refused;
  * storage that cannot be read is `unavailable`, never an empty list — asserted
    against a REAL SQLite database with no schema, not only against a fake;
  * a refusal never echoes the record it refused;
  * no fixture data exists anywhere in the package, and there is no write route,
    no POST, and no import of a send path.

Run: ./venv/bin/python service/tests/test_onlyfans_read.py
     (or service/run_tests.sh onlyfans_read)
"""
import os
import sys
import tempfile
import faulthandler
from datetime import datetime, timedelta
from pathlib import Path

# ── Isolation, before ANY project import ────────────────────────────────────
# `db.engine` reads DATABASE_URL at import time and defaults to the relay's own
# chatterly.db. This suite never wants that file: it points the engine at a
# throwaway SQLite database, so the one case that really does open storage (the
# schema-less database below) cannot touch a real one.
_TMP = Path(tempfile.mkdtemp(prefix="of-b1-"))
# `.as_posix()` on purpose: a Windows path with backslashes is not what
# SQLAlchemy expects in a URL, and a URL it cannot parse is a confusing failure
# in a file whose job is to be clear about failures.
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP / 'empty.db').as_posix()}"
os.environ["CHATTERLY_DB_URL"] = f"sqlite:///{(_TMP / 'empty.db').as_posix()}"
# Never inherit a credential: half these cases are about the absence of one, and
# inheriting would make the suite pass or fail by accident.
os.environ.pop("ONLYFANS_API_TOKEN", None)
os.environ.pop("ONLYFANS_API_TENANT", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A suite that hangs is a suite that hides whatever it was waiting for. Two
# minutes is far longer than any case here needs; past that, dump every thread's
# stack and exit non-zero so the runner reports a failure instead of waiting.
faulthandler.enable()
faulthandler.dump_traceback_later(120, exit=True)

import asyncio  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from onlyfans_read import auth as A  # noqa: E402
from onlyfans_read import contract as C  # noqa: E402
from onlyfans_read import service as S  # noqa: E402
from onlyfans_read import projections as P  # noqa: E402
from onlyfans_read.api import router as of_router  # noqa: E402
from onlyfans_read.auth import ServicePrincipal  # noqa: E402
from onlyfans_read.errors import BadRequest, Refused, Unauthorized, Unavailable  # noqa: E402
from onlyfans_read.repo import (  # noqa: E402
    BindingRow,
    ConversationRow,
    DeliveryRow,
    FactRow,
    MessageRow,
    OwnedRow,
    SqlOnlyFansReadRepo,
    StateRow,
)

FAILURES: list[str] = []
PKG = Path(__file__).resolve().parent.parent / "onlyfans_read"
PACKAGE_SOURCE = "\n".join(
    path.read_text(encoding="utf-8") for path in sorted(PKG.glob("*.py"))
)


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def raised(fn):
    """The exception a call raised, or None. For asserting the exact refusal."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — the point is to inspect it
        return exc
    return None


def check_raises(label: str, fn, exc_type) -> None:
    got = raised(fn)
    if isinstance(got, exc_type):
        print(f"  ok   {label}")
        return
    detail = f"raised {type(got).__name__}" if got else "did not raise"
    FAILURES.append(f"{label}: {detail}, want {exc_type.__name__}")
    print(f"  FAIL {label}: {detail}, want {exc_type.__name__}")


def run(coro):
    """Run one coroutine on a fresh loop — no shared loop, no ordering effects."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def check_raises_async(label: str, coro_factory, exc_type) -> None:
    got = None
    try:
        run(coro_factory())
    except Exception as exc:  # noqa: BLE001
        got = exc
    if isinstance(got, exc_type):
        print(f"  ok   {label}")
        return
    detail = f"raised {type(got).__name__}" if got else "did not raise"
    FAILURES.append(f"{label}: {detail}, want {exc_type.__name__}")
    print(f"  FAIL {label}: {detail}, want {exc_type.__name__}")


# ── Fixtures: two tenants, two creators, three accounts ─────────────────────
#
#   agency-a  creator-ava   account 1001  2 conversations (one of them held)
#                          account 1002  bound, no conversations
#   agency-b  creator-bea   account 2001  1 conversation, dead session
#
# 1001 and 2001 are the same shape with different owners, which is what makes
# the cross-tenant cases meaningful rather than accidentally different.

NOW = datetime(2026, 10, 18, 12, 0, 0)

BINDINGS = [
    BindingRow(
        binding_id="ofb-1001",
        tenant_id="agency-a",
        creator_id="creator-ava",
        creator_name="Ava Hart",
        account_id="1001",
        handle="avahart",
        channel="onlyfans",
        created_at=NOW - timedelta(days=30),
        last_probe_at=NOW - timedelta(minutes=5),
    ),
    BindingRow(
        binding_id="ofb-1002",
        tenant_id="agency-a",
        creator_id="creator-ava",
        creator_name="Ava Hart",
        account_id="1002",
        handle="avahartvip",
        channel="onlyfans",
        created_at=NOW - timedelta(days=9),
    ),
    BindingRow(
        binding_id="ofb-2001",
        tenant_id="agency-b",
        creator_id="creator-bea",
        creator_name="Bea Lane",
        account_id="2001",
        handle="bealane",
        channel="onlyfans",
        created_at=NOW - timedelta(days=3),
        session_dead_at=NOW - timedelta(hours=5),
        health_reason="OnlyFans rejected the stored session for this account.",
    ),
]

CONVERSATIONS = [
    ConversationRow(
        account_id="1001",
        fan_id=5001,
        last_message_id=900101,
        last_message_at=NOW - timedelta(minutes=3),
        last_message_preview="hey, you around?",
        unread_count=2,
        is_priority=True,
        is_pinned=False,
        fan_username="dave",
        fan_display_name="Dave K",
    ),
    ConversationRow(
        account_id="1001",
        fan_id=5002,
        last_message_id=900201,
        last_message_at=NOW - timedelta(hours=6),
        last_message_preview="thanks!",
        unread_count=0,
        is_priority=False,
        is_pinned=False,
        fan_username=None,
        fan_display_name=None,
    ),
    ConversationRow(
        account_id="1001",
        fan_id=5003,
        last_message_id=900301,
        last_message_at=NOW - timedelta(days=1),
        last_message_preview="older thread",
        unread_count=0,
        is_priority=False,
        is_pinned=False,
        fan_username="old",
        fan_display_name="Old Thread",
    ),
    ConversationRow(
        account_id="2001",
        fan_id=7001,
        last_message_id=990101,
        last_message_at=NOW - timedelta(minutes=30),
        last_message_preview="hi there",
        unread_count=0,
        is_priority=False,
        is_pinned=False,
        fan_username="zed",
        fan_display_name="Zed",
    ),
]


def _msg(message_id, direction, body, *, minutes_ago=1, temp_id=None, kind=None, employee=None):
    return MessageRow(
        message_id=message_id,
        direction=direction,
        body=body,
        created_at=NOW - timedelta(minutes=minutes_ago),
        media_count=0,
        price_cents=0,
        is_paid=None,
        purchased_at=None,
        is_unsent=False,
        temp_id=temp_id,
        automation_kind=kind,
        sent_by_employee_id=employee,
    )


MESSAGES = {
    ("1001", 5001): [
        _msg(900100, "in", "hey, you around?", minutes_ago=3),
        _msg(900101, "out", "Always. What's up?", minutes_ago=1, kind="autoreply"),
    ],
    ("1001", 5002): [
        _msg(900200, "out", "mass blast placeholder", minutes_ago=360, temp_id="mass:5:5002"),
        _msg(900201, "out", "hand-written reply", minutes_ago=300, employee=7),
    ],
}

DELIVERIES = {
    ("1001", 5001): {
        900101: DeliveryRow(
            message_id=900101,
            outcome="unknown",
            attempt_id="attempt-77",
            evidence_ref=None,
            detail=None,
            recorded_at=NOW - timedelta(seconds=30),
        )
    },
}

STATES = {
    ("1001", 5001): StateRow(
        mode="human_controlled",
        revision=4,
        takeover_state="effective",
        unresolved_attempt_id="attempt-77",
        requires_attention=False,
        updated_at=NOW - timedelta(seconds=30),
    )
}

FACTS = {("1001", 5001): [FactRow(label="City", value="Berlin")]}

OWNED = {
    ("1001", 5001): [
        OwnedRow(
            media_ids=[4242],
            label="gym set",
            price_cents=1500,
            purchased_at=NOW - timedelta(days=2),
        )
    ]
}

ANCHORS = {
    "1001": NOW - timedelta(minutes=3),
    "1002": NOW - timedelta(days=9),
    # Comfortably past the freshness window, so the "stale" case is about the
    # policy rather than about sitting exactly on its boundary.
    "2001": NOW - timedelta(hours=3),
}


class FakeRepo:
    """The repository boundary, in memory.

    It applies the SAME tenant narrowing the SQL does — `tenant_id is None`
    means the deployment scope, otherwise an equality filter — because the
    cross-tenant refusals are only meaningful if the fake and the real query
    agree about what "this credential's accounts" means.
    """

    def __init__(self, *, fail: Exception | None = None):
        self.fail = fail

    def _maybe_fail(self) -> None:
        if self.fail is not None:
            raise self.fail

    async def list_bindings(self, *, tenant_id, creator_id, account_id):
        self._maybe_fail()
        out = [b for b in BINDINGS if tenant_id is None or b.tenant_id == tenant_id]
        if creator_id is not None:
            out = [b for b in out if b.creator_id == creator_id]
        if account_id is not None:
            out = [b for b in out if b.account_id == account_id]
        return out

    async def get_binding(self, *, tenant_id, account_id):
        rows = await self.list_bindings(tenant_id=tenant_id, creator_id=None, account_id=account_id)
        return rows[0] if rows else None

    async def conversation_page(self, *, account_id, before, limit):
        self._maybe_fail()
        rows = [c for c in CONVERSATIONS if c.account_id == account_id]
        rows.sort(key=lambda c: (c.last_message_at or datetime.min, c.fan_id), reverse=True)
        if before is not None:
            anchor_at, anchor_fan = before
            rows = [
                c
                for c in rows
                if (
                    (anchor_at is None and c.last_message_at is None and c.fan_id < anchor_fan)
                    or (
                        anchor_at is not None
                        and (
                            (c.last_message_at is not None and c.last_message_at < anchor_at)
                            or (c.last_message_at == anchor_at and c.fan_id < anchor_fan)
                            or c.last_message_at is None
                        )
                    )
                )
            ]
        return rows[:limit]

    async def conversation(self, *, account_id, fan_id):
        self._maybe_fail()
        for row in CONVERSATIONS:
            if row.account_id == account_id and row.fan_id == fan_id:
                return row
        return None

    async def conversation_state(self, *, account_id, fan_id):
        self._maybe_fail()
        return STATES.get((account_id, fan_id))

    async def messages(self, *, account_id, fan_id, limit):
        self._maybe_fail()
        return MESSAGES.get((account_id, fan_id), [])

    async def deliveries(self, *, account_id, fan_id):
        self._maybe_fail()
        return DELIVERIES.get((account_id, fan_id), {})

    async def fan_facts(self, *, account_id, fan_id):
        self._maybe_fail()
        return FACTS.get((account_id, fan_id), [])

    async def owned_media(self, *, account_id, fan_id, limit):
        self._maybe_fail()
        return OWNED.get((account_id, fan_id), [])

    async def account_activity_anchor(self, *, account_id):
        self._maybe_fail()
        return ANCHORS.get(account_id)


DEPLOYMENT = ServicePrincipal(tenant_id=None, label="deployment")
AGENCY_A = ServicePrincipal(tenant_id="agency-a", label="tenant:agency-a")
AGENCY_B = ServicePrincipal(tenant_id="agency-b", label="tenant:agency-b")

TOKEN = "test-service-token-value"


def headers(**extra):
    base = {
        "authorization": f"Bearer {TOKEN}",
        C.SCHEMA_VERSION_HEADER: C.SCHEMA_VERSION,
    }
    base.update(extra)
    return base


# ══ 1. The contract ════════════════════════════════════════════════════════
print("\n1. The contract: identity, cursors and bounds")

check("the schema version is the dashboard's own", C.SCHEMA_VERSION, "of-preview.v2")
check("and this service claims to support it", C.SCHEMA_VERSION in C.SUPPORTED_SCHEMA_VERSIONS, True)
check("the namespace avoids the cookie-gated /api/of prefix",
      C.SERVICE_PREFIX.startswith("/api/of/"), False)
check("the namespace is exempted exactly, never by a stem",
      (C.is_service_path(C.SERVICE_PREFIX),
       C.is_service_path(C.SERVICE_PREFIX + "/accounts"),
       C.is_service_path(C.SERVICE_PREFIX + "10"),
       C.is_service_path("/onlyfans/inbox")),
      (True, True, False, False))

CONV = C.conversation_id_for("1001", 5001)
check("a conversation id carries its account", CONV, "1001:5001")
check("inside the id class the dashboard already accepts", bool(C.ID_PATTERN.match(CONV)), True)
check("it resolves back to the fan", C.fan_id_from_conversation_id(CONV, "1001"), 5001)
check("an id from another account does not resolve",
      C.fan_id_from_conversation_id(CONV, "2001"), None)
check("nor does a fabricated one",
      C.fan_id_from_conversation_id("1001:not-a-fan", "1001"), None)
check("two accounts never mint the same conversation id",
      C.conversation_id_for("1001", 5001) == C.conversation_id_for("2001", 5001), False)

CURSOR = C.encode_cursor(account_id="1001", last_activity_at="2026-10-18T11:57:00Z", fan_id=5001)
check("a cursor decodes for the account it was minted for",
      C.decode_cursor(CURSOR, account_id="1001"), ("2026-10-18T11:57:00Z", 5001))
check_raises("a cursor from another account is refused",
             lambda: C.decode_cursor(CURSOR, account_id="2001"), ValueError)
check_raises("a cursor that is not base64 is refused",
             lambda: C.decode_cursor("not!base64!", account_id="1001"), ValueError)
check_raises("an empty cursor is refused",
             lambda: C.decode_cursor("", account_id="1001"), ValueError)
check_raises("a cursor minted for another account is refused, not followed",
             lambda: C.decode_cursor(
                 C.encode_cursor(account_id="2001", last_activity_at=None, fan_id=1),
                 account_id="1001"),
             ValueError)

# The convention that bit once: `datetime.fromisoformat("...Z")` is
# offset-AWARE on 3.11+, and every DateTime column here is naive UTC. Comparing
# the two raises at the page boundary, so the decode has to return the same kind
# of value the rows hold.
_decoded_at, _ = C.decode_cursor(CURSOR, account_id="1001")
check("a decoded cursor timestamp is naive UTC, like the columns",
      C.parse_iso(_decoded_at).tzinfo, None)
check("and it round-trips to the instant it named",
      C.parse_iso(_decoded_at), datetime(2026, 10, 18, 11, 57, 0))
check_raises("a cursor with an unreadable timestamp is refused",
             lambda: C.parse_iso("not-a-time"), ValueError)

check("an absent limit takes the default", C.clamp_limit(None), C.DEFAULT_LIMIT)
check("a bigger ask is bounded", C.clamp_limit(10_000), C.MAX_LIMIT)
check("and a nonsense one is still a page", C.clamp_limit(0), 1)

# The freshness policy, stated as a boundary rather than left to be inferred
# from a fixture: at the window the projection is still usable, past it the
# dashboard says "last known" instead of "updated".
from onlyfans_read import projections as _P  # noqa: E402

check("a projection exactly at the freshness window is not stale",
      _P.freshness(NOW - timedelta(seconds=_P.FRESHNESS_MAX_AGE_SECONDS), now=NOW)["stale"], False)
check("one second past it is",
      _P.freshness(NOW - timedelta(seconds=_P.FRESHNESS_MAX_AGE_SECONDS + 1), now=NOW)["stale"], True)
check("and the window it publishes is the one it uses",
      _P.freshness(NOW, now=NOW)["maxAgeSeconds"], _P.FRESHNESS_MAX_AGE_SECONDS)


# ══ 2. Service-to-service auth ═════════════════════════════════════════════
print("\n2. The service credential: bearer only, and it fails closed")

check("with no token configured the service refuses everything",
      type(raised(lambda: A.verify_bearer("Bearer anything"))), Unavailable)
check("and it says so as an unavailable state, not a denial",
      raised(lambda: A.verify_bearer("Bearer anything")).state, "unavailable")

os.environ["ONLYFANS_API_TOKEN"] = TOKEN
check("a missing header is refused",
      type(raised(lambda: A.verify_bearer(None))), Unauthorized)
check("a wrong scheme is refused",
      type(raised(lambda: A.verify_bearer(f"Basic {TOKEN}"))), Unauthorized)
check("a bare token with no scheme is refused",
      type(raised(lambda: A.verify_bearer(TOKEN))), Unauthorized)
check("a wrong token is refused",
      type(raised(lambda: A.verify_bearer("Bearer nope"))), Unauthorized)
check("a cookie-shaped value is not a credential",
      type(raised(lambda: A.verify_bearer("Bearer session=abc123"))), Unauthorized)

# A token with NO tenant used to be read as the deployment scope, which made
# "may read every binding in this installation" the state you get by forgetting
# a variable. It is now refused, and the refusal is checked BEFORE anything
# could read a binding (section 9 repeats this with a real database).
check("the right token is not enough on its own — no tenant is refused",
      type(raised(lambda: A.verify_bearer(f"Bearer {TOKEN}"))), Unavailable)
check("and it is unavailable rather than denied",
      raised(lambda: A.verify_bearer(f"Bearer {TOKEN}")).state, "unavailable")
check("its code names the decision that is missing, not a record",
      raised(lambda: A.verify_bearer(f"Bearer {TOKEN}")).code, "tenant_not_configured")

os.environ["ONLYFANS_API_TENANT"] = ""
check("a blank tenant is the same refusal",
      type(raised(lambda: A.verify_bearer(f"Bearer {TOKEN}"))), Unavailable)
os.environ["ONLYFANS_API_TENANT"] = "   "
check("and so is a whitespace-only tenant",
      type(raised(lambda: A.verify_bearer(f"Bearer {TOKEN}"))), Unavailable)
check("the deployment reports no usable scope while it is undecided",
      A.tenant_configured(), False)
# The order matters and is asserted: a misconfigured deployment must not
# describe its own configuration to whoever happened to send a bad token.
check("a wrong token is still a denial, not a configuration answer",
      type(raised(lambda: A.verify_bearer("Bearer nope"))), Unauthorized)
os.environ.pop("ONLYFANS_API_TENANT", None)

# The rule itself is one pure function, so every branch is driven directly —
# no process environment, no request, no storage.
check("the deployment scope requires the word 'deployment'",
      A.resolve_tenant("deployment"), None)
check("and the match is exact — a near-miss stays a tenant id",
      A.resolve_tenant("Deployment"), "Deployment")
check("as does a word that merely contains it",
      A.resolve_tenant("deployment-2"), "deployment-2")
check("an id narrows to that id", A.resolve_tenant("agency-a"), "agency-a")
check("a blank tenant is refused by the rule itself",
      type(raised(lambda: A.resolve_tenant("  "))), Unavailable)
check("and so is an absent one",
      type(raised(lambda: A.resolve_tenant(None))), Unavailable)
check("so nothing can resolve blank to the deployment scope",
      (A.resolve_tenant("agency-a") is None, A.resolve_tenant("deployment") is None),
      (False, True))

os.environ["ONLYFANS_API_TENANT"] = "agency-a"
check("a tenant-scoped token narrows to that tenant",
      A.verify_bearer(f"Bearer {TOKEN}").tenant_id, "agency-a")
check("and is not the deployment scope",
      A.verify_bearer(f"Bearer {TOKEN}").is_deployment_scope, False)
os.environ["ONLYFANS_API_TENANT"] = "deployment"
check("the literal 'deployment' means the deployment scope",
      A.verify_bearer(f"Bearer {TOKEN}").is_deployment_scope, True)
check("and is admitted by name", A.verify_bearer(f"Bearer {TOKEN}").label, "deployment")
os.environ.pop("ONLYFANS_API_TENANT", None)

AUTH_SOURCE = (PKG / "auth.py").read_text(encoding="utf-8")
check("the auth module cannot read a cookie", ".cookies" in AUTH_SOURCE, False)
check("nor a query parameter", "query_params" in AUTH_SOURCE, False)
check("and it compares in constant time", "compare_digest" in AUTH_SOURCE, True)

check("a request with no schema version is refused",
      type(raised(lambda: A.negotiate_schema_version(None))), BadRequest)
check("an empty schema version is refused",
      type(raised(lambda: A.negotiate_schema_version("   "))), BadRequest)
check("an unimplemented schema version is refused",
      type(raised(lambda: A.negotiate_schema_version("of-preview.v9"))), BadRequest)
check("the implemented one is accepted",
      A.negotiate_schema_version(C.SCHEMA_VERSION), C.SCHEMA_VERSION)


# ══ 3. Valid scoped reads ══════════════════════════════════════════════════
print("\n3. Valid reads answer with the documented envelope")

accounts = run(S.list_accounts(FakeRepo(), AGENCY_A, now=NOW))
check("the envelope is versioned", accounts["schemaVersion"], C.SCHEMA_VERSION)
check("and carries the requested scope",
      accounts["scope"], {"creatorId": None, "accountId": None})
check("agency-a sees exactly its two accounts",
      [a["accountId"] for a in accounts["accounts"]], ["1001", "1002"])

AVA = accounts["accounts"][0]
check("an account names its binding, creator and handle",
      (AVA["binding"]["bindingId"], AVA["binding"]["creatorId"], AVA["binding"]["handle"]),
      ("ofb-1001", "creator-ava", "avahart"))
check("the channel is OnlyFans, never a persona field", AVA["binding"]["channel"], "onlyfans")
check("an unverified account does not claim connected", AVA["health"], "disconnected")
check("and explains the missing connection evidence", bool(AVA["healthReason"]), True)
check("the view capability is the one this service implements",
      AVA["capabilities"]["viewConversations"], "available")
check("the recorded sales view is available", AVA["capabilities"]["viewSales"], "available")
check("and every mutation is not_configured rather than available",
      sorted({v for k, v in AVA["capabilities"].items() if k not in {"viewConversations", "viewSales"}}),
      ["not_configured"])
check("freshness is anchored on the newest observed activity",
      AVA["syncFreshness"]["asOf"], C.iso(NOW - timedelta(minutes=3)))
check("and a recently active account is not stale", AVA["syncFreshness"]["stale"], False)

BEA = run(S.list_accounts(FakeRepo(), AGENCY_B, now=NOW))["accounts"][0]
check("a dead session reads as expired_session", BEA["health"], "expired_session")
check("with the relay's own reason attached",
      BEA["healthReason"], "OnlyFans rejected the stored session for this account.")
check("and an account with no observed activity is stale, not fresh",
      BEA["syncFreshness"]["stale"], True)

PAGE = run(S.list_conversations(FakeRepo(), AGENCY_A, account_id="1001", now=NOW))
check("the list envelope is versioned", PAGE["schemaVersion"], C.SCHEMA_VERSION)
check("and scoped to the account asked for", PAGE["accountId"], "1001")
check("newest activity first",
      [i["conversationId"] for i in PAGE["page"]["items"]],
      ["1001:5001", "1001:5002", "1001:5003"])
FIRST = PAGE["page"]["items"][0]
check("a summary carries the fan",
      FIRST["fan"], {"fanId": "5001", "displayName": "Dave K", "shortRef": "5001"})
check("and the priority the relay recorded", FIRST["priority"], "priority")
check("and the control row's revision", FIRST["revision"], 4)
check("and its mode and takeover state",
      (FIRST["automationMode"], FIRST["takeoverState"]), ("human_controlled", "effective"))
check("the last message's unsettled outcome reaches the row", FIRST["delivery"], "unknown")
check("a fan with no name is named, not blank",
      PAGE["page"]["items"][1]["fan"]["displayName"], "Unnamed fan")
check("an account with one page has no cursor", PAGE["page"]["nextCursor"], None)
check("a held conversation is flagged for attention",
      FIRST["requiresAttention"], True)

DETAIL = run(
    S.get_conversation(FakeRepo(), AGENCY_A, account_id="1001",
                       conversation_id="1001:5001", now=NOW)
)["conversation"]
check("the detail carries the summary the list showed",
      DETAIL["summary"]["conversationId"], "1001:5001")
check("messages are in the order they happened",
      [m["messageId"] for m in DETAIL["messages"]], ["900100", "900101"])
check("each message carries its own conversation",
      {m["conversationId"] for m in DETAIL["messages"]}, {"1001:5001"})
check("a fan message is inbound and from the fan",
      (DETAIL["messages"][0]["direction"], DETAIL["messages"][0]["origin"]), ("inbound", "fan"))
check("an inbound message has no send receipt", DETAIL["messages"][0]["delivery"], "none")
check("an automation send is attributed to the ai", DETAIL["messages"][1]["origin"], "ai")
check("a recorded unknown outcome is not smoothed into sent",
      DETAIL["messages"][1]["delivery"], "unknown")
check("and it carries no evidence it does not have",
      DETAIL["messages"][1]["evidenceRef"], None)
check("facts are labelled with their provenance",
      DETAIL["facts"], [{"label": "City", "value": "Berlin", "source": "imported"}])
check("money is minor units with an explicit currency",
      DETAIL["ownedMedia"][0]["price"], {"currency": "USD", "minorUnits": 1500})
check("an unresolved send offers the resolution path",
      (DETAIL["deliveryIncident"]["attemptId"], DETAIL["deliveryIncident"]["outcome"]),
      ("attempt-77", "unknown"))

SECOND = run(
    S.get_conversation(FakeRepo(), AGENCY_A, account_id="1001",
                       conversation_id="1001:5002", now=NOW)
)["conversation"]
BY_ID = {m["messageId"]: m for m in SECOND["messages"]}
check("a mass placeholder is dispatching, not sent", BY_ID["900200"]["delivery"], "dispatching")
check("a human send is attributed to a human", BY_ID["900201"]["origin"], "human")
check("a confirmed outbound row is sent", BY_ID["900201"]["delivery"], "sent")
check("and a settled conversation has no incident", SECOND["deliveryIncident"], None)

# The text delivery authority records authorship on its receipt; it does not
# impersonate an employee or write legacy automation metadata to infer it.
for recorded_origin in ("human", "ai"):
    projected = P.message_model(
        conversation_id="1001:5002",
        message=_msg(900202, "out", "synthetic accepted text"),
        recorded=DeliveryRow(900202, "sent", "attempt-origin", "receipt-origin",
                             None, NOW, origin=recorded_origin),
    )
    check(f"a recorded {recorded_origin} origin survives an accepted text receipt",
          (projected["origin"], projected["delivery"], projected["evidenceRef"]),
          (recorded_origin, "sent", "receipt-origin"))
check("a missing receipt origin preserves legacy automation inference",
      P.message_origin(_msg(900203, "out", "legacy", kind="autoreply"),
                       DeliveryRow(900203, "unknown", None, None, None, NOW)), "ai")
check("an unsupported receipt origin does not replace legacy human attribution",
      P.message_origin(_msg(900204, "out", "legacy", employee=7),
                       DeliveryRow(900204, "sent", None, None, None, NOW, origin="fan")),
      "human")
check("a receipt cannot relabel an inbound fan message as local authorship",
      P.message_origin(_msg(900205, "in", "inbound"),
                       DeliveryRow(900205, "sent", None, None, None, NOW, origin="ai")), "fan")


# ══ 4. Refusals: another tenant, another account, or nothing there ═════════
print("\n4. Another tenant's data is refused, and an unknown account with it")

check_raises_async("an account bound to another tenant is refused",
                   lambda: S.get_conversation(FakeRepo(), AGENCY_A, account_id="2001",
                                              conversation_id="2001:7001"),
                   Refused)
check_raises_async("so is asking for it by scope",
                   lambda: S.list_accounts(FakeRepo(), AGENCY_A, account_id="2001"),
                   Refused)
check_raises_async("and so is its creator",
                   lambda: S.list_accounts(FakeRepo(), AGENCY_A, creator_id="creator-bea"),
                   Refused)
check_raises_async("a completely unknown account is refused, not empty",
                   lambda: S.list_accounts(FakeRepo(), AGENCY_A, account_id="9999"),
                   Refused)
check_raises_async("a conversation id from another account is refused",
                   lambda: S.get_conversation(FakeRepo(), AGENCY_A, account_id="1001",
                                              conversation_id="2001:7001"),
                   Refused)
check_raises_async("an unknown conversation on a bound account is refused",
                   lambda: S.get_conversation(FakeRepo(), AGENCY_A, account_id="1001",
                                              conversation_id="1001:888888"),
                   Refused)
check_raises_async("a malformed account id is a bad request, not a miss",
                   lambda: S.list_accounts(FakeRepo(), AGENCY_A, account_id="../../etc/passwd"),
                   BadRequest)

check("every cause of refusal answers with the same sentence",
      {raised(lambda: None) or Refused("unknown_account").message,
       Refused("unknown_conversation").message},
      {"That record is not available to this service credential."})
check("the refusal body never names the record it refused",
      "2001" in str(Refused("unknown_account").as_body(expose=True)), False)

EMPTY = run(S.list_conversations(FakeRepo(), AGENCY_A, account_id="1002", now=NOW))
check("a bound account with no conversations is a legitimate empty page",
      (EMPTY["page"]["items"], EMPTY["page"]["nextCursor"]), ([], None))
check("which is a success envelope, not a refusal", EMPTY["schemaVersion"], C.SCHEMA_VERSION)

DEPLOY_ACCOUNTS = run(S.list_accounts(FakeRepo(), DEPLOYMENT, now=NOW))
check("the deployment scope sees every bound account",
      [a["accountId"] for a in DEPLOY_ACCOUNTS["accounts"]], ["1001", "1002", "2001"])
check("so the tenant narrowing is what refuses, not the absence of a record",
      len(DEPLOY_ACCOUNTS["accounts"]) > len(accounts["accounts"]), True)


# ══ 5. Paging ══════════════════════════════════════════════════════════════
print("\n5. Paging keeps its cursor, its order and its account")

FIRST_PAGE = run(S.list_conversations(FakeRepo(), AGENCY_A, account_id="1001", limit=2, now=NOW))
check("the page honours the limit", len(FIRST_PAGE["page"]["items"]), 2)
check("and tells the client there is more",
      isinstance(FIRST_PAGE["page"]["nextCursor"], str), True)
SECOND_PAGE = run(
    S.list_conversations(FakeRepo(), AGENCY_A, account_id="1001", limit=2,
                         cursor=FIRST_PAGE["page"]["nextCursor"], now=NOW)
)
check("the next page resumes after the last row shown",
      [i["conversationId"] for i in SECOND_PAGE["page"]["items"]], ["1001:5003"])
check("and stops advertising more", SECOND_PAGE["page"]["nextCursor"], None)
check("no row is repeated across the two pages",
      {i["conversationId"] for i in FIRST_PAGE["page"]["items"]}
      & {i["conversationId"] for i in SECOND_PAGE["page"]["items"]},
      set())
check_raises_async("a cursor from another account is refused",
                   lambda: S.list_conversations(
                       FakeRepo(), AGENCY_A, account_id="1001",
                       cursor=C.encode_cursor(account_id="1002", last_activity_at=None, fan_id=1)),
                   BadRequest)
check_raises_async("a corrupt cursor is refused",
                   lambda: S.list_conversations(FakeRepo(), AGENCY_A, account_id="1001",
                                                cursor="!!!not-a-cursor!!!"),
                   BadRequest)
check_true("an over-large ask is clamped to the maximum",
           len(run(S.list_conversations(FakeRepo(), AGENCY_A, account_id="1001",
                                        limit=10_000, now=NOW))["page"]["items"]) <= C.MAX_LIMIT)


# ══ 6. Unreadable storage, and no fallback of any kind ════════════════════
print("\n6. Unreadable storage is unavailable, and there is no fallback")

check_raises_async("a repository failure surfaces as unavailable, not as empty",
                   lambda: S.list_accounts(FakeRepo(fail=Unavailable("storage_unavailable", "x")),
                                           AGENCY_A),
                   Unavailable)

# The REAL repository against a real, schema-less SQLite database — which is
# exactly the production shape of "this migration has not been applied yet".
_STORAGE = SqlOnlyFansReadRepo()
_STORAGE_ERROR = None
try:
    run(_STORAGE.list_bindings(tenant_id=None, creator_id=None, account_id=None))
except Exception as exc:  # noqa: BLE001
    _STORAGE_ERROR = exc
check("the SQL repository fails closed on a schema-less database",
      type(_STORAGE_ERROR), Unavailable)
check("and calls it storage, not a missing record",
      getattr(_STORAGE_ERROR, "code", None), "storage_unavailable")
check("and never reports it as an empty account list",
      getattr(_STORAGE_ERROR, "state", None), "unavailable")

check("the package contains no fixture data",
      any(word in PACKAGE_SOURCE for word in ("Sample data", "sample_data", "FAKE_")), False)
check("the package never imports the provider client", "of_client" in PACKAGE_SOURCE, False)
check("and never imports an automation or send path",
      any(word in PACKAGE_SOURCE for word in ("pack_sender", "sell_lane", "automation_executor")),
      False)
# The recorded-earnings projection merges a Python dictionary; it is not SQL.
check("nothing in the package writes (no inserts, updates or deletes)",
      any(word in PACKAGE_SOURCE.replace('result.update(status="current",', "dictionary_merge(") for word in ("sqlite_insert", "insert(", ".update(", "delete(")),
      False)

ROUTE_METHODS = {m for route in of_router.routes for m in getattr(route, "methods", set())}
check("the router exposes GET and HEAD and nothing else",
      ROUTE_METHODS - {"GET", "HEAD"}, set())
PATHS = sorted(getattr(route, "path", "") for route in of_router.routes)
check("it registers scoped reads, native workspace and the smoke check", PATHS, [
    f"{C.SERVICE_PREFIX}/accounts",
    f"{C.SERVICE_PREFIX}/accounts/{{account_id}}/conversations",
    f"{C.SERVICE_PREFIX}/accounts/{{account_id}}/conversations/{{conversation_id}}",
    f"{C.SERVICE_PREFIX}/accounts/{{account_id}}/conversations/{{conversation_id}}/handover",
    f"{C.SERVICE_PREFIX}/accounts/{{account_id}}/ledger",
    f"{C.SERVICE_PREFIX}/accounts/{{account_id}}/operations",
    f"{C.SERVICE_PREFIX}/accounts/{{account_id}}/workspace/{{section}}",
    f"{C.SERVICE_PREFIX}/earnings",
    f"{C.SERVICE_PREFIX}/status",
])

SERVER_SOURCE = (PKG.parent / "server.py").read_text(encoding="utf-8")
check("the relay registers the router",
      "include_router(_onlyfans_read_router)" in SERVER_SOURCE, True)
check("and exempts its namespace from the browser share gate",
      "_is_onlyfans_service_path(request.url.path)" in SERVER_SOURCE, True)
check("using the package's own prefix constant, not a copied string",
      "from onlyfans_read import is_service_path" in SERVER_SOURCE, True)


# ══ 7. Over HTTP: status codes, headers and the gate ═══════════════════════
print("\n7. Over HTTP the refusals are distinguishable, and never cached")

os.environ["ONLYFANS_API_TOKEN"] = TOKEN
os.environ["ONLYFANS_API_TENANT"] = "agency-a"

import onlyfans_read.api as API  # noqa: E402  (after the env, like the router sees it)

# Drive the router with the same fake, as above — and KEEP the real factory, so
# section 10 can put it back before it exercises the application end to end.
# (The patch is on the module object, which `import server` shares.)
_PRODUCTION_REPO_FACTORY = API._repo
API._repo = lambda: FakeRepo()

APP = FastAPI()
APP.include_router(of_router)
CLIENT = TestClient(APP)

OK = CLIENT.get(f"{C.SERVICE_PREFIX}/status", headers=headers())
check("a correctly credentialled smoke check is 200", OK.status_code, 200)
check("and states the version it speaks", OK.json()["schemaVersion"], C.SCHEMA_VERSION)
check("and its scope, without naming an account", OK.json()["scope"], "tenant")
check("private answers are never cached", OK.headers.get("cache-control"), "private, no-store")
check("the version is repeated where a client can check it",
      OK.headers.get(C.SCHEMA_VERSION_HEADER.lower()), C.SCHEMA_VERSION)

check("no credential is a 401",
      CLIENT.get(f"{C.SERVICE_PREFIX}/status").status_code, 401)
check("a wrong credential is the same 401",
      CLIENT.get(f"{C.SERVICE_PREFIX}/status",
                 headers=headers(authorization="Bearer wrong")).status_code, 401)
check("no schema version is a 400",
      CLIENT.get(f"{C.SERVICE_PREFIX}/status",
                 headers={"authorization": f"Bearer {TOKEN}"}).status_code, 400)
check("and it names the missing header",
      CLIENT.get(f"{C.SERVICE_PREFIX}/status",
                 headers={"authorization": f"Bearer {TOKEN}"}).json()["code"],
      "missing_schema_version")
check("an unimplemented schema version is a 400",
      CLIENT.get(f"{C.SERVICE_PREFIX}/status",
                 headers=headers(**{C.SCHEMA_VERSION_HEADER: "of-preview.v0"})).status_code, 400)

ACCT = CLIENT.get(f"{C.SERVICE_PREFIX}/accounts", headers=headers())
check("the accounts read is 200 for an authorized caller", ACCT.status_code, 200)
check("and versioned in the body", ACCT.json()["schemaVersion"], C.SCHEMA_VERSION)

# `?account=` is a query parameter, not a header — an id in a header would not
# reach the handler at all, which is the sort of thing a green suite should not
# be able to hide.
MISSING = CLIENT.get(f"{C.SERVICE_PREFIX}/accounts?account=9999", headers=headers())
check("an unknown account is a 404, never an empty 200", MISSING.status_code, 404)
check("with a refusal state", MISSING.json()["state"], "denied")
check("and no accounts array to misread", "accounts" in MISSING.json(), False)

CROSSED = CLIENT.get(f"{C.SERVICE_PREFIX}/accounts?account=2001", headers=headers())
check("another tenant's account is indistinguishable from an unknown one",
      CROSSED.status_code, MISSING.status_code)
check("and its body does not echo the id that was refused",
      "2001" in CROSSED.text, False)

EMPTY_INBOX = CLIENT.get(f"{C.SERVICE_PREFIX}/accounts/1002/conversations", headers=headers())
check("a bound account with an empty inbox is 200", EMPTY_INBOX.status_code, 200)
check("and says so with an empty page", EMPTY_INBOX.json()["page"]["items"], [])

CONV_HTTP = CLIENT.get(f"{C.SERVICE_PREFIX}/accounts/1001/conversations/1001:5001",
                       headers=headers())
check("a conversation read is 200", CONV_HTTP.status_code, 200)
check("and carries the conversation",
      CONV_HTTP.json()["conversation"]["conversationId"], "1001:5001")
check("a conversation id from another account is a 404",
      CLIENT.get(f"{C.SERVICE_PREFIX}/accounts/1001/conversations/2001:7001",
                 headers=headers()).status_code, 404)

os.environ.pop("ONLYFANS_API_TOKEN", None)
UNCONFIGURED = CLIENT.get(f"{C.SERVICE_PREFIX}/accounts", headers=headers())
check("with no token configured the whole service is unavailable", UNCONFIGURED.status_code, 503)
check("it is unavailable rather than denied", UNCONFIGURED.json()["state"], "unavailable")
check("and certainly not an empty list of accounts", "accounts" in UNCONFIGURED.json(), False)
check("a refusal is never cached either",
      (UNCONFIGURED.headers.get("cache-control"),
       MISSING.headers.get("cache-control")),
      ("private, no-store", "private, no-store"))
os.environ["ONLYFANS_API_TOKEN"] = TOKEN
os.environ.pop("ONLYFANS_API_TENANT", None)


# ══ 8. The migration ═══════════════════════════════════════════════════════
print("\n8. The migration is in the graph, unique, and creates only what is read")

# Read the revision graph straight from the files, AND through Alembic's own
# configuration. Both, because they answer different questions: the file walk
# says what the chain IS, and the config walk says whether Alembic can see it.
# The config walk is the one that used to fail — `version_locations` expanded
# `%(here)s` to an absolute path and Alembic's legacy split broke it on the
# spaces in "TWIN CLONE CREATION", so the CLI reported ZERO revisions while the
# files were perfectly fine. A suite that only read the files could not have
# caught that, which is why the alembic.ini repair is asserted rather than
# assumed.
import ast  # noqa: E402
import re as _re  # noqa: E402

VERSIONS = PKG.parent / "db" / "migrations" / "versions"


def _revision_meta(path: Path) -> tuple[str | None, str | None]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    revision = down = None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        else:
            continue
        if target in ("revision", "down_revision"):
            try:
                literal = ast.literal_eval(value)
            except Exception:  # noqa: BLE001 — a non-literal is not our shape
                continue
            if target == "revision":
                revision = literal
            else:
                down = literal
    return revision, down


GRAPH: dict[str, str | None] = {}
DUPLICATES: list[str] = []
for path in sorted(VERSIONS.glob("*.py")):
    rev, down = _revision_meta(path)
    if rev is None:
        continue
    if rev in GRAPH:
        DUPLICATES.append(rev)
    GRAPH[rev] = down

MINE = "0070_onlyfans_read_bindings"
check("every revision id in the tree is unique", DUPLICATES, [])
check("the OF-B1 migration is in the graph", MINE in GRAPH, True)
check("it descends from the committed head this branch was cut from",
      GRAPH.get(MINE), "0064_user_llm_keys")
# A down_revision may be a TUPLE — 0022 merges two branches — so it has to be
# unpacked. Treating the tuple as one opaque parent is what made 0019 and 0021
# look like heads in the previous revision of this suite.
PARENTS = {
    parent
    for down in GRAPH.values()
    if down
    for parent in (down if isinstance(down, tuple) else (down,))
}
HEADS = sorted(rev for rev in GRAPH if rev not in PARENTS)
check("0070 is now an ancestor of the foundation head", MINE in HEADS, False)
check("and nothing else descends from the committed head it was cut from",
      sorted(rev for rev, down in GRAPH.items() if down == "0064_user_llm_keys"),
      [MINE])
# ONE head, this one. The chain that once looked like it had two stray heads
# (0019/0021) in fact merges at 0022, so `upgrade heads` and `upgrade head` are
# the same operation here — asserted now that the graph can be read at all.
from db.schema import HEAD as _SUPPORTED_HEAD
check("the tree has exactly one head", HEADS, [_SUPPORTED_HEAD])
check("its parent is a revision that exists", "0064_user_llm_keys" in GRAPH, True)

# — and now the same graph through Alembic's real configuration, which is the
# side that was broken. `from_config` is exactly what `alembic upgrade head`
# calls, so a green result here is the CLI's own discovery rather than a
# reconstruction of it.
from alembic.config import Config as _AlembicConfig  # noqa: E402
from alembic.script import ScriptDirectory as _ScriptDirectory  # noqa: E402

ALEMBIC_INI = PKG.parent / "db" / "migrations" / "alembic.ini"
ALEMBIC_INI_SOURCE = ALEMBIC_INI.read_text(encoding="utf-8")
# Only the live keys, so the comment explaining the removal does not itself
# look like the setting.
ALEMBIC_LIVE_KEYS = [
    line.split("=", 1)[0].strip()
    for line in ALEMBIC_INI_SOURCE.splitlines()
    if line.strip() and not line.strip().startswith("#") and "=" in line
]
check("the alembic config sets no version_locations (the split-on-space trap)",
      [key for key in ALEMBIC_LIVE_KEYS if key == "version_locations"], [])
check("it still resolves the script location relative to itself",
      "script_location" in ALEMBIC_LIVE_KEYS, True)
check("and records that re-adding the key needs a separator too",
      "version_path_separator = os" in ALEMBIC_INI_SOURCE, True)

# Alembic loads every revision module to read its ids, and `0001_initial` does
# `from service.db.models import Base` — so the REPO ROOT has to be importable,
# exactly as env.py arranges for the CLI. Without this the walk fails with
# "No module named 'service'", which is a property of the harness rather than of
# the graph.
sys.path.insert(0, str(PKG.parent.parent))

_CFG = _AlembicConfig(str(ALEMBIC_INI))
_SD = _ScriptDirectory.from_config(_CFG)
_DISCOVERED = [script.revision for script in _SD.walk_revisions()]
check("alembic discovers every revision in the tree", len(_DISCOVERED), len(GRAPH))
check("including this one", MINE in _DISCOVERED, True)
check("and reports it as the single head", list(_SD.get_heads()), [_SUPPORTED_HEAD])
check("with 0001_initial still the single base", list(_SD.get_bases()), ["0001_initial"])
check("and the down_revision Alembic resolves is the committed head",
      _SD.get_revision(MINE).down_revision, "0064_user_llm_keys")

MIGRATION_SOURCE = (
    VERSIONS / "20261018_0100_onlyfans_read_bindings.py"
).read_text(encoding="utf-8")
for table in ("of_creator_bindings", "of_conversation_state", "of_message_delivery"):
    check(f"it creates {table}", f'"{table}"' in MIGRATION_SOURCE, True)
check("it creates exactly those three tables",
      _re.findall(r"op\.create_table\(\s*\n\s*\"([a-z_]+)\"", MIGRATION_SOURCE),
      ["of_creator_bindings", "of_conversation_state", "of_message_delivery"])
check("it seeds nothing — no insert of any kind",
      any(token in MIGRATION_SOURCE for token in ("bulk_insert", "op.execute", "INSERT INTO")),
      False)
check("it can be backed out", "def downgrade()" in MIGRATION_SOURCE, True)
check("and its downgrade drops only its own tables",
      sorted(_re.findall(r"op\.drop_table\(\"([a-z_]+)\"\)", MIGRATION_SOURCE)),
      ["of_conversation_state", "of_creator_bindings", "of_message_delivery"])

# The schema facts this repair is about. The ORM side is asserted by compiling
# the real index object for both dialects — the partial predicate has to survive
# compilation, because an index that lost its WHERE clause would silently become
# the global unique constraint it replaces, and a source substring would not
# notice.
from sqlalchemy.schema import CreateIndex as _CreateIndex  # noqa: E402
from sqlalchemy.schema import CreateTable as _CreateTable  # noqa: E402
from sqlalchemy.dialects import postgresql as _pg, sqlite as _sqlite  # noqa: E402
from sqlalchemy import Boolean as _Boolean  # noqa: E402

from db.models import (  # noqa: E402
    OfConversationState as _OfState,
    OfCreatorBinding as _OfBinding,
    OfMessageDelivery as _OfDelivery,
)

ORM_INDEXES = {ix.name: ix for ix in _OfBinding.__table__.indexes}
check("the ORM carries an index named for the ACTIVE binding",
      "uq_of_creator_bindings_active_account" in ORM_INDEXES, True)
_ACTIVE = ORM_INDEXES["uq_of_creator_bindings_active_account"]
check("it is unique", bool(_ACTIVE.unique), True)
check("it is on the account alone", [c.name for c in _ACTIVE.columns], ["account_id"])
_PG_DDL = str(_CreateIndex(_ACTIVE).compile(dialect=_pg.dialect())).strip()
_SQLITE_DDL = str(_CreateIndex(_ACTIVE).compile(dialect=_sqlite.dialect())).strip()
check("PostgreSQL gets a PARTIAL unique index, not a global one",
      _PG_DDL, "CREATE UNIQUE INDEX uq_of_creator_bindings_active_account ON "
      "of_creator_bindings (account_id) WHERE revoked_at IS NULL")
check("and the dialect the relay runs on renders the same predicate",
      _SQLITE_DDL, "CREATE UNIQUE INDEX uq_of_creator_bindings_active_account ON "
      "of_creator_bindings (account_id) WHERE revoked_at IS NULL")
check("the ORM declares no global unique constraint on the account",
      sorted(
          c.name
          for c in _OfBinding.__table__.constraints
          if c.__class__.__name__ == "UniqueConstraint"
      ),
      [])

# And the same three facts as the MIGRATION writes them, since the two must not
# drift: the tests in section 9 exercise the ORM's DDL, and the PostgreSQL
# rehearsal in test_onlyfans_migration_pg.py exercises the migration's.
check("the migration no longer declares a global UNIQUE(account_id)",
      "uq_of_creator_bindings_account_id" in MIGRATION_SOURCE, False)
check("nor the composite scope constraint that blocked re-binding",
      "uq_of_creator_bindings_scope" in MIGRATION_SOURCE, False)
check("it creates the same partial unique index",
      _re.search(
          r"op\.create_index\(\s*\n\s*\"uq_of_creator_bindings_active_account\"",
          MIGRATION_SOURCE,
      )
      is not None,
      True)
check("flagged unique", "unique=True," in MIGRATION_SOURCE, True)
check("with the predicate spelled for PostgreSQL",
      'postgresql_where=sa.text("revoked_at IS NULL")' in MIGRATION_SOURCE, True)
check("and for the dialect the relay actually runs on",
      'sqlite_where=sa.text("revoked_at IS NULL")' in MIGRATION_SOURCE, True)
check("a boolean default is no longer written as an integer literal",
      'sa.Boolean(), nullable=False, server_default=sa.text("0")' in MIGRATION_SOURCE, False)
check("but as the dialect's own false",
      "server_default=sa.false()" in MIGRATION_SOURCE, True)
check("the upgrade is guarded against the table already existing",
      MIGRATION_SOURCE.count("not in existing") >= 3, True)

# The boolean server default, again by compiling the real column rather than by
# matching a string. `revision INTEGER DEFAULT 0` is correct and is left alone;
# what matters is that the BOOLEAN column does not carry that same 0.
_STATE_PG_DDL = str(_CreateTable(_OfState.__table__).compile(dialect=_pg.dialect()))
_STATE_SQLITE_DDL = str(_CreateTable(_OfState.__table__).compile(dialect=_sqlite.dialect()))
check("the boolean default compiles to PostgreSQL's own false",
      "requires_attention BOOLEAN DEFAULT false NOT NULL" in _STATE_PG_DDL, True)
check("and never to the integer PostgreSQL rejects on a boolean column",
      "requires_attention BOOLEAN DEFAULT 0" in _STATE_PG_DDL, False)
check("SQLite renders that same default as its own false",
      "requires_attention BOOLEAN DEFAULT (0) NOT NULL" in _STATE_SQLITE_DDL, True)
check("while an integer column keeps its integer default, as it should",
      "revision INTEGER DEFAULT 0 NOT NULL" in _STATE_PG_DDL, True)
check("and no other table in this package has a boolean to get wrong",
      [
          c.name
          for table in (_OfBinding.__table__, _OfDelivery.__table__)
          for c in table.columns
          if isinstance(c.type, _Boolean)
      ],
      [])

ENGINE_PATH = PKG.parent / "db" / "engine.py"
_engine_tree = ast.parse(ENGINE_PATH.read_text(encoding="utf-8"))
# Any CALL of something named `upgrade` — not a mention of one. engine.py talks
# about `command.upgrade` in two comments explaining why it does not call it, and
# a substring test cannot tell those from the real thing.
_upgrade_calls = [
    node
    for node in ast.walk(_engine_tree)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    and node.func.attr in ("upgrade", "downgrade")
]
check("nothing applies it automatically at startup — init_db calls no upgrade",
      len(_upgrade_calls), 0)
check("so a deployment that has not migrated fails closed rather than serving empty",
      "storage_unavailable" in (PKG / "repo.py").read_text(encoding="utf-8"), True)


# ══ 9. The binding authority ═══════════════════════════════════════════════
print("\n9. One ACTIVE binding per account, and history that survives rebinding")

from sqlalchemy import create_engine as _create_engine  # noqa: E402
from sqlalchemy import insert as _insert, select as _select, update as _update  # noqa: E402
from sqlalchemy.exc import IntegrityError as _IntegrityError  # noqa: E402

from db.models import (  # noqa: E402
    Account as _Account,
    AccountHealth as _AccountHealth,
    Base as _ModelsBase,
    Chat as _Chat,
    Fan as _Fan,
    Message as _Message,
    VaultItem as _VaultItem,
    VaultSend as _VaultSend,
)

# These are rules about a CONSTRAINT, so they are asserted through the real
# repository against a real database. A Python-side fake would agree with itself
# whatever the schema said, and the whole defect lived in the schema: a global
# UNIQUE(account_id) that counted REVOKED rows, so an account could be unlinked
# and then never linked again — to anybody.
#
# The tables come from the relay's own models, which is the metadata the
# migration mirrors; `test_onlyfans_migration_pg.py` rehearses the migration's
# own DDL against PostgreSQL.
#
# The relay's async engine is a process-wide singleton, so this phase checks
# connections out of the same pool section 6 did, from its own event loop. That
# is fine for SQLite — the driver creates its future on whichever loop is
# calling, and the phases are sequential — and it is what the production process
# does too, since the relay serves every request from one loop.
from db.onlyfans_models import OfRuntime as _OfRuntime
from db.onlyfans_connection_models import OfProviderConnection as _OfProviderConnection
_SCHEMA_ENGINE = _create_engine(os.environ["CHATTERLY_DB_URL"])
_ModelsBase.metadata.create_all(
    _SCHEMA_ENGINE,
    tables=[
        _Account.__table__,
        _AccountHealth.__table__,
        _Chat.__table__,
        _Fan.__table__,
        _Message.__table__,
        _VaultItem.__table__,
        _VaultSend.__table__,
        _OfBinding.__table__,
        _OfState.__table__,
        _OfDelivery.__table__,
        _OfRuntime.__table__,
        _OfProviderConnection.__table__,
    ],
)

_T0 = NOW - timedelta(days=10)
_T1 = NOW - timedelta(days=4)
_T2 = NOW - timedelta(days=2)
_T3 = NOW - timedelta(days=1)
_T4 = NOW - timedelta(hours=6)


def _sql(statement):
    with _SCHEMA_ENGINE.begin() as conn:
        result = conn.execute(statement)
        # An INSERT/UPDATE returns no rows, and asking `.all()` of one raises
        # ResourceClosedError — which would be this helper's bug, not the
        # schema's.
        return result.all() if result.returns_rows else None


def _bind(binding_id, account_id, *, tenant, creator, revoked_at=None):
    return _sql(
        _insert(_OfBinding).values(
            id=binding_id,
            tenant_id=tenant,
            creator_id=creator,
            creator_name=creator,
            account_id=account_id,
            handle=f"handle-{account_id}",
            channel="onlyfans",
            revoked_at=revoked_at,
        )
    )


def _attempt_bind(*args, **kwargs):
    """The IntegrityError a refused binding raises, or None if it was accepted."""
    try:
        _bind(*args, **kwargs)
    except _IntegrityError as exc:
        return exc
    return None


async def _binding_authority_cases():
    """Every case in one event loop, so the pool is used from one loop only."""
    repo = SqlOnlyFansReadRepo()
    out: dict[str, object] = {}

    # The read JOINs `accounts`, so the relay's own row has to exist.
    _sql(_insert(_Account).values(id="9001"))
    _sql(_insert(_Account).values(id="9002"))
    _sql(_insert(_Account).values(id="9003"))

    # 1. A live binding is one the real repository returns.
    _bind("ofb-9001-a", "9001", tenant="agency-a", creator="creator-ava", revoked_at=None)
    out["live"] = [b.account_id for b in await repo.list_bindings(
        tenant_id=None, creator_id=None, account_id=None)]
    out["live_creator"] = (await repo.get_binding(tenant_id="agency-a", account_id="9001")).creator_id

    # 2. A SECOND ACTIVE binding is refused — under the same tenant and creator,
    #    under a different creator, and from a different tenant entirely. The
    #    rule is per ACCOUNT, not per scope.
    out["second_same_scope"] = _attempt_bind(
        "ofb-9001-b", "9001", tenant="agency-a", creator="creator-ava")
    out["second_other_creator"] = _attempt_bind(
        "ofb-9001-c", "9001", tenant="agency-a", creator="creator-nova")
    out["second_other_tenant"] = _attempt_bind(
        "ofb-9001-d", "9001", tenant="agency-b", creator="creator-bea")
    out["still_one_active"] = len(await repo.list_bindings(
        tenant_id=None, creator_id=None, account_id="9001"))

    # 3. Revoke, then bind the SAME account to the SAME tenant and creator.
    #    This is the legitimate re-link the composite scope constraint used to
    #    make impossible, and the reason the global unique rule had to go.
    _sql(_update(_OfBinding).where(_OfBinding.id == "ofb-9001-a").values(revoked_at=_T1))
    out["rebind_same_scope"] = _attempt_bind(
        "ofb-9001-e", "9001", tenant="agency-a", creator="creator-ava")

    # 4. Revoke again and rebind to a DIFFERENT creator: reads must resolve the
    #    ACTIVE binding, not the first row or the oldest one.
    _sql(_update(_OfBinding).where(_OfBinding.id == "ofb-9001-e").values(revoked_at=_T2))
    _bind("ofb-9001-f", "9001", tenant="agency-a", creator="creator-nova", revoked_at=None)
    out["rebound_creator"] = (
        await repo.get_binding(tenant_id="agency-a", account_id="9001")).creator_id
    out["rebound_is_the_only_live_one"] = [
        b.binding_id for b in await repo.list_bindings(
            tenant_id="agency-a", creator_id=None, account_id="9001")
    ]
    # ...and the superseded creator no longer answers for that account.
    out["old_creator_scope_is_empty"] = await repo.list_bindings(
        tenant_id="agency-a", creator_id="creator-ava", account_id="9001")

    # 5. Revoke once more and hand the account back to the FIRST creator, so the
    #    suite ends with one live binding and a history of three closed ones.
    _sql(_update(_OfBinding).where(_OfBinding.id == "ofb-9001-f").values(revoked_at=_T3))
    _bind("ofb-9001-g", "9001", tenant="agency-a", creator="creator-ava", revoked_at=None)

    history = _sql(
        _select(_OfBinding.id, _OfBinding.tenant_id, _OfBinding.creator_id, _OfBinding.revoked_at)
        .where(_OfBinding.account_id == "9001")
        .order_by(_OfBinding.id)
    )
    out["history_ids"] = [row[0] for row in history]
    out["history_revoked"] = [row[0] for row in history if row[3] is not None]
    out["history_owner_intact"] = (history[0][1], history[0][2])
    out["final_live_rows"] = [
        b.binding_id for b in await repo.list_bindings(
            tenant_id=None, creator_id=None, account_id="9001")
    ]

    # 7. An account whose ONLY binding is revoked reads exactly like one that was
    #    never bound: refused. "We used to own this" is history, not access.
    _bind("ofb-9002-a", "9002", tenant="agency-a", creator="creator-ava", revoked_at=_T4)
    out["revoked_only_is_none"] = await repo.get_binding(tenant_id="agency-a", account_id="9002")
    out["never_bound_is_none"] = await repo.get_binding(tenant_id="agency-a", account_id="9003")
    out["revoked_account_listed"] = await repo.list_bindings(
        tenant_id="agency-a", creator_id=None, account_id="9002")

    # 8. And the tenant with no bindings at all is a legitimate EMPTY list, which
    #    must stay distinguishable from the two refusals above.
    out["other_tenant_empty"] = await repo.list_bindings(
        tenant_id="agency-b", creator_id=None, account_id=None)
    return out


BIND = run(_binding_authority_cases())

check("a live binding is what the real repository returns",
      BIND["live"], ["9001"])
check("and it resolves the creator that binding names",
      BIND["live_creator"], "creator-ava")

check("a second ACTIVE binding in the same scope is refused",
      type(BIND["second_same_scope"]), _IntegrityError)
check("a second ACTIVE binding for a different creator is refused",
      type(BIND["second_other_creator"]), _IntegrityError)
check("a second ACTIVE binding from another tenant is refused too",
      type(BIND["second_other_tenant"]), _IntegrityError)
check("so the account still has exactly one live binding",
      BIND["still_one_active"], 1)

check("after a revocation the same scope can be bound again",
      BIND["rebind_same_scope"], None)
check("and a different creator can take it over after that",
      BIND["rebound_creator"], "creator-nova")
check("reads resolve the ACTIVE binding and only that one",
      BIND["rebound_is_the_only_live_one"], ["ofb-9001-f"])
check("so the superseded creator no longer answers for the account",
      BIND["old_creator_scope_is_empty"], [])

check("every revoked binding is still present",
      BIND["history_ids"],
      ["ofb-9001-a", "ofb-9001-e", "ofb-9001-f", "ofb-9001-g"])
check("with its revocation recorded",
      BIND["history_revoked"], ["ofb-9001-a", "ofb-9001-e", "ofb-9001-f"])
check("and its original owner intact",
      BIND["history_owner_intact"], ("agency-a", "creator-ava"))
check("while the live set is still exactly one row",
      BIND["final_live_rows"], ["ofb-9001-g"])

check("an account whose only binding was revoked reads as unbound",
      BIND["revoked_only_is_none"], None)
check("exactly as an account that was never bound does",
      BIND["never_bound_is_none"], None)
check("and it is absent from the account list",
      BIND["revoked_account_listed"], [])
check("a tenant with no bindings is a legitimate empty list, not a refusal",
      BIND["other_tenant_empty"], [])


async def _durable_receipt_cases():
    """Read actual scoped receipts without expanding the 100-message window."""
    repo = SqlOnlyFansReadRepo()
    for fan_id in (8101, 8102, 8103):
        _sql(_insert(_Fan).values(account_id="9001", fan_id=fan_id))
        _sql(_insert(_Chat).values(account_id="9001", fan_id=fan_id,
                                  last_message_id=10105, last_message_at=NOW))
    _sql(_insert(_Message).values([
        {"account_id": "9001", "fan_id": 8101, "message_id": message_id,
         "direction": "out", "body": "synthetic text", "created_at": NOW}
        for message_id in range(10001, 10106)
    ]))
    for fan_id, message_id in ((8102, 20001), (8103, 30001)):
        _sql(_insert(_Message).values(account_id="9001", fan_id=fan_id,
                                     message_id=message_id, direction="out",
                                     body="synthetic text", created_at=NOW))
    receipt_time = NOW - timedelta(days=2)
    _sql(_insert(_OfDelivery).values([
        {"account_id": "9001", "fan_id": 8101, "message_id": 10001,
         "outcome": "unknown", "attempt_id": "old-unresolved",
         "evidence_ref": None, "origin": None, "recorded_at": receipt_time},
        {"account_id": "9001", "fan_id": 8101, "message_id": 10104,
         "outcome": "sent", "attempt_id": "accepted-ai",
         "evidence_ref": "synthetic-ai-receipt", "origin": "ai", "recorded_at": NOW},
        {"account_id": "9001", "fan_id": 8101, "message_id": 10105,
         "outcome": "sent", "attempt_id": "accepted-human",
         "evidence_ref": "synthetic-human-receipt", "origin": "human", "recorded_at": NOW},
        {"account_id": "9001", "fan_id": 8102, "message_id": 20001,
         "outcome": "partial", "attempt_id": None,
         "evidence_ref": None, "origin": None, "recorded_at": NOW},
        {"account_id": "9001", "fan_id": 8103, "message_id": 30001,
         "outcome": "unknown", "attempt_id": "unrelated-receipt",
         "evidence_ref": None, "origin": None, "recorded_at": NOW},
    ]))
    _sql(_insert(_OfState).values(account_id="9001", fan_id=8101,
                                 mode="automated", revision=9, updated_at=NOW))
    _sql(_insert(_OfState).values(account_id="9001", fan_id=8103,
                                 mode="paused", revision=3,
                                 unresolved_attempt_id="authority-pointer", updated_at=NOW))

    # A historic accepted pack had its files in VaultSend while Message stayed
    # empty. The read must recover only this account/fan/message's files.
    _sql(_update(_Message).where(_Message.account_id == "9001", _Message.fan_id == 8101,
        _Message.message_id == 10103).values(price_cents=500, is_paid=False))
    _sql(_insert(_VaultItem).values(account_id="9001", media_id=7001, kind="photo", created_at=NOW))
    _sql(_insert(_VaultSend).values([
        {"account_id": "9001", "fan_id": 8101, "message_id": 10103, "media_id": 7001, "price_cents": 500},
        {"account_id": "9001", "fan_id": 8102, "message_id": 10103, "media_id": 7999, "price_cents": 500},
        {"account_id": "9002", "fan_id": 8101, "message_id": 10103, "media_id": 7998, "price_cents": 500},
    ]))
    detail = (await S.get_conversation(
        repo, AGENCY_A, account_id="9001", conversation_id="9001:8101", now=NOW
    ))["conversation"]
    check("a durable old receipt does not expand the visible thread",
          (len(detail["messages"]), detail["messages"][0]["messageId"]), (100, "10006"))
    check("an unresolved receipt outside that thread remains visible as an incident",
          (detail["deliveryIncident"]["attemptId"], detail["deliveryIncident"]["recordedAt"]),
          ("old-unresolved", C.iso(receipt_time)))
    check("an old unresolved receipt flags its conversation for attention",
          (detail["summary"]["requiresAttention"], detail["summary"]["revision"]), (True, 9))
    actual_messages = {message["messageId"]: message for message in detail["messages"]}
    check("historic paid attachments are recovered without crossing fan or account scope",
          actual_messages["10103"]["attachments"],
          {"count": 1, "media": [{"mediaId": "7001", "kind": "photo"}],
           "price": {"currency": "USD", "minorUnits": 500}, "purchaseStatus": "not_purchased"})
    from dataclasses import replace
    paid = replace(_msg(900205, "out", "paid"), price_cents=500, is_paid=True,
                   media_count=1, media=((7001, "photo"),))
    check("accepted send status and purchase status remain separate",
          P.message_model(conversation_id="9001:8101", message=paid, recorded=None)["attachments"]["purchaseStatus"], "purchased")
    check("an unknown purchase never becomes a confirmed non-purchase",
          P.message_model(conversation_id="9001:8101", message=replace(paid, is_paid=None), recorded=None)["attachments"]["purchaseStatus"], "unconfirmed")
    from onlyfans_read.repo import _message_media_ids
    for invalid in ("not-json", "[true]", "[0]", "[1,1]", "[1.5]", "{}"):
        check("malformed media never fabricates attachment identities: " + invalid, _message_media_ids(invalid), [])

    check("SQL reads the accepted manual receipt's authorship and evidence",
          (actual_messages["10105"]["origin"], actual_messages["10105"]["delivery"],
           actual_messages["10105"]["evidenceRef"]),
          ("human", "sent", "synthetic-human-receipt"))
    check("SQL reads the accepted automated receipt's authorship and evidence",
          (actual_messages["10104"]["origin"], actual_messages["10104"]["delivery"],
           actual_messages["10104"]["evidenceRef"]),
          ("ai", "sent", "synthetic-ai-receipt"))
    missing_control = await repo.conversation_state(account_id="9001", fan_id=8102)
    check("an unsettled receipt with no control row derives a paused attention state",
          (missing_control.mode, missing_control.revision, missing_control.requires_attention,
           missing_control.unresolved_attempt_id), ("paused", 0, True, "message:20001"))
    explicit = await repo.conversation_state(account_id="9001", fan_id=8103)
    check("an explicit authority pointer is preserved over a different receipt",
          (explicit.unresolved_attempt_id, explicit.revision), ("authority-pointer", 3))
    check("a receipt from another fan cannot derive a hold for this fan",
          await repo.conversation_state(account_id="9001", fan_id=8199), None)
    check("a receipt from another account cannot derive a hold for this account",
          await repo.conversation_state(account_id="9002", fan_id=8101), None)
    check("the derived old hold never writes a missing control pointer",
          _sql(_select(_OfState.unresolved_attempt_id, _OfState.requires_attention)
               .where(_OfState.account_id == "9001", _OfState.fan_id == 8101)), [(None, False)])
    check("the derived paused state never creates a missing control row",
          _sql(_select(_OfState.fan_id).where(_OfState.account_id == "9001",
                                            _OfState.fan_id == 8102)), [])


run(_durable_receipt_cases())


# ══ 10. The relay application itself ═══════════════════════════════════════
print("\n10. The relay constructs, mounts the router read-only, and fails closed")

# `import server` builds the REAL FastAPI app: every router, every middleware,
# everything. It used to raise at service/llm_models.py — a frozen dataclass
# defaulting to a MappingProxyType, which `@dataclass` refuses — so nothing
# downstream of it could be asserted at all, and the router's registration was
# only ever verified by reading server.py. The llm_models repair removes that
# blocker, so these are assertions about the constructed application.
import onlyfans_read.api as API  # noqa: E402  (same module object section 7 patched)
API._repo = _PRODUCTION_REPO_FACTORY  # the real repository, for the real app

import server as RELAY  # noqa: E402

APP_ROUTES = [
    (getattr(route, "path", ""), tuple(sorted(getattr(route, "methods", ()) or ())))
    for route in RELAY.app.routes
]
OF_ROUTES = sorted((path, methods) for path, methods in APP_ROUTES if C.is_service_path(path))
check("the OnlyFans router is mounted on the real application", len(OF_ROUTES), 9)
check("with exactly the routes the package declares", [p for p, _ in OF_ROUTES], [
    f"{C.SERVICE_PREFIX}/accounts",
    f"{C.SERVICE_PREFIX}/accounts/{{account_id}}/conversations",
    f"{C.SERVICE_PREFIX}/accounts/{{account_id}}/conversations/{{conversation_id}}",
    f"{C.SERVICE_PREFIX}/accounts/{{account_id}}/conversations/{{conversation_id}}/handover",
    f"{C.SERVICE_PREFIX}/accounts/{{account_id}}/ledger",
    f"{C.SERVICE_PREFIX}/accounts/{{account_id}}/operations",
    f"{C.SERVICE_PREFIX}/accounts/{{account_id}}/workspace/{{section}}",
    f"{C.SERVICE_PREFIX}/earnings",
    f"{C.SERVICE_PREFIX}/status",
])
check("every one of them is a read",
      sorted({m for _, methods in OF_ROUTES for m in methods} - {"GET", "HEAD"}), [])
check("no write method exists anywhere in the namespace",
      [(p, methods) for p, methods in APP_ROUTES
       if C.is_service_path(p) and set(methods) & {"POST", "PUT", "PATCH", "DELETE"}],
      [])
check("and the rest of the application exposes no OnlyFans write route either",
      [p for p, methods in APP_ROUTES
       if p.startswith("/onlyfans") and set(methods) & {"POST", "PUT", "PATCH", "DELETE"}],
      [])
check("the namespace is a prefix of its own and not of a neighbour",
      (C.is_service_path(C.SERVICE_PREFIX), C.is_service_path(C.SERVICE_PREFIX + "/x"),
       C.is_service_path(C.SERVICE_PREFIX + "0"), C.is_service_path("/onlyfans/other")),
      (True, True, False, False))

# `TestClient` is used without a context manager, so the lifespan does NOT run:
# no `init_db()`, no schema is created, no startup side effect. Every answer
# below is the router, the gate and the repository — nothing else.
REAL = TestClient(RELAY.app)

# Missing credentials, through the application rather than through a fixture app.
os.environ.pop("ONLYFANS_API_TOKEN", None)
NO_TOKEN = REAL.get(f"{C.SERVICE_PREFIX}/accounts", headers=headers())
check("with no token the real application is unavailable", NO_TOKEN.status_code, 503)
check("and it answers with no accounts array at all", "accounts" in NO_TOKEN.json(), False)

# A token with no tenant, which is the state this repair made fail closed.
os.environ["ONLYFANS_API_TOKEN"] = TOKEN
os.environ.pop("ONLYFANS_API_TENANT", None)
NO_TENANT = REAL.get(f"{C.SERVICE_PREFIX}/accounts", headers=headers())
check("a token without a tenant is unavailable through the real application",
      NO_TENANT.status_code, 503)
check("and it names the decision that is missing, not a record",
      NO_TENANT.json()["code"], "tenant_not_configured")
check("nor is there an account list to misread",
      "accounts" in NO_TENANT.json(), False)
check("the same is true of a list read for a specific account",
      REAL.get(f"{C.SERVICE_PREFIX}/accounts?account=9001", headers=headers()).status_code, 503)

# With the decision made, the same application reads the same binding.
os.environ["ONLYFANS_API_TENANT"] = "agency-a"
LIVE = REAL.get(f"{C.SERVICE_PREFIX}/accounts", headers=headers())
check("with a decided tenant the real repository is read", LIVE.status_code, 200)
check("and the live binding is what comes back",
      [a["accountId"] for a in LIVE.json()["accounts"]], ["9001"])
check("so the gate was the only thing withholding it",
      LIVE.json()["accounts"][0]["binding"]["creatorId"], "creator-ava")

check("an account whose only binding was revoked is refused, not listed",
      REAL.get(f"{C.SERVICE_PREFIX}/accounts?account=9002", headers=headers()).status_code, 404)
check("and it is indistinguishable from one that was never bound",
      REAL.get(f"{C.SERVICE_PREFIX}/accounts?account=9003", headers=headers()).status_code, 404)

os.environ["ONLYFANS_API_TENANT"] = "agency-b"
check("another tenant's live account is refused for this one",
      REAL.get(f"{C.SERVICE_PREFIX}/accounts?account=9001", headers=headers()).status_code, 404)
check("a tenant with no bindings gets a legitimate EMPTY list, not a refusal",
      REAL.get(f"{C.SERVICE_PREFIX}/accounts", headers=headers()).json()["accounts"], [])
os.environ.pop("ONLYFANS_API_TENANT", None)


# ══ Result ═════════════════════════════════════════════════════════════════
faulthandler.cancel_dump_traceback_later()
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    for failure in FAILURES:
        print(f"   - {failure}")
    sys.exit(1)
print("all cases passed — OnlyFans read service (OF-B1)")
