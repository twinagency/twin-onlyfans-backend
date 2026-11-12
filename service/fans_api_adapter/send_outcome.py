"""Conservative mapping of documented message-send error fields.

Provider docs: https://docs.onlyfansapi.com/introduction/essentials/idempotency
This module does not send requests, infer acceptance from a success status, or
resolve earlier attempts. Durable history is evaluated separately.

It also builds the typed, SANITIZED evidence kept for every send the provider did not prove accepted or not sent.
Evidence is descriptive only: it never changes an outcome, releases a fence or authorises a retry.
"""
from dataclasses import dataclass
import json
import re

from conversation_engine.recovery import Outcome

EVIDENCE_PREFIX = "send-evidence:"
EVIDENCE_MAX = 256            # of_text_intents.evidence_ref is VARCHAR(256)
BODY_MAX = 120
UNKNOWN_REASONS = frozenset({
    "provider_rejected_4xx", "provider_ambiguous_5xx", "malformed_success", "transport_exception",
    "provider_idempotency_conflict", "provider_key_mismatch", "provider_unexpected_status", "invalid_receipt",
    "ambiguous_transport"})
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s\"',;}]+")
_ASSIGNED = re.compile(r"""(?i)((?:authorization|api[_-]?key|token|secret|password|signature)["']?\s*[:=]\s*)("[^"]*"|[^\s,;}&]+)""")
_PREFIXED = re.compile(r"\b(?:sk|pk|ak|ofapi)[-_][A-Za-z0-9_-]{8,}")
_TOKENISH = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_SAFE = re.compile(r"[A-Za-z0-9._:-]+")


@dataclass(frozen=True)
class SendEvidence:
    reason: str
    http_status: int | None = None
    provider_code: str | None = None
    request_outcome: str | None = None
    trace_id: str | None = None
    exception_class: str | None = None
    body_excerpt: str | None = None


def sanitize_excerpt(raw, secrets=(), limit=BODY_MAX):
    """Printable-ASCII, single-line, secret-redacted, size-capped excerpt of a provider body (or None)."""
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw[:4096]).decode("utf-8", "replace")
    elif isinstance(raw, str):
        text = raw[:4096]
    else:
        return None
    for secret in secrets:
        if isinstance(secret, str) and secret:
            for variant in {secret, json.dumps(secret, ensure_ascii=True)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]}:
                if variant:
                    text = text.replace(variant, "[redacted]")
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _ASSIGNED.sub(lambda m: m.group(1) + "[redacted]", text)
    text = _PREFIXED.sub("[redacted]", text)
    text = _TOKENISH.sub("[redacted]", text)
    text = "".join(c if 32 <= ord(c) < 127 else " " for c in text)
    return re.sub(r"\s+", " ", text).strip()[:limit] or None


def _safe(value, limit):
    if type(value) is str and 1 <= len(value) <= limit and _SAFE.fullmatch(value):
        return value
    return None


def unknown_reason(state, status, exception_class):
    """Why a send stayed unproven. Naming only: every one of these keeps the fence."""
    if exception_class is not None:
        return "transport_exception"
    if state == "key_mismatch":
        return "provider_key_mismatch"
    if state == "conflict":
        return "provider_idempotency_conflict"
    if type(status) is int and 200 <= status < 300:
        return "malformed_success"
    if type(status) is int and 400 <= status < 500:
        return "provider_rejected_4xx"
    if type(status) is int and status >= 500:
        return "provider_ambiguous_5xx"
    return "provider_unexpected_status"


def build_evidence(state, status, body, raw, trace_id, exception_class, secrets=()):
    """Evidence for a send that is neither proven accepted nor proven not sent. ``body`` is the parsed JSON or None."""
    code = outcome = None
    if type(body) is dict:
        error = body.get("error")
        if type(error) is dict:
            error = error.get("code")
        code = _safe(error if type(error) is str else str(error) if type(error) is int else None, 48) or _safe(body.get("code"), 48)
        outcome = _safe(body.get("request_outcome"), 32)
    return SendEvidence(
        reason=unknown_reason(state, status, exception_class),
        http_status=status if type(status) is int and 100 <= status <= 599 else None,
        provider_code=code, request_outcome=outcome, trace_id=_safe(trace_id, 64),
        exception_class=_safe(exception_class, 48), body_excerpt=sanitize_excerpt(raw, secrets))


def encode_evidence(evidence):
    """Compact JSON, re-sanitized and shrunk so the whole string always fits the 256-character column."""
    status = evidence.http_status
    fields = {"reason": evidence.reason if evidence.reason in UNKNOWN_REASONS else "invalid_receipt",
              "status": status if type(status) is int and 100 <= status <= 599 else None,
              "code": _safe(evidence.provider_code, 48), "outcome": _safe(evidence.request_outcome, 32),
              "trace": _safe(evidence.trace_id, 64), "exc": _safe(evidence.exception_class, 48),
              "body": sanitize_excerpt(evidence.body_excerpt)}
    fields = {k: v for k, v in fields.items() if v is not None}

    def dump():
        return EVIDENCE_PREFIX + json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    while len(dump()) > EVIDENCE_MAX and fields.get("body"):
        fields["body"] = fields["body"][:len(fields["body"]) // 2]
        if not fields["body"]:
            del fields["body"]
    for drop in ("outcome", "trace", "code", "exc"):
        if len(dump()) <= EVIDENCE_MAX:
            break
        fields.pop(drop, None)
    return dump()


def classify_send_error(status: int | None, body: object) -> Outcome:
    if type(status) is not int or type(body) is not dict:
        return Outcome.UNKNOWN
    error = body.get("error")
    request_outcome = body.get("request_outcome")
    if status == 422 and error == "IDEMPOTENCY_KEY_MISMATCH":
        return Outcome.KEY_MISMATCH
    if status == 409 and error == "IDEMPOTENCY_CONFLICT":
        return Outcome.CONFLICT
    if status == 503 and error == "IDEMPOTENCY_UNAVAILABLE" and request_outcome in (None, "not_sent"):
        return Outcome.NOT_SENT
    if status == 503 and error == "SERVICE_UNAVAILABLE" and request_outcome == "not_sent":
        return Outcome.NOT_SENT
    return Outcome.UNKNOWN
