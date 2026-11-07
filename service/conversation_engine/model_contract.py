"""Offline qualification contract for an engine proposal; no provider I/O.

The existing account-scoped model registry and cost ledger are not activated
by this module. Real invocation still needs a durable engine-owned reservation,
scoped call authorization, settlement, and separate approval.
"""
import json
from onlyfans_runtime.store import Refused

_MAX_RESPONSE = 20000


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError("nonfinite number")


def parse_proposal(raw):
    """Turn one model JSON object into the same validated proposal as fixtures."""
    from .engine import SyntheticProposal
    if type(raw) is not str:
        raise Refused("model proposal exceeds its contract")
    try:
        if len(raw.encode("utf-8")) > _MAX_RESPONSE:
            raise ValueError("oversized proposal")
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
        if (type(value) is not dict or set(value) - {
                "body", "sales_signal", "item_scope", "action", "offer_grant_id", "parts"}
                or type(value.get("body")) is not str
                or type(value.get("item_scope", [])) is not list
                or type(value.get("parts", [])) is not list
                or type(value.get("sales_signal", "none")) is not str
                or value.get("sales_signal", "none") not in {"none", "decline", "reopen"}
                or type(value.get("action", "reply")) is not str
                or value.get("action", "reply") not in {"reply", "offer"}
                or value.get("offer_grant_id") is not None and type(value.get("offer_grant_id")) is not str
                or any(type(item) is not str for item in value.get("parts", []))
                or any(type(item) is not str for item in value.get("item_scope", []))):
            raise ValueError("unsupported proposal")
        for item in [value["body"], *value.get("parts", []), *value.get("item_scope", [])]:
            item.encode("utf-8")
        return SyntheticProposal(body=value["body"], sales_signal=value.get("sales_signal", "none"),
            item_scope=tuple(value.get("item_scope", [])), action=value.get("action", "reply"),
            offer_grant_id=value.get("offer_grant_id"), parts=tuple(value.get("parts", [])))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise Refused("model proposal does not match its contract") from None


class StructuredFixtureModel:
    """In-memory responses only; deliberately has no callable/network hook."""
    def __init__(self, responses):
        if type(responses) is not dict or any(type(k) is not str or type(v) is not str for k, v in responses.items()):
            raise ValueError("fixture responses required")
        self._responses = dict(responses)
        self.calls = []

    def generate(self, snapshot):
        self.calls.append(snapshot)
        raw = self._responses.get(snapshot["current_message"]["body"])
        if raw is None:
            raise Refused("structured model has no reviewed fixture")
        return parse_proposal(raw)
