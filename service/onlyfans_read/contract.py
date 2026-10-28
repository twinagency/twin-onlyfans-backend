"""The wire contract between this relay and the Twin dashboard.

Every rule here is one definition used by the router, the service and the
offline suite, so a test can never agree with a copy of the rule that the
running service does not use.

WHY A SCHEMA VERSION IS REQUIRED IN *BOTH* DIRECTIONS. The dashboard's parser
used to shape-check an answer and ignore its version, which means a backend
that kept the field names and changed their meaning would have been accepted
silently — the exact failure the version exists to prevent. So:

  • the client sends `X-OnlyFans-Schema-Version` and the service refuses a
    request that omits it or names a version it does not implement; and
  • the service stamps `schemaVersion` on every envelope, and the dashboard's
    parser refuses an answer whose version is absent or different.

Both halves are asserted in the checks on each side. `SUPPORTED_SCHEMA_VERSIONS`
is a set rather than a single string so a version can be added without a flag
day, and the *emitted* version stays a single value.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone

# ── Versions ────────────────────────────────────────────────────────────────

# The value the dashboard's `ONLYFANS_PREVIEW_SCHEMA_VERSION` declares. These
# two literals are a contract; changing one without the other is a breaking
# change and is detected at runtime in both directions.
SCHEMA_VERSION = "of-preview.v2"

# What this service can serve. Newest first is a habit, not a rule — the
# service emits SCHEMA_VERSION regardless of which supported value the client
# asked for, because a client that asked for an older version would otherwise
# receive a newer body than it validated.
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)

SCHEMA_VERSION_HEADER = "X-OnlyFans-Schema-Version"

# ── Namespace ───────────────────────────────────────────────────────────────

# Deliberately NOT under `/api/of/*`. That prefix is scanned by the relay's
# account-isolation middleware, which answers with a cookie-gated 401 for a
# request with no principal — and a service client has no cookies by design.
# This namespace therefore carries its own, stricter gate (see auth.py) and
# declares itself to the two browser perimeters through `is_service_path`.
SERVICE_PREFIX = "/onlyfans/api/v1"


def is_service_path(path: str) -> bool:
    """True for this service's namespace, and only that.

    Prefix-bounded on purpose: `/onlyfans/api/v1` and `/onlyfans/api/v1/...`
    are in, `/onlyfans/api/v10` and `/onlyfans/whatever` are out. The relay's
    share-token gate consults this instead of repeating the string, so a route
    rename cannot leave a stale hole behind (and cannot silently open a route
    that is not ours).
    """
    return path == SERVICE_PREFIX or path.startswith(SERVICE_PREFIX + "/")


# ── Identity shapes ─────────────────────────────────────────────────────────

# Backend ids are opaque, so this does not guess their format. It rejects what
# cannot be an id: empty, over-long, or carrying anything that would have to be
# escaped or interpreted. The dashboard's own scope parser bounds ids the same
# way, with the same character class, so a value that reaches here has already
# survived one gate.
ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

# Money is an integer count of minor units plus an explicit currency. The
# relay stores `*_cents` for OnlyFans dollars, so the currency is not derived
# from the row — it is stated here, once.
MONEY_CURRENCY = "USD"

# Paging bounds. `DEFAULT_LIMIT` is what a client that asks for nothing gets;
# `MAX_LIMIT` is what it cannot exceed, so one request cannot pull an account's
# whole history.
DEFAULT_LIMIT = 25
MAX_LIMIT = 100

# ── Vocabularies ────────────────────────────────────────────────────────────

# These four sets are the dashboard's own unions. A value outside them is a
# bug on this side, and `_checked` below refuses rather than emitting it —
# a read model that says "sentish" is worse than a failed read.
AUTOMATION_MODES = ("automated", "human_controlled", "paused", "no_contact")
TAKEOVER_STATES = ("none", "requested", "effective")
DELIVERY_OUTCOMES = (
    "none",
    "generating",
    "queued",
    "dispatching",
    "sent",
    "retracted",
    "partial",
    "unknown",
    "refused",
    "failed",
)
CONNECTION_HEALTH = ("connected", "reconnecting", "disconnected", "expired_session", "revoked")

# Outcomes that must hold a conversation: an unresolved send blocks the next
# one, so anything that is not a settled answer is one of these.
UNRESOLVED_OUTCOMES = frozenset({"generating", "queued", "dispatching", "unknown", "partial"})

# Capability states. Everything except `viewConversations` is `not_configured`
# today: this milestone builds no delivery authority, so the honest answer is
# that the operation is not configured rather than that it is available and
# broken.
CAPABILITY_AVAILABLE = "available"
CAPABILITY_UNAVAILABLE = "unavailable"
CAPABILITY_NOT_CONFIGURED = "not_configured"


def vocabulary_contains(values: tuple[str, ...], candidate: str) -> bool:
    return candidate in values


# ── Conversation identity ───────────────────────────────────────────────────

def conversation_id_for(account_id: str, fan_id: int | str) -> str:
    """THE definition of a conversation id.

    A conversation *is* (account, fan) — the relay's `chats` table is keyed on
    exactly that pair, and OnlyFans fan ids are only unique within an account.
    So the id carries both halves: `"<account_id>:<fan_id>"`. Two accounts can
    therefore never mint the same id, and the colon is inside the id character
    class the dashboard's scope parser already accepts.

    Derived rather than stored so a conversation has an id before any control
    row exists for it. Milestone 3 writes `of_conversation_state` rows; the id
    is the same string either way, which is what lets a URL from before that
    still resolve.
    """
    return f"{account_id}:{fan_id}"


def fan_id_from_conversation_id(conversation_id: str, account_id: str) -> int | None:
    """Recover the fan id, refusing an id that belongs to another account.

    A conversation id from another account is not a lookup miss — it is a
    cross-account request, and the caller must not be able to tell those two
    apart in the answer. This returns None for both so the service can raise
    the same refusal.
    """
    prefix = f"{account_id}:"
    if not conversation_id.startswith(prefix):
        return None
    tail = conversation_id[len(prefix):]
    if not tail.isdigit() or len(tail) > 20:
        return None
    return int(tail)


# ── Cursors ─────────────────────────────────────────────────────────────────
#
# A cursor is opaque to the browser and decoded here. It carries the account it
# was minted for, so a cursor moved between accounts is refused rather than
# silently paging the wrong account's rows.
#
# It is deliberately NOT signed. The only thing a tampered cursor can do is
# skip or repeat rows *inside the account it was already authorized for* — the
# account is re-checked against the binding authority on every request — so a
# signature would buy integrity of position, not of access, and would need a
# key to rotate. What it must not do is become a channel for a larger limit,
# which is why the limit is a separate, bounded parameter.

_CURSOR_VERSION = 1


def encode_cursor(*, account_id: str, last_activity_at: str | None, fan_id: int, scope_key: str | None = None) -> str:
    raw = json.dumps(
        {"v": _CURSOR_VERSION, "a": account_id, "t": last_activity_at, "f": int(fan_id), **({"s": scope_key} if scope_key else {})},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(value: str, *, account_id: str, scope_key: str | None = None) -> tuple[str | None, int]:
    """Decode a cursor for `account_id`. Raises ValueError on anything else."""
    if not value or len(value) > 512:
        raise ValueError("cursor is empty or absurdly long")
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:  # noqa: BLE001 — any decode failure is one refusal
        raise ValueError("cursor is not valid base64url") from exc
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("cursor is not valid JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("cursor is not an object")
    if body.get("v") != _CURSOR_VERSION:
        raise ValueError("cursor version is not supported")
    if body.get("a") != account_id:
        # The same refusal as a conversation from another account: the caller
        # learns that the cursor is not usable, not what it would have shown.
        raise ValueError("cursor belongs to another account")
    if body.get("s") != scope_key:
        raise ValueError("cursor belongs to another search")
    fan_id = body.get("f")
    if not isinstance(fan_id, int) or isinstance(fan_id, bool) or fan_id < 0:
        raise ValueError("cursor carries no fan id")
    last_activity_at = body.get("t")
    if last_activity_at is not None and not isinstance(last_activity_at, str):
        raise ValueError("cursor carries no timestamp")
    return last_activity_at, fan_id


def clamp_limit(value: int | None) -> int:
    """The page size rule, in one place. Absent means DEFAULT_LIMIT; a value
    below one or above MAX_LIMIT is clamped rather than refused, because a
    client asking for 500 rows is asking for too much of the same thing, not
    for something else."""
    if value is None:
        return DEFAULT_LIMIT
    if value < 1:
        return 1
    return min(value, MAX_LIMIT)


# ── Time ────────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    """Naive UTC, matching every `DateTime` column in this relay."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(value: datetime | None) -> str | None:
    """ISO-8601 with a Z suffix, so the dashboard's `IsoTimestamp` is honest
    about the zone. The relay stores naive UTC; this is where that fact is
    stated rather than assumed by the reader."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="seconds") + "Z"


def parse_iso(value: str) -> datetime:
    """The inverse of `iso`, in the relay's own NAIVE-UTC convention.

    This exists because `datetime.fromisoformat("...Z")` returns an
    *offset-aware* datetime on Python 3.11+, while every `DateTime` column in
    this relay stores naive UTC. Mixing the two is not a type error that
    announces itself: the comparison either raises
    (`TypeError: can't compare offset-naive and offset-aware datetimes`) on the
    keyset page boundary, or — worse, where a driver coerces — silently orders
    rows against a shifted anchor. A cursor decodes to the same kind of value
    the rows are, or it does not decode at all.

    Raises ValueError for anything it cannot read, which the caller turns into
    an `invalid_cursor` refusal.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("no timestamp")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def age_seconds(value: datetime | None, *, now: datetime | None = None) -> float | None:
    if value is None:
        return None
    reference = now or utc_now()
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return max(0.0, (reference - value).total_seconds())


# ── Envelopes ───────────────────────────────────────────────────────────────

def accounts_envelope(*, creator_id: str | None, account_id: str | None, accounts: list[dict]) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "scope": {"creatorId": creator_id, "accountId": account_id},
        "accounts": accounts,
    }


def page_envelope(*, account_id: str, items: list[dict], next_cursor: str | None) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "accountId": account_id,
        "page": {"items": items, "nextCursor": next_cursor},
    }


def conversation_envelope(*, account_id: str, conversation: dict) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "accountId": account_id,
        "conversation": conversation,
    }

def encode_thread_cursor(*, account_id: str, fan_id: int, before_message_id: int) -> str:
    raw = json.dumps({"v":1,"a":account_id,"f":fan_id,"m":before_message_id},
                     separators=(",",":"),sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_thread_cursor(value: str, *, account_id: str, fan_id: int) -> int:
    if not isinstance(value,str) or not value or len(value)>512:
        raise ValueError("invalid thread cursor")
    try:
        body=json.loads(base64.urlsafe_b64decode((value+"="*(-len(value)%4)).encode("ascii")))
    except Exception as exc:
        raise ValueError("invalid thread cursor") from exc
    if (not isinstance(body,dict) or set(body)!={"v","a","f","m"} or body["v"]!=1
            or body["a"]!=account_id or type(body["f"]) is not int or body["f"]!=fan_id
            or type(body["m"]) is not int or not 0<body["m"]<2**63):
        raise ValueError("invalid thread cursor")
    return body["m"]
