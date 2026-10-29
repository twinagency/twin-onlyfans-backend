"""The read repository boundary, including bounded native workspace projections.

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

from dataclasses import dataclass, replace
import json
import os
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import and_, or_, select, func, case, tuple_, cast, String, text as sql_text
from sqlalchemy.exc import SQLAlchemyError

from db.engine import get_session
from db.models import (
    Account,
    AccountHealth,
    Chat,
    Fan,
    Message,
    VaultItem,
    VaultSend,
    OfConversationState,
    OfCreatorBinding,
    OfMessageDelivery,
    created_at_text,
    parse_ts,
)

from db.onlyfans_models import OfRuntime, OfIngress
from db.onlyfans_connection_models import OfProviderConnection

from .contract import UNRESOLVED_OUTCOMES, iso
from .errors import Unavailable

MAX_ACCOUNT_READ = 500

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
    connection_status: str | None = None
    connection_verified_at: datetime | None = None
    connection_expires_at: datetime | None = None


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
    fan_avatar_url: str | None = None


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
    media: tuple[tuple[int, str], ...] = ()
    message_media_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class DeliveryRow:
    message_id: int
    outcome: str
    attempt_id: str | None
    evidence_ref: str | None
    detail: str | None
    recorded_at: datetime | None
    origin: str | None = None


@dataclass(frozen=True)
class StateRow:
    mode: str
    revision: int
    takeover_state: str
    unresolved_attempt_id: str | None
    requires_attention: bool
    updated_at: datetime | None
    attempt_outcome: str | None = None
    history_held: bool = False


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
        self, *, account_id: str, before: tuple[datetime | None, int] | None, limit: int, ai_only: bool = False, query: str = "", filter: str | None = None, search: str | None = None
    ) -> list[ConversationRow]: ...

    async def conversation(self, *, account_id: str, fan_id: int) -> ConversationRow | None: ...

    async def conversation_state(self, *, account_id: str, fan_id: int) -> StateRow | None: ...

    async def messages(self, *, account_id: str, fan_id: int, limit: int, before_message_id: int | None = None) -> list[MessageRow]: ...

    async def deliveries(self, *, account_id: str, fan_id: int, before_message_id: int | None = None) -> dict[int, DeliveryRow]: ...

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


from .agency_scope import current_scope, lock_read_scope


class SqlOnlyFansReadRepo:
    """The real repository: async SQLAlchemy over this relay's own tables."""

    async def workspace(self, *, tenant_id, account_id, section, cursor=None, limit=25):
        from .workspace import read_workspace
        try:
            async with get_session() as s:
                return await read_workspace(s, tenant_id=tenant_id, account_id=account_id,
                    section=section, cursor=cursor, limit=limit)
        except (SQLAlchemyError, ValueError, TypeError, RecursionError) as exc:
            raise _storage_failure(exc) from exc

    async def fan_spend(self, *, account_id, fan_id):
        from .spend import fan_spend
        async with get_session() as session:
            await lock_read_scope(session, account_id)
            return await fan_spend(session, account_id, fan_id)

    async def account_health(self, *, account_id):
        from .health import account_health
        async with get_session() as session:
            await lock_read_scope(session, account_id)
            return await account_health(session, account_id)

    async def ledger(self, *, tenant_id, account_id):
        from onlyfans_runtime.launch_ledger.dashboard import read_ledger
        try:
            async with get_session() as s:
                await lock_read_scope(s, account_id)
                return await s.run_sync(lambda sync: read_ledger(sync, tenant_id, account_id))
        except (SQLAlchemyError, ValueError, TypeError, RecursionError) as exc:
            raise _storage_failure(exc) from exc

    async def operations(self, *, tenant_id: str | None, account_id: str) -> dict | None:
        """Account-scoped counters only; a refusal never becomes healthy/zero."""
        from db.onlyfans_models import OfIngress, OfWork
        from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent, OfNativePlan, OfCommerceEvidence
        from db.models import AccountAiConfig, GrokCall, GrokDailyCost
        from .contract import iso
        try:
            async with get_session() as s:
                await lock_read_scope(s, account_id)
                binding = select(OfCreatorBinding.id).where(
                    OfCreatorBinding.account_id == account_id,
                    OfCreatorBinding.channel == "onlyfans",
                    OfCreatorBinding.revoked_at.is_(None),
                )
                if tenant_id is not None:
                    binding = binding.where(OfCreatorBinding.tenant_id == tenant_id)
                # Prevent revocation from committing between authorization and
                # these private reads; no model/provider call crosses this lock.
                if await s.scalar(binding.with_for_update(read=True)) is None:
                    return None
                now = await s.scalar(select(func.clock_timestamp()))
                naive_now = now.astimezone(timezone.utc).replace(tzinfo=None)
                runtime = await s.get(OfRuntime, account_id)
                connection = await s.get(OfProviderConnection, account_id)
                policy = await s.get(OfDeliveryPolicy, account_id)
                cfg = await s.get(AccountAiConfig, account_id)
                cap = cfg.daily_cost_cap_cents if cfg is not None else None
                if cap is not None and (type(cap) is not int or not 0 <= cap <= 1000000):
                    raise Unavailable("storage_unavailable")
                day = naive_now.strftime("%Y-%m-%d")
                totals = (await s.execute(select(
                    func.coalesce(func.sum(GrokDailyCost.cost_cents), 0),
                    func.coalesce(func.sum(GrokDailyCost.call_count), 0), func.count(),
                    func.min(GrokDailyCost.cost_cents), func.max(GrokDailyCost.cost_cents),
                    func.min(GrokDailyCost.call_count)).where(
                    GrokDailyCost.account_id == account_id, GrokDailyCost.day == day))).one()
                spent, calls, providers, minimum, maximum, min_calls = totals
                if (minimum is not None and minimum < 0) or (min_calls is not None and min_calls < 0):
                    raise Unavailable("storage_unavailable")
                unresolved_calls = await s.scalar(select(func.count()).select_from(GrokCall).where(
                    GrokCall.account_id == account_id, GrokCall.status.in_(("pending", "unknown"))))
                model_costs = {"day": day, "unit": "usd_millicents", "spentMillicents": spent,
                    "capPerProviderMillicents": None if cap is None else cap*100,
                    "calls": calls, "unresolvedCalls": unresolved_calls, "providers": providers}

                async def counts(model, status, values):
                    rows = (await s.execute(select(status, func.count()).where(
                        model.account_id == account_id, status.in_(values)).group_by(status))).all()
                    return {v: dict(rows).get(v, 0) for v in values}

                ingress = await counts(OfIngress, OfIngress.status, ("pending", "quarantined"))
                work = await counts(OfWork, OfWork.status, ("waiting", "claimed", "blocked"))
                delivery = await counts(OfTextIntent, OfTextIntent.status, ("queued", "dispatching", "unknown"))
                # Holds describe each conversation's current canonical input.
                # Historical unknown/refused plans remain durable evidence, but
                # must not hold a newer input after it has superseded them.
                plan_statuses = ("generating", "ready", "unknown", "refused", "stale")
                plan_status = case(
                    (and_(OfNativePlan.status.in_(("ready", "queued")),
                        or_(OfNativePlan.model_call_id.is_(None), OfNativePlan.cost_settled_at.is_(None))), "unknown"),
                    (and_(OfNativePlan.status == "generating", OfNativePlan.expires_at <= naive_now), "unknown"),
                    (and_(OfNativePlan.status == "ready", OfNativePlan.expires_at <= naive_now), "stale"),
                    else_=OfNativePlan.status)
                plan_counts = dict((await s.execute(select(plan_status, func.count()).join(
                    OfConversationState, and_(OfConversationState.account_id == OfNativePlan.account_id,
                        OfConversationState.fan_id == OfNativePlan.fan_id,
                        OfConversationState.input_revision == OfNativePlan.input_revision)
                ).where(OfNativePlan.account_id == account_id, OfNativePlan.status.in_((*plan_statuses, "queued")))
                    .group_by(plan_status))).all())
                planning = {status: plan_counts.get(status, 0) for status in plan_statuses}
                awaiting_projection = await s.scalar(select(func.count()).select_from(OfTextIntent).where(
                    OfTextIntent.account_id == account_id, OfTextIntent.status == "accepted", OfTextIntent.projected_at.is_(None)))
                retained_unknown = await s.scalar(select(func.count()).select_from(OfMessageDelivery).where(
                    OfMessageDelivery.account_id == account_id, OfMessageDelivery.outcome.in_(UNRESOLVED_OUTCOMES)))
                oldest = await s.scalar(select(func.min(OfWork.due_at)).where(
                    OfWork.account_id == account_id, OfWork.status == "waiting"))
                age = max(0, int((naive_now - oldest).total_seconds())) if oldest else None
                reasons = []
                from onlyfans_runtime.commerce import held
                if await s.run_sync(lambda sync: held(sync, account_id)):
                    reasons.append("commerce_evidence_conflict")
                if cfg is not None:
                    from onlyfans_runtime.creator_facts import effective
                    source=await s.run_sync(lambda sync:effective(sync,cfg,now=naive_now)[1])
                    if source is not None and source["state"]!="current":reasons.append("creator_facts_not_current")
                if unresolved_calls: reasons.append("model_cost_unresolved")
                if cap is not None and maximum is not None and maximum >= cap*100:
                    reasons.append("model_budget_exhausted")
                if runtime is None: reasons.append("runtime_not_registered")
                else:
                    if not runtime.enabled: reasons.append("runtime_disabled")
                    if runtime.recovery_needed: reasons.append("history_recovery_required")
                    if runtime.pending_events != ingress["pending"]: reasons.append("intake_count_mismatch")
                    if runtime.last_sync_at is None or (naive_now-runtime.last_sync_at).total_seconds() > 1800:
                        reasons.append("history_not_current")
                    elif runtime.last_sync_at > naive_now:
                        reasons.append("history_sync_time_invalid")
                if ingress["quarantined"]: reasons.append("history_quarantined")
                if delivery["unknown"] or retained_unknown: reasons.append("delivery_unresolved")
                if delivery["dispatching"]: reasons.append("delivery_in_progress")
                if awaiting_projection: reasons.append("receipt_projection_pending")
                if planning["unknown"]: reasons.append("generation_unresolved")
                if planning["stale"] or planning["refused"]: reasons.append("generation_held")
                if policy is None or not policy.enabled: reasons.append("delivery_disabled")
                if policy is not None and policy.used_units >= policy.limit_units: reasons.append("cap_exhausted")
                if age is not None and age > 120: reasons.append("waiting_queue_older_than_two_minutes")
                return {"schemaVersion": "of-operations.v3", "accountId": account_id, "asOf": iso(now),
                        "providerConnectionVerified": bool(connection and runtime and connection.binding_id == runtime.binding_id and connection.status == "verified" and connection.verified_at is not None and connection.expires_at > naive_now), "reasons": reasons,
                        "modelCosts": model_costs,
                        "intake": ingress, "planning": planning, "work": {**work, "oldestWaitingAgeSeconds": age},
                        "delivery": {**delivery, "acceptedAwaitingProjection": awaiting_projection,
                                     "retainedUnresolvedReceipts": retained_unknown},
                        "runtime": {"registered": runtime is not None, "enabled": bool(runtime and runtime.enabled),
                                    "recoveryRequired": bool(runtime is None or runtime.recovery_needed),
                                    "lastCompleteSyncAt": iso(runtime.last_sync_at) if runtime else None},
                        "policy": None if policy is None else {"revision": policy.revision, "enabled": policy.enabled,
                                    "usedUnits": policy.used_units, "limitUnits": policy.limit_units,
                                    "nextDispatchAt": iso(policy.next_dispatch_at)}}
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc

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
                        OfProviderConnection.status,
                        OfProviderConnection.verified_at,
                        OfProviderConnection.expires_at,
                    )
                    .select_from(OfCreatorBinding)
                    .join(Account, Account.id == OfCreatorBinding.account_id)
                    .outerjoin(AccountHealth, AccountHealth.account_id == OfCreatorBinding.account_id)
                    .outerjoin(OfProviderConnection, and_(OfProviderConnection.account_id == OfCreatorBinding.account_id,OfProviderConnection.binding_id == OfCreatorBinding.id))
                    .where(OfCreatorBinding.revoked_at.is_(None))
                    .order_by(OfCreatorBinding.creator_name, OfCreatorBinding.account_id)
                )
                scope = current_scope.get()
                if scope is not None:
                    stmt = stmt.where(OfCreatorBinding.creator_id.in_(scope.creator_ids))
                if tenant_id is not None:
                    stmt = stmt.where(OfCreatorBinding.tenant_id == tenant_id)
                if creator_id is not None:
                    stmt = stmt.where(OfCreatorBinding.creator_id == creator_id)
                if account_id is not None:
                    stmt = stmt.where(OfCreatorBinding.account_id == account_id)
                rows = (await s.execute(stmt.limit(MAX_ACCOUNT_READ + 1))).all()
                if len(rows) > MAX_ACCOUNT_READ:
                    raise Unavailable("scope_too_large", "Narrow this read to one creator or account.")
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
                connection_status=row[12],connection_verified_at=row[13],connection_expires_at=row[14],
            )
            for row in rows
        ]

    async def get_binding(self, *, tenant_id: str | None, account_id: str) -> BindingRow | None:
        rows = await self.list_bindings(tenant_id=tenant_id, creator_id=None, account_id=account_id)
        return rows[0] if rows else None

    async def fan_financials(self, *, account_id: str, fan_id: int) -> dict:
        """Canonical transaction rows, counted once per receipt, never per asset.

        Amounts remain separated by recorded currency and payment status. These
        are recorded totals, not a claim of complete lifetime provider coverage.
        """
        from db.models import Transaction
        from db.onlyfans_ledger_models import OfLedgerScan
        from onlyfans_runtime.ledger import coverage_proven
        from onlyfans_runtime.commerce import held
        try:
            async with get_session() as s:
                await lock_read_scope(s, account_id)
                rows = (await s.execute(select(Transaction.currency, Transaction.status,
                    func.count(), func.sum(Transaction.amount_cents), func.sum(Transaction.net_cents),
                    func.count(Transaction.net_cents), func.max(Transaction.occurred_at)).where(
                    Transaction.account_id == account_id, Transaction.fan_id == fan_id,
                    Transaction.source == "ledger").group_by(Transaction.currency, Transaction.status)
                    .order_by(Transaction.currency, Transaction.status))).all()
                scan = await s.get(OfLedgerScan, account_id)
                runtime = await s.get(OfRuntime, account_id)
                current = bool(scan and runtime and scan.binding_id == runtime.binding_id)
                coverage = bool(current and coverage_proven(scan))
                commerce_held = await s.run_sync(lambda sync: held(sync, account_id, fan_id))
                return {"version": "of-fan-financials.v1", "totals": [
                    {"currency": row[0], "status": row[1], "transactions": row[2], "grossCents": row[3],
                     "netCents": row[4] if row[5] == row[2] else None, "lastTransactionAt": iso(row[6])}
                    for row in rows], "coverageComplete": coverage,
                    "coverageAt": iso(scan.last_complete_at) if current else None,
                    "commerceHeld": commerce_held}
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc

    async def conversation_page(
        self, *, account_id: str, before: tuple[datetime | None, int] | None, limit: int, ai_only: bool = False, query: str = "", filter: str | None = None, search: str | None = None
    ) -> list[ConversationRow]:
        if search is not None:
            from .search import normalize_search
            query = normalize_search(search)
        try:
            async with get_session() as s:
                await lock_read_scope(s, account_id)
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
                        Fan.avatar_url,
                    )
                    .select_from(Chat)
                    .outerjoin(Fan, and_(Fan.account_id == Chat.account_id, Fan.fan_id == Chat.fan_id))
                    .where(Chat.account_id == account_id)
                    .where(Chat.hidden_locally.is_(False))
                    # Newest first, with the fan id as a stable tiebreak so a
                    # page boundary cannot repeat or skip a row when two chats
                    # share a timestamp.
                    .order_by(Chat.last_message_at.desc().nullslast(), Chat.fan_id.desc())
                    .limit(limit)
                )
                if query:
                    stmt = stmt.where(or_(Fan.of_username.icontains(query, autoescape=True),
                        Fan.of_display_name.icontains(query, autoescape=True),
                        cast(Chat.fan_id, String).contains(query, autoescape=True)))
                if filter == "unread":
                    stmt = stmt.where(Chat.unread_count > 0)
                elif filter == "attention":
                    stmt = stmt.where(select(OfConversationState.fan_id).where(
                        OfConversationState.account_id == Chat.account_id, OfConversationState.fan_id == Chat.fan_id,
                        OfConversationState.requires_attention.is_(True)).exists())
                if ai_only:
                    # Filter before the keyset limit, including enabled fans on later pages.
                    # Both identity columns are required: fan ids can repeat across creators.
                    stmt = stmt.where(select(OfConversationState.fan_id).where(
                        OfConversationState.account_id == Chat.account_id,
                        OfConversationState.fan_id == Chat.fan_id,
                        OfConversationState.mode == "automated",
                        OfConversationState.takeover_state == "none",
                    ).exists())
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
                fan_avatar_url=row[10],
            )
            for row in rows
        ]

    async def conversation(self, *, account_id: str, fan_id: int) -> ConversationRow | None:
        try:
            async with get_session() as s:
                await lock_read_scope(s, account_id)
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
                        Fan.avatar_url,
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
            fan_avatar_url=row[10],
        )

    async def conversation_metadata(self, *, account_id: str, rows):
        """One scoped session and three bounded set reads for a list page."""
        fans = list(dict.fromkeys(row.fan_id for row in rows))
        if not fans:
            return {}, {}
        if len(fans) > 100:
            raise ValueError("conversation metadata page exceeds limit")
        try:
            async with get_session() as session:
                await lock_read_scope(session, account_id)
                states = (await session.execute(select(
                    OfConversationState.fan_id, OfConversationState.mode,
                    OfConversationState.revision, OfConversationState.takeover_state,
                    OfConversationState.unresolved_attempt_id,
                    OfConversationState.requires_attention, OfConversationState.updated_at
                ).where(OfConversationState.account_id == account_id,
                        OfConversationState.fan_id.in_(fans)))).all()
                held = select(
                    OfMessageDelivery.fan_id, OfMessageDelivery.attempt_id,
                    OfMessageDelivery.message_id, OfMessageDelivery.recorded_at,
                    func.row_number().over(partition_by=OfMessageDelivery.fan_id,
                        order_by=(OfMessageDelivery.recorded_at.desc().nulls_last(),
                                  OfMessageDelivery.message_id.desc())).label("position")
                ).where(OfMessageDelivery.account_id == account_id,
                        OfMessageDelivery.fan_id.in_(fans),
                        OfMessageDelivery.outcome.in_(UNRESOLVED_OUTCOMES)).subquery()
                unresolved = {row[0]: row for row in (await session.execute(
                    select(held).where(held.c.position == 1))).all()}
                latest = [(row.fan_id, int(row.last_message_id)) for row in rows
                          if row.last_message_id is not None]
                outcomes = dict((await session.execute(select(
                    OfMessageDelivery.fan_id, OfMessageDelivery.outcome).where(
                    OfMessageDelivery.account_id == account_id,
                    tuple_(OfMessageDelivery.fan_id, OfMessageDelivery.message_id).in_(latest)
                ))).all()) if latest else {}
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc
        by_fan = {row[0]: row for row in states}
        result = {}
        for fan in fans:
            row = by_fan.get(fan)
            pending = unresolved.get(fan) if row is None or not row[4] else None
            if row is None and pending is None:
                result[fan] = None
                continue
            result[fan] = StateRow(
                mode=(row[1] or "paused") if row else "paused",
                revision=int(row[2] or 0) if row else 0,
                takeover_state=(row[3] or "none") if row else "none",
                unresolved_attempt_id=(pending[1] or f"message:{pending[2]}") if pending else row[4],
                requires_attention=bool(pending or (row and row[5])),
                updated_at=pending[3] if pending else row[6])
        async with get_session() as session:
            await lock_read_scope(session, account_id)
            from .attempts import outcome
            held_fans=set((await session.scalars(select(OfIngress.fan_id).where(OfIngress.account_id==account_id,
                OfIngress.fan_id.in_(fans),OfIngress.status=='quarantined').distinct())).all())
            for fan, state in result.items():
                if state:result[fan]=state=replace(state,history_held=fan in held_fans)
                if state and state.unresolved_attempt_id:
                    value = await session.run_sync(lambda sync: outcome(sync, account_id, fan, state.unresolved_attempt_id))
                    result[fan] = replace(state, attempt_outcome=value)
        return result, outcomes

    async def conversation_state(self, *, account_id: str, fan_id: int) -> StateRow | None:
        unresolved = None
        try:
            async with get_session() as s:
                await lock_read_scope(s, account_id)
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
                # The visible thread is bounded; an older unsettled receipt
                # still holds the conversation even if its control pointer was
                # never written. Derive an attention state without repairing
                # storage or replacing an explicit authority pointer.
                if row is None or not row[3]:
                    unresolved = (
                        await s.execute(
                            select(
                                OfMessageDelivery.attempt_id,
                                OfMessageDelivery.message_id,
                                OfMessageDelivery.recorded_at,
                            ).where(
                                OfMessageDelivery.account_id == account_id,
                                OfMessageDelivery.fan_id == fan_id,
                                OfMessageDelivery.outcome.in_(UNRESOLVED_OUTCOMES),
                            ).order_by(
                                OfMessageDelivery.recorded_at.desc().nulls_last(),
                                OfMessageDelivery.message_id.desc(),
                            ).limit(1)
                        )
                    ).first()
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc
        if row is None and unresolved is None:
            return None
        reference = (unresolved[0] or f"message:{unresolved[1]}") if unresolved else row[3]
        async with get_session() as session:
            await lock_read_scope(session, account_id)
            from .attempts import outcome
            attempt_outcome = await session.run_sync(lambda sync: outcome(sync, account_id, fan_id, reference))
            history_held = await session.scalar(select(OfIngress.event_key).where(OfIngress.account_id==account_id,OfIngress.fan_id==fan_id,OfIngress.status=='quarantined').limit(1)) is not None
        return StateRow(
            history_held=history_held,
            attempt_outcome=attempt_outcome,
            mode=(row[0] or "paused") if row else "paused",
            revision=int(row[1] or 0) if row else 0,
            takeover_state=(row[2] or "none") if row else "none",
            unresolved_attempt_id=(
                unresolved[0] or f"message:{unresolved[1]}"
                if unresolved is not None else row[3]
            ),
            requires_attention=bool(unresolved is not None or (row and row[4])),
            updated_at=unresolved[2] if unresolved is not None else row[5],
        )

    async def messages(self, *, account_id: str, fan_id: int, limit: int, before_message_id: int | None = None) -> list[MessageRow]:
        try:
            async with get_session() as s:
                await lock_read_scope(s, account_id)
                rows = (await s.execute(select(
                    Message.message_id, Message.direction, Message.body, created_at_text(),
                    Message.media_count, Message.price_cents, Message.is_paid, Message.purchased_at,
                    Message.is_unsent, Message.temp_id, Message.automation_kind,
                    Message.sent_by_employee_id, Message.media_ids, Message.raw_json,
                ).where(Message.account_id == account_id, Message.fan_id == fan_id,
                    Message.message_id < before_message_id if before_message_id is not None else True)
                    .order_by(Message.message_id.desc()).limit(limit))).all()
                refs = {int(row[0]): _message_media_ids(row[12]) for row in rows}
                raw_media = {int(row[0]): _message_media_kinds(row[13], refs[int(row[0])]) for row in rows}
                missing = [int(row[0]) for row in rows if not refs[int(row[0])]
                           and (int(row[4] or 0) > 0 or int(row[5] or 0) > 0)]
                if missing:
                    # Older pack sends saved file identities in this existing
                    # send ledger only. One bounded, account-and-fan-scoped read.
                    saved = (await s.execute(select(VaultSend.message_id, VaultSend.media_id)
                        .where(VaultSend.account_id == account_id, VaultSend.fan_id == fan_id,
                               VaultSend.message_id.in_(missing))
                        .order_by(VaultSend.message_id, VaultSend.id).limit(limit * 100))).all()
                    for message_id, media_id in saved:
                        group = refs[int(message_id)]
                        if len(group) < 100 and int(media_id) not in group:
                            group.append(int(media_id))
                ids = {mid for values in refs.values() for mid in values}
                kinds = dict((await s.execute(select(VaultItem.media_id, VaultItem.kind)
                    .where(VaultItem.account_id == account_id, VaultItem.media_id.in_(ids))
                    .limit(limit * 100))).all()) if ids else {}
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc
        out = [MessageRow(
            message_id=int(row[0]), direction=row[1] or "in", body=row[2] or "",
            created_at=parse_ts(row[3]), media_count=max(int(row[4] or 0), len(refs[int(row[0])])),
            price_cents=int(row[5] or 0), is_paid=row[6], purchased_at=row[7],
            is_unsent=bool(row[8]), temp_id=row[9], automation_kind=row[10],
            sent_by_employee_id=row[11],
            media=tuple((mid, raw_media[int(row[0])].get(mid) or ("photo" if kinds.get(mid) in {"image", "photo"}
                         else kinds.get(mid) if kinds.get(mid) in {"video", "audio"}
                         else "unknown")) for mid in refs[int(row[0])]),
            message_media_ids=tuple(raw_media[int(row[0])]),
        ) for row in rows]
        out.reverse()
        return out

    async def deliveries(self, *, account_id: str, fan_id: int, before_message_id: int | None = None) -> dict[int, DeliveryRow]:
        try:
            async with get_session() as s:
                await lock_read_scope(s, account_id)
                rows = (
                    await s.execute(
                        select(
                            OfMessageDelivery.message_id,
                            OfMessageDelivery.outcome,
                            OfMessageDelivery.attempt_id,
                            OfMessageDelivery.evidence_ref,
                            OfMessageDelivery.detail,
                            OfMessageDelivery.recorded_at,
                            OfMessageDelivery.origin,
                        ).where(
                            OfMessageDelivery.account_id == account_id,
                            OfMessageDelivery.fan_id == fan_id,
                            OfMessageDelivery.message_id.in_(select(Message.message_id).where(Message.account_id == account_id, Message.fan_id == fan_id, Message.message_id < before_message_id if before_message_id is not None else True).order_by(Message.message_id.desc()).limit(DETAIL_MESSAGE_LIMIT)),
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
                origin=row[6],
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
        learning = None
        try:
            async with get_session() as s:
                await lock_read_scope(s, account_id)
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
                if os.environ.get("ONLYFANS_PROFILE_LEARNING_ENABLED") == "1":
                    from onlyfans_runtime.profile_learning.storage import profile_view, coverage
                    from db.onlyfans_models import OfRuntime
                    from db.models import OfCreatorBinding
                    runtime = await s.get(OfRuntime,account_id)
                    binding = await s.get(OfCreatorBinding,runtime.binding_id) if runtime else None
                    if binding is not None and binding.revoked_at is None:
                        learning = await s.run_sync(lambda sync: profile_view(sync,binding.tenant_id,account_id,binding.id,fan_id))
                        learning['coverage'] = await s.run_sync(lambda sync: coverage(sync,binding.tenant_id,account_id,binding.id))

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
        if learning is not None:
            # Undated legacy ages/locations must not contradict the dated profile.
            protected=learning['manualCorrections']
            label_fields={'Age':'his_age','Country':'home_country','City':'home_city'}
            out=[r for r in out if r.label not in label_fields or label_fields[r.label] in protected]
            out.append(FactRow(label="Conversation summary",value=learning['summary']))
            out.append(FactRow(label="Profile learning",value=learning['state'] +
                (" · "+learning['errorCode'] if learning['errorCode'] else "") +
                (" · Updated "+learning['lastUpdated']+" UTC" if learning['lastUpdated'] else "")))
            out.append(FactRow(label="History coverage",value=str(learning['processedMessages'])+
                " messages processed · "+("Stored history analysed" if learning['storedHistoryProcessed'] else "Analysis pending")))
            c=learning['coverage']
            out.append(FactRow(label="Creator profile coverage",value=
                f"{c['processedFans']}/{c['totalDiscoveredFans']} fans processed · {c['pendingProfiles']} pending · "
                f"{c['deferredFailures']} deferred · queue lag {c['queueLagSeconds']}s · " +
                ("Historical import incomplete" if c['historicalImportIncomplete'] else "Historical import complete")))
        return out

    async def owned_media(self, *, account_id: str, fan_id: int, limit: int) -> list[OwnedRow]:
        """What this fan has actually PAID for.

        A paid message is the durable proof available to this read: the relay
        historically ratchets `messages.is_paid` from the purchase feed.
        Dedicated canonical reversal evidence may explicitly revoke it.
        Offered-but-unbought PPV rows are NULL/false and excluded, the product rule
        `ownership.py` states at length: dedup bought, never sent.
        """
        try:
            async with get_session() as s:
                await lock_read_scope(s, account_id)
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
        """Last complete durable synchronization; message activity is not health."""
        try:
            async with get_session() as s:
                await lock_read_scope(s, account_id)
                row = (await s.execute(select(OfRuntime.last_sync_at, OfRuntime.recovery_needed)
                       .where(OfRuntime.account_id == account_id))).first()
        except SQLAlchemyError as exc:
            raise _storage_failure(exc) from exc
        return row[0] if row and not row[1] else None


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


def _message_media_ids(raw: str | None) -> list[int]:
    """Never invent an attachment from a message id or malformed history."""
    try:
        values = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    if (not isinstance(values, list) or len(values) > 100
            or any(type(mid) is not int or not 0 < mid < 2**63 for mid in values)
            or len(set(values)) != len(values)):
        return []
    return values


def _message_media_kinds(raw, ids):
    try:
        frame = json.loads(raw or '{}')
        media = frame.get('media', [])
        if type(media) is not list or len(media) > 100:
            return {}
        return {m['id']: 'photo' if m.get('type') in {'photo','image','gif'} else m['type']
                for m in media if type(m) is dict and type(m.get('id')) is int and m['id'] in ids
                and m.get('type') in {'photo','image','gif','video','audio'}}
    except (ValueError, TypeError, AttributeError):
        return {}

