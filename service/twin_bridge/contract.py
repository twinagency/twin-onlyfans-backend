"""Exact mirror of Twin's accepted bridge wire contract.

Source of truth: Twin `src/channels/types.ts` (Stage 1A/1B, accepted) and
`src/channels/identity.ts` for the key tuples. Nothing here invents a rule; the
values and the rejections are copied so a Python-built event is byte-compatible
with what Twin's `parseInboundEvent` accepts.

The one place Python and JavaScript genuinely differ is string length: Twin
measures JavaScript UTF-16 code units, Python measures code points. Every length
rule below uses `utf16_len` so a non-BMP emoji counts the same on both sides.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from .errors import ContractError

# Twin REQUEST_LIMITS.
SCHEMA_VERSION = 1
MAX_ID = 256            # one identifier, in UTF-16 code units
MAX_TIMESTAMP = 40      # ISO-8601, e.g. 2026-10-15T04:05:06.789Z
MAX_TEXT = 4000         # one inbound fan message, in UTF-16 code units
MAX_TOTAL_BYTES = 16 * 1024  # the whole serialized request object, UTF-8

CHANNEL = "onlyfans"
DIRECTION = "inbound"

# Twin: /[\/\\\u0000-\u001F\u007F]/ - path separators and control characters.
_ID_FORBIDDEN = re.compile("[/\\\\\u0000-\u001f\u007f]")

# Twin: TIMESTAMP_RE - a timezone is mandatory, fraction is optional.
_TIMESTAMP_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$"
)
_DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _is_leap(year: int) -> bool:
    return (year % 4 == 0 and year % 100 != 0) or year % 400 == 0


def _days_in_month(year: int, month: int) -> int:
    return 29 if (month == 2 and _is_leap(year)) else _DAYS_IN_MONTH[month - 1]


def utf16_len(value: str) -> int:
    """Length in UTF-16 code units - what JavaScript's `String.length` reports.

    A non-BMP character is ONE Python code point but TWO UTF-16 units, so a
    naive `len()` would let a Python-built id or text through where Twin's
    parser rejects it (or vice versa).
    """
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in value)


def has_unpaired_surrogate(value: str) -> bool:
    """True for ill-formed Unicode, matching Twin's explicit surrogate scan."""
    i = 0
    n = len(value)
    while i < n:
        c = ord(value[i])
        if 0xD800 <= c <= 0xDBFF:
            nxt = ord(value[i + 1]) if i + 1 < n else -1
            if not (0xDC00 <= nxt <= 0xDFFF):
                return True
            i += 2
            continue
        if 0xDC00 <= c <= 0xDFFF:
            return True
        i += 1
    return False


def validate_id(value: object, field: str) -> str:
    """One identifier as it appears on the wire: a string."""
    if value is None:
        raise ContractError(f"{field} is required")
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string; provider ids are never coerced through Number")
    if not value.strip():
        raise ContractError(f"{field} is blank")
    if utf16_len(value) > MAX_ID:
        raise ContractError(f"{field} exceeds {MAX_ID} characters")
    if value != value.strip():
        raise ContractError(f"{field} has leading or trailing whitespace")
    if value in (".", ".."):
        raise ContractError(f"{field} is a traversal value")
    if _ID_FORBIDDEN.search(value):
        raise ContractError(f"{field} contains a path separator or control character")
    if has_unpaired_surrogate(value):
        raise ContractError(f"{field} contains an unpaired UTF-16 surrogate")
    return value


def validate_text(value: object) -> str:
    """One inbound message body: non-blank, at most 4000 UTF-16 units."""
    if value is None:
        raise ContractError("text is required")
    if not isinstance(value, str):
        raise ContractError("text must be a string")
    if not value.strip():
        raise ContractError("text is blank")
    if utf16_len(value) > MAX_TEXT:
        raise ContractError(f"text exceeds {MAX_TEXT} characters")
    return value


def canonical_timestamp(value: datetime) -> str:
    """Format an instant exactly as Twin stores it: `...SS.mmmZ`, UTC.

    Twin normalises with `Date.prototype.toISOString()`, which always emits
    three fractional digits and a `Z`.
    """
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def parse_canonical_timestamp(value: object, field: str) -> datetime:
    """Parse Twin's timestamp subset into a naive-UTC datetime.

    Mirrors `readTimestamp` + `parseTimestamp`: a mandatory timezone, real
    calendar dates (no rollover, no invalid leap days), bounded components, and
    fractional seconds truncated to milliseconds.
    """
    if value is None:
        raise ContractError(f"{field} is required")
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    if not value.strip():
        raise ContractError(f"{field} is blank")
    if utf16_len(value) > MAX_TIMESTAMP:
        raise ContractError(f"{field} exceeds {MAX_TIMESTAMP} characters")
    if value != value.strip():
        raise ContractError(f"{field} has leading or trailing whitespace")

    m = _TIMESTAMP_RE.match(value)
    if not m:
        raise ContractError(f"{field} must be a timezone-qualified ISO-8601 timestamp such as 2026-10-15T04:00:00Z")

    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour, minute, second = int(m.group(4)), int(m.group(5)), int(m.group(6))
    zone = m.group(8)

    bad = f"{field} must be a timezone-qualified ISO-8601 timestamp such as 2026-10-15T04:00:00Z"
    if month < 1 or month > 12:
        raise ContractError(bad)
    if day < 1 or day > _days_in_month(year, month):
        raise ContractError(bad)
    if hour > 23 or minute > 59 or second > 59:
        raise ContractError(bad)

    offset_minutes = 0
    if zone != "Z":
        sign = -1 if zone[0] == "-" else 1
        off_hour, off_minute = int(zone[1:3]), int(zone[4:6])
        if off_hour > 23 or off_minute > 59:
            raise ContractError(bad)
        offset_minutes = sign * (off_hour * 60 + off_minute)

    millis = int((m.group(7) + "000")[:3]) if m.group(7) else 0
    # `datetime(...)` refuses year 0 outright and the offset arithmetic can
    # underflow past year 1, each raising a bare ValueError/OverflowError. Both
    # are conversion failures on THIS field, so they leave through the same fixed
    # contract error instead of escaping as a raw Python exception.
    try:
        instant = datetime(year, month, day, hour, minute, second, millis * 1000) - timedelta(minutes=offset_minutes)
    except (ValueError, OverflowError) as exc:
        raise ContractError(f"{field} normalises outside the supported year range") from exc
    if not (1 <= instant.year <= 9999):
        raise ContractError(f"{field} normalises outside the supported year range")
    return instant


def _encode_tuple(parts: list[str]) -> str:
    """`"<utf8ByteLength>:<value>"` joined by `|`, so a delimiter inside one
    value can never shift a field boundary onto a different tuple."""
    return "|".join(f"{len(p.encode('utf-8'))}:{p}" for p in parts)


def _hash_tuple(prefix: str, parts: list[str]) -> str:
    return prefix + hashlib.sha256(_encode_tuple(parts).encode("utf-8")).hexdigest()


def conversation_key(channel: str, account_id: str, creator_slug: str, conversation_id: str, fan_id: str) -> str:
    """Twin `conversationKey`: channel + account + creator + conversation + fan."""
    return _hash_tuple("of_", [channel, account_id, creator_slug, conversation_id, fan_id])


def message_key(channel: str, account_id: str, fan_id: str, message_id: str) -> str:
    """Twin `messageKey`: channel + account + fan + provider message id."""
    return _hash_tuple("ofm_", [channel, account_id, fan_id, message_id])


# ── Strict typing helpers ─────────────────────────────────────────────
#
# Python's `bool` IS an `int` subclass, so `isinstance(True, int)` is true. Every
# numeric check below goes through these, so a boolean can never stand in for an
# id, an epoch, a count or a usage figure.

def strict_int(value: object, field: str, *, minimum: int | None = None) -> int:
    """A real integer (never a bool), optionally bounded below."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{field} must be at least {minimum}")
    return value


def strict_number(value: object, field: str, *, minimum: float | None = None) -> float:
    """A finite real number (never a bool), optionally bounded below."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{field} must be a number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        # A JSON integer with hundreds of digits is still valid JSON, and
        # `float()` overflows on it. That is a conversion failure on this field,
        # not a reason to clamp the value or to leak an OverflowError.
        raise ContractError(f"{field} must be finite") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ContractError(f"{field} must be finite")
    if minimum is not None and number < minimum:
        raise ContractError(f"{field} must be at least {minimum}")
    return number


def provider_id(value: object, field: str) -> int:
    """A provider-supplied id: a real JSON integer, never a bool, float or string.

    `int()` coercion is exactly the bug this refuses: `int(1002.9)` becomes 1002
    and `int(True)` becomes 1, so a coerced id can silently claim a different
    message - or a different fan - than the provider actually sent.
    """
    return strict_int(value, field, minimum=1)


def is_canonical_timestamp(value: object) -> bool:
    """True when `value` is a valid Twin-format `...SS.mmmZ` instant."""
    try:
        parse_canonical_timestamp(value, "timestamp")
    except ContractError:
        return False
    return True


def check_schema_version(value: object, field: str = "schemaVersion") -> int:
    """Twin's wire version as the INTEGER 1 - never `True`, which equals 1.

    An equality test alone would accept a boolean, because Python's `True == 1`.
    A malformed envelope must reach the caller as a malformed envelope, not as a
    supported one, so the type is checked before the value.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{field} must be an integer")
    if value != SCHEMA_VERSION:
        raise ContractError(f"{field} is not a supported version")
    return value


def validate_provider_label(value: object, field: str = "provider") -> str:
    """Provider metadata: a NONBLANK string, and nothing more.

    Twin asserts exactly `typeof provider === 'string' && provider.trim()`
    (`src/channels/service.ts`) and then projects the value verbatim, so a label
    such as `synthetic/provider` is legitimate. `validate_id` must NOT be used
    here: its slash, padding and control-character rules belong to path-like
    identifiers and would reject - and could not report - a provider Twin really
    did name. The value is kept byte-for-byte; nothing here normalises it.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    if not value.strip():
        raise ContractError(f"{field} is blank")
    return value
