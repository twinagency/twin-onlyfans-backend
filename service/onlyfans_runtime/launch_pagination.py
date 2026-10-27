"""Bounded keyset cursors, scoped to the account's current creator binding.

Cursors carry no authority: the caller still acquires Store._runtime first.
Their scope and shape are checked before any query is constructed.
"""
import base64
import json

from .store import Refused


def encode(kind, account, binding, position):
    value = [1, kind, account, binding, position]
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def decode(value, kind, account, binding):
    if value is None:
        return None
    if type(value) is not str or not 1 <= len(value) <= 2048:
        raise Refused("invalid cursor")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        result = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise Refused("invalid cursor") from None
    if (type(result) is not list or len(result) != 5
            or result[:4] != [1, kind, account, binding]):
        raise Refused("invalid cursor scope")
    return result[4]


def query_bounds(params):
    if set(params) - {"cursor", "limit"} or any(len(params.getlist(k)) != 1 for k in params):
        raise Refused("invalid pagination")
    raw = params.get("limit", "25")
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= 100:
        raise Refused("invalid pagination limit")
    return params.get("cursor"), int(raw)
