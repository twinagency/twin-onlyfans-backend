"""Turn a persisted Fastt `Message` plus its original provider frame into a Twin
bridge event - or refuse it.

This is the ONE eligibility/normalization path. The ingestion hook passes the
live frame; the reconciliation passes the frame persisted in
`messages.raw_json`. Both go through the same `frame_evidence` ->
`build_candidate` -> `eligibility_problem` -> `build_event` chain, so the two can
never disagree about which DM qualifies or what the frozen event says.

Two rules drive the shape of this module:

* Provider evidence is preserved BEFORE any lossy conversion. An id is checked
  as the provider sent it - `int()` coercion is what lets `1002.9` become 1002
  and `True` become 1, silently claiming a different message or a different fan.
* The provider instant comes from the original `createdAt` string (an offset is
  CONVERTED, not stripped). The stored `created_at` column is never used as a
  provider time, because the transcoder substitutes `now` when `createdAt` is
  missing - which would turn an undated replay into new work.

Nothing here fetches a link or media, truncates oversized text into an
apparently complete event, or invents a timestamp.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from . import contract
from .errors import ContractError
from .scope import BridgeScope

# ── HTML -> deterministic plain text ──────────────────────────────────

_BR = re.compile(r"(?i)<br\s*/?>")
_BLOCK_END = re.compile(r"(?i)</(?:p|div|li|tr|h[1-6]|blockquote|section)\s*>")
_TAG = re.compile(r"<[^>]*>")
_BLANK_RUN = re.compile(r"\n{3,}")

# `responseType` values seen on a plain DM. Anything else is an event shape this
# bridge does not answer (reactions, system rows, ...).
PLAIN_RESPONSE_TYPE = "message"


def html_to_text(raw: str | None) -> str:
    """Deterministic plain text from OF's minimal chat HTML.

    Tags are removed BEFORE entity decoding, so a literal `&lt;b&gt;` the fan
    typed survives as text instead of being deleted as markup. Block ends become
    line breaks; runs of blank lines collapse; NBSP becomes a normal space.
    """
    if not raw:
        return ""
    text = _BR.sub("\n", raw)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


# ── Timestamps ────────────────────────────────────────────────────────

def provider_timestamp_to_utc(value: object) -> datetime | None:
    """A PROVIDER time -> naive UTC, honouring a real offset. None when unusable.

    Deliberately NOT the transcoder's `_parse_iso`: that helper does
    `.replace(tzinfo=None)` and so DISCARDS the offset without converting it,
    which would shift an offset-bearing `createdAt` by the offset. A provider
    value with no timezone at all is ambiguous here and is refused.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return None
    try:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        # Converting an extreme offset (e.g. 0001-01-01T00:00:00+14:00) steps
        # outside the representable range. That is an unusable provider instant,
        # not a reason to abort the caller's transaction.
        return None


def stored_utc(value: object) -> datetime | None:
    """A Fastt COLUMN value -> naive UTC, tolerating junk.

    Fastt stores naive UTC, so a naive value is read as UTC (its documented
    convention). Unlike `provider_timestamp_to_utc`, a bare string is accepted
    here because this is our own column, not a provider promise. Returns None
    for anything unreadable, so one dirty cell cannot take down a page.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        try:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, OverflowError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        # A near-the-edge instant such as `9999-12-31T23:59:59-01:00` normalises
        # past the representable range and raises OverflowError, and
        # `fromisoformat` raises ValueError for year 0. Both are just an
        # unreadable receipt: refuse the cell so a bounded reconciliation keeps
        # advancing to the next valid row instead of aborting the whole page.
        try:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, OverflowError):
            return None
    return dt


# ── Original provider evidence ────────────────────────────────────────

@dataclass(frozen=True)
class ProviderEvidence:
    """The provider frame's own values, before any persistence or coercion."""

    message_id: object
    fan_id: object
    created_at: object
    text: object
    response_type: object
    locked_text: bool
    giphy_id: object
    media: object
    media_count: object
    is_tip: bool
    price: object


def frame_evidence(frame: object) -> ProviderEvidence | None:
    """Extract evidence from one provider frame, or None when there is none.

    Accepts the live frame from the transcoder and the same dict re-parsed from
    `messages.raw_json`, so hook and reconciliation read identical evidence.
    """
    if not isinstance(frame, dict):
        return None
    from_user = frame.get("fromUser")
    if not isinstance(from_user, dict):
        from_user = {}
    return ProviderEvidence(
        message_id=frame.get("id"),
        fan_id=from_user.get("id"),
        created_at=frame.get("createdAt"),
        text=frame.get("text"),
        response_type=frame.get("responseType"),
        locked_text=bool(frame.get("lockedText")),
        giphy_id=frame.get("giphyId"),
        media=frame.get("media"),
        media_count=frame.get("mediaCount"),
        is_tip=bool(frame.get("isTip")),
        price=frame.get("price"),
    )


def frame_evidence_from_raw_json(raw_json: object) -> ProviderEvidence | None:
    """The same evidence, re-parsed from the persisted frame.

    `raw_json` is capped on write, so a truncated frame simply yields no
    evidence and the row is refused - never upgraded into eligible text.
    """
    if not isinstance(raw_json, str) or not raw_json:
        return None
    try:
        parsed = json.loads(raw_json)
    except ValueError:
        return None
    return frame_evidence(parsed)


# ── Candidate + eligibility ───────────────────────────────────────────

@dataclass(frozen=True)
class Candidate:
    """The canonical-column view of one DM, plus its original frame evidence."""

    account_id: str
    fan_id: int
    message_id: int
    direction: str
    body: str
    media_count: int
    media_ids: tuple[int, ...]
    price_cents: int
    is_paid: bool | None
    is_tip: bool
    is_unsent: bool
    receipt: datetime | None          # Fastt's FIRST receipt, naive UTC
    evidence: ProviderEvidence | None = None


def build_candidate(row: object, *, evidence: ProviderEvidence | None = None) -> Candidate | None:
    """Build a Candidate from a `Message`-like row, or None if IDs are unusable."""
    try:
        account_id = str(getattr(row, "account_id"))
        fan_id = int(getattr(row, "fan_id"))
        message_id = int(getattr(row, "message_id"))
    except (TypeError, ValueError, AttributeError):
        return None

    media_ids: tuple[int, ...] = ()
    raw_media = getattr(row, "media_ids", None)
    if isinstance(raw_media, str) and raw_media:
        try:
            parsed = json.loads(raw_media)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            media_ids = tuple(v for v in parsed if isinstance(v, int) and not isinstance(v, bool))

    return Candidate(
        account_id=account_id,
        fan_id=fan_id,
        message_id=message_id,
        direction=str(getattr(row, "direction", "") or ""),
        body=str(getattr(row, "body", "") or ""),
        media_count=int(getattr(row, "media_count", 0) or 0),
        media_ids=media_ids,
        price_cents=int(getattr(row, "price_cents", 0) or 0),
        is_paid=getattr(row, "is_paid", None),
        is_tip=bool(getattr(row, "is_tip", False)),
        is_unsent=bool(getattr(row, "is_unsent", False)),
        receipt=stored_utc(getattr(row, "ingested_at", None)),
        evidence=evidence,
    )


def candidate_text(c: Candidate) -> str:
    """The normalized body, preferring the original frame's text."""
    evidence = c.evidence
    raw = evidence.text if evidence is not None and isinstance(evidence.text, str) else c.body
    return html_to_text(raw)


def candidate_occurred_at(c: Candidate) -> datetime | None:
    """The provider instant, converted from the ORIGINAL string. Never `now`."""
    if c.evidence is None:
        return None
    return provider_timestamp_to_utc(c.evidence.created_at)


def _is_positive(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _frame_has_media(evidence: ProviderEvidence) -> bool:
    media = evidence.media
    if isinstance(media, list) and len(media) > 0:
        return True
    count = evidence.media_count
    if isinstance(count, bool) or not isinstance(count, int):
        return False
    return count > 0


def _frame_is_gif(evidence: ProviderEvidence) -> bool:
    """A Giphy DM: `giphyId` with an empty media array (see of_shapes.giphy_dm_id)."""
    if isinstance(evidence.media, list) and len(evidence.media) > 0:
        return False
    gid = evidence.giphy_id
    return isinstance(gid, str) and bool(gid.strip())


def eligibility_problem(c: Candidate, scope: BridgeScope) -> str | None:
    """Why this DM must NOT become bridge work, or None when it qualifies.

    Every exclusion the assignment calls out is answered here and nowhere else.
    The return value is a short, fixed reason for tests and logs - never fan
    text. The hook and the reconciliation both call this with the same evidence.
    """
    if not scope.enabled or scope.activation_cutoff is None:
        return "scope-disabled"
    if not scope.covers(c.account_id, c.fan_id):
        return "out-of-scope"

    evidence = c.evidence
    if evidence is None:
        return "missing-provider-evidence"

    # Provider identity, validated BEFORE any coercion has a chance to lose it.
    try:
        raw_message = contract.provider_id(evidence.message_id, "provider message id")
        raw_fan = contract.provider_id(evidence.fan_id, "provider fan id")
    except ContractError:
        return "malformed-provider-id"
    if str(raw_message) != str(c.message_id) or str(raw_fan) != str(c.fan_id):
        return "provider-identity-mismatch"

    # Outbound/self echoes. (The transcoder already skips these, but the
    # reconciliation reads rows directly, so the rule lives here too.)
    if c.direction != "in":
        return "not-inbound"
    # System/reaction/unsupported event shapes.
    response_type = evidence.response_type
    if response_type is not None:
        if not isinstance(response_type, str):
            return "unsupported-shape"
        if response_type.strip().lower() != PLAIN_RESPONSE_TYPE:
            return "unsupported-shape"
    # Locked (PPV) content, from the frame's own flag.
    if evidence.locked_text:
        return "locked-text"
    # Tips, in the frame or the persisted row.
    if c.is_tip or evidence.is_tip or _is_positive(evidence.price):
        return "tip" if (c.is_tip or evidence.is_tip) else "paid"
    # Paid / locked content, and a PPV that has been unlocked.
    if c.price_cents > 0 or c.is_paid is True:
        return "paid"
    # Media / attachments / GIFs, even when captioned - from the frame first,
    # then the persisted row (belt and braces for both paths).
    if _frame_has_media(evidence):
        return "media"
    if _frame_is_gif(evidence):
        return "gif"
    if c.media_count > 0 or c.media_ids:
        return "media"
    # Unsent / deleted.
    if c.is_unsent:
        return "unsent"

    # Both activation boundaries, on verified values.
    occurred = candidate_occurred_at(c)
    if occurred is None:
        return "missing-provider-timestamp"
    if occurred < scope.activation_cutoff:
        return "before-cutoff"
    if c.receipt is None:
        return "missing-receipt"
    if c.receipt < scope.activation_cutoff:
        return "receipt-before-cutoff"

    text = candidate_text(c)
    if not text:
        return "empty-text"
    if contract.utf16_len(text) > contract.MAX_TEXT:
        return "oversized-text"
    return None


def build_event(c: Candidate, scope: BridgeScope) -> dict:
    """The frozen, versioned Twin inbound event for an eligible candidate.

    `conversationId = fanId`: for this account/fan chat shape the DM thread IS
    the fan, and account/creator remain separate identity components. The
    returned dict is what Twin's `parseInboundEvent` validates and what the
    stored snapshot must keep byte-for-byte.
    """
    occurred = candidate_occurred_at(c)
    if occurred is None or c.receipt is None:
        raise ContractError("candidate has no verified provider instant or receipt")
    if scope.creator_slug is None or scope.activation_cutoff is None:
        raise ContractError("scope is not fully enabled")

    text = candidate_text(c)
    fan_id = str(c.fan_id)
    return {
        "schemaVersion": contract.SCHEMA_VERSION,
        "channel": contract.CHANNEL,
        "accountId": contract.validate_id(c.account_id, "accountId"),
        "creatorSlug": contract.validate_id(scope.creator_slug, "creatorSlug"),
        "conversationId": contract.validate_id(fan_id, "conversationId"),
        "fanId": contract.validate_id(fan_id, "fanId"),
        "messageId": contract.validate_id(str(c.message_id), "messageId"),
        "occurredAt": contract.canonical_timestamp(occurred),
        "receivedAt": contract.canonical_timestamp(c.receipt),
        "direction": contract.DIRECTION,
        "text": contract.validate_text(text),
    }
