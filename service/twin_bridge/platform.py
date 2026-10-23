"""Account-bound OnlyFans text adapter: exactly one message POST per attempt.

Ordinary free text only - price zero, unlocked, no media/previews/tags/GIF/quote
and auto-tagging off - sent through Fastt's explicit account client factory and
its established body/signing, but on a NO-RETRY path: `OFClient.send_text_once`
performs one HTTP POST, so the bridge's "one attempt = one message" claim is a
property of the transport, not a hope about a wrapper.

A return value only proves delivery when it is a validated created-message
object with an exact positive provider id. Everything else - an empty object, a
`success:true` envelope, malformed JSON, a float/bool id, a message belonging to
another chat, a timeout, or a connection loss after the POST began - is
uncertain, and uncertainty pauses the conversation instead of retrying.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from .contract import ContractError, parse_canonical_timestamp
from .coordination import guarded_send

log = logging.getLogger("of-relay.twin_bridge.platform")

ONLYFANS_PLATFORM = "onlyfans"
HISTORY_MAX_PAGES = 20
HISTORY_MAX_ITEMS = 1000

# A provider id is a positive integer. It is accepted as a real int (never a
# bool) or as a plain decimal string, and is always returned as the decimal
# STRING so it can never be re-read through a float.
_DECIMAL_ID = re.compile(r"^[0-9]{1,19}$")


class PlatformError(RuntimeError):
    """Base for adapter failures. Carries a fixed reason code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class PlatformRefused(PlatformError):
    """The provider answered and refused: provably NO message was created."""


class PlatformUnknown(PlatformError):
    """Delivery cannot be established: the message may or may not exist."""


@dataclass(frozen=True)
class TextSendResult:
    provider_message_id: str
    evidence: dict


def normalise_provider_id(value: object) -> str | None:
    """A created message's id as a canonical decimal string, or None.

    A bool is rejected explicitly (`True == 1` in Python), a float is rejected
    rather than truncated, and a decimal string is accepted only in plain digit
    form so `"12.5"` or `"1e3"` cannot masquerade as an id.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not _DECIMAL_ID.match(text):
            return None
        number = int(text)
        return str(number) if number > 0 else None
    return None


def escape_plain_text(text: str) -> str:
    """Escape a bridge draft so its fan-visible text is EXACTLY what was written.

    The bridge sends plain text, so every character is literal - including
    HTML-looking strings, ampersands and emoji. `_wrap_text_as_p`'s "already
    HTML" shortcut and its `.strip()` are correct for the legacy composer (an
    operator may intend markup) but wrong here: a draft `<p>literal</p>` would
    be sent as markup and its leading/trailing whitespace would vanish. So the
    bridge escapes unconditionally and preserves the whitespace (only interior
    newlines become `<br />`, matching OF's own editor).
    """
    if not text:
        return text
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br />")
    )
    return f"<p>{escaped}</p>"


def validate_created_message(payload: object, *, chat_id: object,
                             creator_id: object = None) -> TextSendResult:
    """Validate the provider's created-message object against the BOUND identity.

    A created-message response only proves OUR message exists when every field
    the provider DID supply agrees with it. Supplied-but-contradictory evidence -
    a non-boolean direction, an author that is neither the bound creator nor
    absent, a recipient that is not the bound fan, a null/malformed id, or a
    non-object container - is refused rather than treated as delivery. Fields the
    real minimum schema may omit stay legal.
    """
    if not isinstance(payload, dict):
        raise PlatformUnknown("provider-response-not-an-object")
    if not payload:
        raise PlatformUnknown("provider-response-empty")
    provider_id = normalise_provider_id(payload.get("id"))
    if provider_id is None:
        raise PlatformUnknown("provider-id-missing-or-invalid")

    # Direction. A supplied `isFromMe` must be a real boolean; a non-boolean
    # (0/1, "false", null) is malformed, and an explicit False contradicts a send.
    if "isFromMe" in payload:
        direction = payload.get("isFromMe")
        if not isinstance(direction, bool):
            raise PlatformUnknown("provider-response-direction-malformed")
        if direction is False:
            raise PlatformUnknown("provider-response-direction-contradicts-send")

    def _canonical(value: object) -> str:
        if isinstance(value, bool):
            return str(value)
        text = str(value).strip()
        return str(int(text)) if text.isdigit() else text

    fan = _canonical(chat_id)
    creator = _canonical(creator_id) if creator_id is not None else None

    # Author. When supplied it must be a readable id, must NOT be the fan, and -
    # when the bound creator is known - must BE the creator. A third-party author
    # is a different message, not our delivery.
    if "fromUser" in payload:
        author = payload.get("fromUser")
        if not isinstance(author, dict) or "id" not in author:
            raise PlatformUnknown("provider-response-author-malformed")
        author_id = normalise_provider_id(author.get("id"))
        if author_id is None:
            raise PlatformUnknown("provider-response-author-malformed")
        if author_id == fan:
            raise PlatformUnknown("provider-response-authored-by-recipient")
        if creator is not None and author_id != creator:
            raise PlatformUnknown("provider-response-third-party-author")

    # Recipient containers. When supplied they must be objects with a readable id
    # equal to the fan; a null id or a non-object container is malformed.
    for field in ("withUser", "user"):
        if field not in payload:
            continue
        owner = payload.get(field)
        if not isinstance(owner, dict) or "id" not in owner:
            raise PlatformUnknown("provider-response-recipient-malformed")
        other = normalise_provider_id(owner.get("id"))
        if other is None:
            raise PlatformUnknown("provider-response-recipient-malformed")
        if other != fan:
            raise PlatformUnknown("provider-response-wrong-recipient")

    evidence = {
        "id": provider_id,
        # `responseType`/`isFromMe` are recorded only when present; nothing is
        # invented for a field the real response shape does not always carry.
        "responseType": payload.get("responseType"),
        "isFromMe": payload.get("isFromMe"),
        "hasText": isinstance(payload.get("text"), str) and bool(payload.get("text")),
    }
    return TextSendResult(provider_message_id=provider_id, evidence=evidence)


class OnlyFansTextAdapter:
    """Sends one free-text DM per call through an explicit account client."""

    def __init__(self, account_id: str, *, client=None, client_factory=None,
                 history_limit: int = 10) -> None:
        self.account_id = str(account_id)
        self._client = client
        self._client_factory = client_factory
        self._history_limit = int(history_limit)

    # ── identity ─────────────────────────────────────────────────────

    def _factory(self):
        if self._client_factory is not None:
            return self._client_factory
        from client_pool import get as pool_get  # explicit account factory
        return pool_get

    async def client(self):
        if self._client is None:
            self._client = await asyncio.to_thread(self._factory(), self.account_id)
        bound = getattr(self._client, "account_id", None)
        if bound is not None and str(bound) != self.account_id:
            raise PlatformError("client-account-mismatch")
        platform = getattr(self._client, "platform", ONLYFANS_PLATFORM)
        if str(platform) != ONLYFANS_PLATFORM:
            raise PlatformError("unsupported-platform")
        return self._client

    # ── reads (never a message POST) ─────────────────────────────────

    async def _history_page(self, client, chat_id: str | int, *,
                            limit: int, before_id: int | None = None) -> tuple[dict, list[dict]]:
        """Read and validate the common page envelope without interpreting coverage."""
        kwargs = {"limit": limit, "order": "desc"}
        if before_id is not None:
            kwargs["before_id"] = before_id
        try:
            page = await asyncio.to_thread(
                client.get_messages, chat_id, **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed
            raise PlatformError("history-unreadable", type(exc).__name__) from exc
        if not isinstance(page, dict):
            raise PlatformError("history-unreadable", "not-an-object")
        if page.get("error"):
            # An error envelope is NOT an empty history - it is an unreadable one.
            raise PlatformError("history-unreadable", "provider-error")
        items = page.get("list")
        if items is None:
            items = page.get("messages")
        if not isinstance(items, list):
            # `{}`, a missing key, or a non-list value must never be read as
            # "nobody wrote to this fan".
            raise PlatformError("history-unreadable", "list-not-an-array")
        for item in items:
            if not isinstance(item, dict):
                raise PlatformError("history-unreadable", "list-has-non-object")
        return page, list(items)

    async def preflight_history(self, chat_id: str | int, *,
                                since: datetime | None = None,
                                source_ids: set[int] | None = None) -> list[dict]:
        """Read history, optionally proving coverage of a frozen turn's interval.

        With a source boundary, a short page never proves exhaustion: walk the
        provider's older-id cursor until a strictly older row or explicit
        ``hasMore=False`` proves coverage. Every frozen source must be observed.
        Equal timestamps stay in the interval. Unreadable, unordered, repeated,
        or capped history is a refusal, never permission to send. The caller
        classifies the returned rows and excludes only proven older activity.

        Omitting both boundary arguments preserves the existing single-page
        envelope validation for callers that do not require interval coverage.
        """
        scoped = since is not None or source_ids is not None
        if scoped:
            if not isinstance(since, datetime) or since.tzinfo is not None:
                raise PlatformError("history-unreadable", "invalid-source-boundary")
            # The shared parser truncates provider precision to milliseconds.
            # Floor the comparison boundary too: a same-millisecond row must
            # remain in scope rather than appearing spuriously older.
            since = since.replace(microsecond=(since.microsecond // 1000) * 1000)
            if not isinstance(source_ids, set) or not source_ids or any(
                isinstance(mid, bool) or not isinstance(mid, int) or mid <= 0
                for mid in source_ids
            ):
                raise PlatformError("history-unreadable", "invalid-source-ids")
            if self._history_limit <= 0:
                raise PlatformError("history-unreadable", "invalid-page-limit")

        client = await self.client()
        if not scoped:
            _, items = await self._history_page(client, chat_id, limit=self._history_limit)
            return items

        result: list[dict] = []
        seen: set[int] = set()
        cursor: int | None = None
        previous_time: datetime | None = None
        for _ in range(HISTORY_MAX_PAGES):
            remaining = HISTORY_MAX_ITEMS - len(result)
            if remaining <= 0:
                raise PlatformError("history-unreadable", "history-item-cap")
            page, items = await self._history_page(
                client, chat_id, limit=min(self._history_limit, remaining), before_id=cursor,
            )
            if len(items) > remaining:
                raise PlatformError("history-unreadable", "history-item-cap")
            crossed_boundary = False
            last_id: int | None = None
            for item in items:
                raw_id = item.get("id")
                canonical_id = normalise_provider_id(raw_id)
                if canonical_id is None or (
                    isinstance(raw_id, str) and _DECIMAL_ID.fullmatch(raw_id) is None
                ):
                    raise PlatformError("history-unreadable", "invalid-message-id")
                mid = int(canonical_id)
                if mid in seen or (cursor is not None and mid >= cursor):
                    raise PlatformError("history-unreadable", "history-cursor-no-progress")
                try:
                    instant = parse_canonical_timestamp(item.get("createdAt"), "createdAt")
                except ContractError as exc:
                    raise PlatformError("history-unreadable", "invalid-message-time") from exc
                if previous_time is not None and instant > previous_time:
                    raise PlatformError("history-unreadable", "history-out-of-order")
                previous_time = instant
                seen.add(mid)
                last_id = mid
                crossed_boundary = crossed_boundary or instant < since
            result.extend(items)

            # A page proving the older boundary needs no pagination flag. Until
            # then, only a real boolean can establish continuation/exhaustion.
            more = page.get("hasMore")
            if not crossed_boundary and not isinstance(more, bool):
                raise PlatformError("history-unreadable", "invalid-has-more")
            if crossed_boundary or more is False:
                if not source_ids.issubset(seen):
                    raise PlatformError("history-unreadable", "source-message-missing")
                return result
            if last_id is None:
                raise PlatformError("history-unreadable", "history-cursor-no-progress")
            cursor = last_id
        raise PlatformError("history-unreadable", "history-page-cap")

    # ── the one write ────────────────────────────────────────────────

    async def send_text(self, chat_id: str | int, text: str, *,
                        dispatch_token: str | None = None) -> TextSendResult:
        """One POST, one message. Raises PlatformRefused / PlatformUnknown."""
        client = await self.client()
        target = int(chat_id)
        body = escape_plain_text(text)
        try:
            # The bridge's own dispatch: allowed through the shared boundary, and
            # counted as in-flight so an ownership change waits for it to settle.
            # The attempt-bound token - not a purpose string - authorizes it.
            with guarded_send(self.account_id, target, purpose="twin_bridge",
                              dispatch_token=dispatch_token):
                payload = await asyncio.to_thread(client.send_text_once_body, target, body)
        except Exception as exc:  # noqa: BLE001 - classified below
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if isinstance(status, int) and status not in (408, 429) and 400 <= status < 500:
                # The provider answered and refused the POST: no message exists.
                raise PlatformRefused("provider-refused", f"status-{status}") from exc
            # Timeout, connection/proxy loss, 5xx, or anything else after the POST
            # boundary: the message may exist, so this is uncertainty, not failure.
            raise PlatformUnknown("dispatch-uncertain", type(exc).__name__) from exc
        return validate_created_message(payload, chat_id=target,
                                        creator_id=self.account_id)
