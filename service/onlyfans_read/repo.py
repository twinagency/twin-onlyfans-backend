"""The read repository boundary — the only SQL in this package.

Everything above this module (service.py, projections.py, contract.py) is pure
and takes rows as plain dataclasses, so the whole read path can be driven
offline with a fake repository and no database at all. That is not test
convenience for its own sake: it is what lets the refusal rules be asserted
directly instead of inferred from a live query, and it is why "storage is
unreachable" is a state a test can produce in two lines.

Two rules this module exists to keep:

1. **A read that cannot run is `Unavailable`, never an empty list.** Every
   method wraps its query; a missing table, an unreachable database or a
   permission error becomes `Unavailable("storage_unavailable")`. Returning []
   on a failure is how an outage reads as "this creator has no conversations",
   which is the single most misleading answer this API could give.

2. **Dirty timestamps do not take a read down.** `messages.created_at` is a
   DateTime column that has held an empty string in production, and SQLAlchemy
   raises while *materialising* the result set — one bad cell silenced an entire
   account (see the block comment above `models.created_at_text`). Every message
   read here selects `created_at_text()` and converts with `parse_ts`, dropping
   the one unusable row rather than losing the conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError

from db.engine import get_session
from db.models import (
    Account,
    AccountHealth,
    Chat,
    Fan,
    Message,
    OfConversationState,
    OfCreatorBinding,
    OfMessageDelivery,
    created_at_text,
    parse_ts,
)

from .errors import Unavailable

#: How many messages of one conversation the detail read returns. The dashboard
#: renders a thread, not an archive; the newest window is the one an operator
#: acts on.
DETAIL_MESSAGE_LIMIT = 100

#: How many purchased items the detail read exposes. Bounded for the same
#: reason the page size is.
DETAIL_OWNED_LIMIT = 50


# ── Rows ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BindingRow:
    binding_id: str
    tenant_id: str
    creator_id: str
    creator_name: str
    account_id: str
    handle: str
    channel: str
    created_at: datetime | None = None
    account_active_default: bool = False
    health_reason: str | None = None
    session_dead_at: datetime | None = None
    last_probe_at: datetime | None = None


@dataclass(frozen=True)
class ConversationRow:
    account_id: str
    fan_id: int
    last_message_id: int | None
    last_message_at: datetime | None
    last_message_preview: str | None
    unread_count: int
    is_priority: bool
    is_pinned: bool
    fan_username: str | None = None
    fan_display_name: str | None = None


@dataclass(frozen=True)
class MessageRow:
    message_id: int
    direction: str
    body: str
    created_at: datetime | None
    media_count: int
    price_cents: int
    is_paid: bool | None
    purchased_at: datetime | None
    is_unsent: bool
    temp_id: str | None
    automation_kind: str | None
    sent_by_employee_id: int | None


@dataclass(frozen=True)
class DeliveryRow:
    message_id: int
    outcome: str
    attempt_id: str | None
    evidence_ref: str | None
    detail: str | None
    recorded_at: datetime | None


@dataclass(frozen=True)
class StateRow:
    mode: str
    revision: int
    takeover_state: str
    unresolved_attempt_id: str | None
    requires_attention: bool
    updated_at: datetime | None


@dataclass(frozen=True)
class FactRow:
    label: str
    value: str


@dataclass(frozen=True)
class OwnedRow:
    media_ids: list[int]
    label: str
    price_cents: int
    purchased_at: datetime | None


# ── The boundary ────────────────────────────────────────────────────────────


class OnlyFansReadRepo(Protocol):
    """What the read service needs, and nothing more.

    Every method is keyword-only and returns rows — no ORM objects, no lazy
    relationships, no session. A fake implementation is a few lines, which is
    what the offline suite uses.
    """

    async def list_bindings(
        self, *, tenant_id: str | None, creator_id: str | None, account_id: str | None
    ) -> list[BindingRow]: ...

    async def get_binding(self, *, tenant_id: str | None, account_id: str) -> BindingRow | None: ...

    async def conversation_page(
        self, *, account_id: str, before: tuple[datetime | None, int] | None, limit: int
    ) -> list[ConversationRow]: ...

    async def conversation(self, *, account_id: str, fan_id: int) -> ConversationRow | None: ...

    async def conversation_state(self, *, account_id: str, fan_id: int) -> StateRow | None: ...

    async def messages(self, *, account_id: str, fan_id: int, limit: int) -> list[MessageRow]: ...

    async def deliveries(self, *, account_id: str, fan_id: int) -> dict[int, DeliveryRow]: ...

    async def fan_facts(self, *, account_id: str, fan_id: int) -> list[FactRow]: ...

    async def owned_media(self, *, account_id: str, fan_id: int, limit: int) -> list[OwnedRow]: ...

    async def account_activity_anchor(self, *, account_id: str) -> datetime | None: ...


# ── The SQL implementation ──────────────────────────────────────────────────


def _storage_failure(exc: Exception) -> Unavailable:
    """One mapping for every way storage can be missing.

    The message deliberately does not carry the driver's text: it can name a
    table, a column or a host, and this answer reaches a client. The detail is
    logged by the caller's logger instead.
    """
    return Unavailable(
        "storage_unavailable",
        "This relay cannot read its OnlyFans storage right now, so no account or "
        "conversation can be read from it.",
    )


class SqlOnlyFansReadRepo:
    """The real repository: async SQLAlchemy over this relay's own tables."""

    async def list_bindings(
        self, *, tenant_id: str | None, creator_id: str | None, account_id: str | None
    ) -> list[BindingRow]:
        try:
            async with get_session() as s:
                stmt = (
                    select(
                        OfCreatorBinding.id,
                        OfCreatorBinding.tenant_id,
                        OfCreatorBinding.creator_id,
                        OfCreatorBinding.creator_name,
                        OfCreatorBinding.account_id,
                        OfCreatorBinding.handle,
                        OfCreatorBinding.channel,
                        OfCreatorBinding.created_at,
                        Account.is_active_default,
                        AccountHealth.session_dead_at,
                        AccountHealth.session_dead_reason,
                        AccountHealth.last_probe_at,
                    )
                    .select_from(OfCreatorBinding)
                    .join(Account, Account.id == OfCreatorBinding.account_id)
                    .outerjoin(AccountHealth, AccountHealth.account_id == OfCreatorBinding.account_id)
                    .where(OfCreatorBinding.revoked_at.is_(None))
                    .order_by(OfCreatorBinding.creator_name, OfCreatorBinding.account_id)
                )
                if tenant_id is not None:
                    stmt = stmt.where(OfCreatorBinding.tenant_id == tenant_id)
                if creator_id is not None:
                    stmt = stmt.where(OfCreatorBinding.creator_id == creator_id)
                if account_id is not None:
                    stmt = stmt.where(OfCreatorBinding.account_id == account_id)
                rows = (await s.execute(stmt)).all()
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc
        return [
            BindingRow(
                binding_id=row[0],
                tenant_id=row[1],
                creator_id=row[2],
                creator_name=row[3] or "",
                account_id=row[4],
                handle=row[5] or "",
                channel=row[6] or "onlyfans",
                created_at=row[7],
                account_active_default=bool(row[8]),
                session_dead_at=row[9],
                health_reason=row[10],
                last_probe_at=row[11],
            )
            for row in rows
        ]

    async def get_binding(self, *, tenant_id: str | None, account_id: str) -> BindingRow | None:
        rows = await self.list_bindings(tenant_id=tenant_id, creator_id=None, account_id=account_id)
        return rows[0] if rows else None

    async def conversation_page(
        self, *, account_id: str, before: tuple[datetime | None, int] | None, limit: int
    ) -> list[ConversationRow]:
        try:
            async with get_session() as s:
                stmt = (
                    select(
                        Chat.account_id,
                        Chat.fan_id,
                        Chat.last_message_id,
                        Chat.last_message_at,
                        Chat.last_message_preview,
                        Chat.unread_count,
                        Chat.is_priority,
                        Chat.is_pinned,
                        Fan.of_username,
                        Fan.of_display_name,
                    )
                    .select_from(Chat)
                    .outerjoin(Fan, and_(Fan.account_id == Chat.account_id, Fan.fan_id == Chat.fan_id))
                    .where(Chat.account_id == account_id)
                    .where(Chat.hidden_locally.is_(False))
                    # Newest first, with the fan id as a stable tiebreak so a
                    # page boundary cannot repeat or skip a row when two chats
                    # share a timestamp.
                    .order_by(Chat.last_message_at.desc(), Chat.fan_id.desc())
                    .limit(limit)
                )
                if before is not None:
                    anchor_at, anchor_fan = before
                    if anchor_at is None:
                        # Every remaining row has no timestamp; only fan id can
                        # order them.
                        stmt = stmt.where(
                            and_(Chat.last_message_at.is_(None), Chat.fan_id < anchor_fan)
                        )
                    else:
                        # Keyset pagination: strictly after the last row of the
                        # previous page, in the same total order.
                        stmt = stmt.where(
                            or_(
                                Chat.last_message_at < anchor_at,
                                and_(
                                    Chat.last_message_at == anchor_at,
                                    Chat.fan_id < anchor_fan,
                                ),
                                Chat.last_message_at.is_(None),
                            )
                        )
                rows = (await s.execute(stmt)).all()
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc
        return [
            ConversationRow(
                account_id=row[0],
                fan_id=int(row[1]),
                last_message_id=row[2],
                last_message_at=row[3],
                last_message_preview=row[4],
                unread_count=int(row[5] or 0),
                is_priority=bool(row[6]),
                is_pinned=bool(row[7]),
                fan_username=row[8],
                fan_display_name=row[9],
            )
            for row in rows
        ]

    async def conversation(self, *, account_id: str, fan_id: int) -> ConversationRow | None:
        try:
            async with get_session() as s:
                row = (
                    await s.execute(
                        select(
                            Chat.account_id,
                            Chat.fan_id,
                            Chat.last_message_id,
                            Chat.last_message_at,
                            Chat.last_message_preview,
                            Chat.unread_count,
                            Chat.is_priority,
                            Chat.is_pinned,
                            Fan.of_username,
                            Fan.of_display_name,
                        )
                        .select_from(Chat)
                        .outerjoin(
                            Fan, and_(Fan.account_id == Chat.account_id, Fan.fan_id == Chat.fan_id)
                        )
                        .where(Chat.account_id == account_id, Chat.fan_id == fan_id)
                    )
                ).first()
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc
        if row is None:
            return None
        return ConversationRow(
            account_id=row[0],
            fan_id=int(row[1]),
            last_message_id=row[2],
            last_message_at=row[3],
            last_message_preview=row[4],
            unread_count=int(row[5] or 0),
            is_priority=bool(row[6]),
            is_pinned=bool(row[7]),
            fan_username=row[8],
            fan_display_name=row[9],
        )

    async def conversation_state(self, *, account_id: str, fan_id: int) -> StateRow | None:
        try:
            async with get_session() as s:
                row = (
                    await s.execute(
                        select(
                            OfConversationState.mode,
                            OfConversationState.revision,
                            OfConversationState.takeover_state,
                            OfConversationState.unresolved_attempt_id,
                            OfConversationState.requires_attention,
                            OfConversationState.updated_at,
                        ).where(
                            OfConversationState.account_id == account_id,
                            OfConversationState.fan_id == fan_id,
                        )
                    )
                ).first()
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc
        if row is None:
            return None
        return StateRow(
            mode=row[0] or "automated",
            revision=int(row[1] or 0),
            takeover_state=row[2] or "none",
            unresolved_attempt_id=row[3],
            requires_attention=bool(row[4]),
            updated_at=row[5],
        )

    async def messages(self, *, account_id: str, fan_id: int, limit: int) -> list[MessageRow]:
        try:
            async with get_session() as s:
                rows = (
                    await s.execute(
                        select(
                            Message.message_id,
                            Message.direction,
                            Message.body,
                            created_at_text(),
                            Message.media_count,
                            Message.price_cents,
                            Message.is_paid,
                            Message.purchased_at,
                            Message.is_unsent,
                            Message.temp_id,
                            Message.automation_kind,
                            Message.sent_by_employee_id,
                        )
                        .where(Message.account_id == account_id, Message.fan_id == fan_id)
                        .order_by(Message.message_id.desc())
                        .limit(limit)
                    )
                ).all()
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc
        out = [
            MessageRow(
                message_id=int(row[0]),
                direction=row[1] or "in",
                body=row[2] or "",
                created_at=parse_ts(row[3]),
                media_count=int(row[4] or 0),
                price_cents=int(row[5] or 0),
                is_paid=row[6],
                purchased_at=row[7],
                is_unsent=bool(row[8]),
                temp_id=row[9],
                automation_kind=row[10],
                sent_by_employee_id=row[11],
            )
            for row in rows
        ]
        # The query walks backwards from the newest message; the thread renders
        # forwards.
        out.reverse()
        return out

    async def deliveries(self, *, account_id: str, fan_id: int) -> dict[int, DeliveryRow]:
        try:
            async with get_session() as s:
                rows = (
                    await s.execute(
                        select(
                            OfMessageDelivery.message_id,
                            OfMessageDelivery.outcome,
                            OfMessageDelivery.attempt_id,
                            OfMessageDelivery.evidence_ref,
                            OfMessageDelivery.detail,
                            OfMessageDelivery.recorded_at,
                        ).where(
                            OfMessageDelivery.account_id == account_id,
                            OfMessageDelivery.fan_id == fan_id,
                        )
                    )
                ).all()
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc
        return {
            int(row[0]): DeliveryRow(
                message_id=int(row[0]),
                outcome=row[1] or "none",
                attempt_id=row[2],
                evidence_ref=row[3],
                detail=row[4],
                recorded_at=row[5],
            )
            for row in rows
        }

    async def fan_facts(self, *, account_id: str, fan_id: int) -> list[FactRow]:
        """Creator-facing facts about one fan, as label/value pairs.

        Only fields the relay actually holds are exposed, and only when they are
        set. An absent fact is absent rather than "unknown": the dashboard
        renders the list it is given, and a row saying "unknown" would read as a
        corrected fact.
        """
        try:
            async with get_session() as s:
                row = (
                    await s.execute(
                        select(
                            Fan.real_name,
                            Fan.his_age,
                            Fan.home_country,
                            Fan.home_city,
                            Fan.hobbies,
                            Fan.timezone,
                            Fan.self_description,
                        ).where(Fan.account_id == account_id, Fan.fan_id == fan_id)
                    )
                ).first()
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc
        if row is None:
            return []
        labels = ("Real name", "Age", "Country", "City", "Hobbies", "Timezone", "Self description")
        out: list[FactRow] = []
        for label, value in zip(labels, row):
            text = (value or "").strip() if isinstance(value, str) else value
            if text:
                out.append(FactRow(label=label, value=str(text)))
        return out

    async def owned_media(self, *, account_id: str, fan_id: int, limit: int) -> list[OwnedRow]:
        """What this fan has actually PAID for.

        A paid message is the durable proof available to this read: the relay
        flips `messages.is_paid` one-way from the purchase feed
        (`models.is_paid_ratchet`) and never un-flips it. Offered-but-unbought
        PPV rows stay NULL, so they are excluded — which is the product rule
        `ownership.py` states at length: dedup bought, never sent.
        """
        try:
            async with get_session() as s:
                rows = (
                    await s.execute(
                        select(Message.message_id, Message.body, Message.price_cents, Message.purchased_at, Message.media_ids)
                        .where(
                            Message.account_id == account_id,
                            Message.fan_id == fan_id,
                            Message.is_paid.is_(True),
                        )
                        .order_by(Message.message_id.desc())
                        .limit(limit)
                    )
                ).all()
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc
        out: list[OwnedRow] = []
        for message_id, body, price_cents, purchased_at, media_ids in rows:
            out.append(
                OwnedRow(
                    media_ids=_media_ids(media_ids, fallback=int(message_id)),
                    label=_label(body),
                    price_cents=int(price_cents or 0),
                    purchased_at=purchased_at,
                )
            )
        return out

    async def account_activity_anchor(self, *, account_id: str) -> datetime | None:
        """The newest observed activity for one account — the freshness anchor.

        It reads `chats.last_message_at`, not `max(messages.created_at)`, on
        purpose: the chats row is maintained by the inbox poll and is not the
        column that has held an empty string in production, so the anchor
        cannot be poisoned by one bad message row. `None` means this relay has
        no observed activity for the account, which the projection reports as
        "no anchor" rather than substituting the current time — a read that
        stamped itself with `now` would always look fresh.
        """
        try:
            async with get_session() as s:
                row = (
                    await s.execute(
                        select(Chat.last_message_at)
                        .where(Chat.account_id == account_id)
                        .order_by(Chat.last_message_at.desc())
                        .limit(1)
                    )
                ).first()
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc
        return row[0] if row else None


def _media_ids(raw: str | None, *, fallback: int) -> list[int]:
    """Parse the JSON media id list, tolerating what a scrape may have written."""
    import json

    try:
        parsed = json.loads(raw or "[]")
    except Exception:  # noqa: BLE001 — a malformed cell drops to the fallback
        parsed = []
    if not isinstance(parsed, list):
        return [fallback]
    ids = [int(item) for item in parsed if isinstance(item, int) and not isinstance(item, bool)]
    return ids or [fallback]


def _label(body: str | None) -> str:
    text = (body or "").strip().replace("\n", " ")
    if not text:
        return "Purchased content"
    return text[:60]
