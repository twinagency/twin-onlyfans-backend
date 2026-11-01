"""Glue between the engine's existing seams and the R5 decision layer (no sender, no model call of its own).

* ``ready_shelf``       the media ids product readiness says may be offered to this fan (one definition);
* ``counts``            grouped creator turns / raw bubbles since the last paid card;
* ``accept_invitation`` a short affirmation that unambiguously accepts a stored invitation, revalidated;
* ``record``            one decision row for the turn, best effort, never raising into the reply path.

Everything is a no-op unless contextual mode is shadow/live for the creator: off keeps today's behaviour exactly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from db.engine import get_session
from db.models import Message, VaultItem
from onlyfans_runtime.commercial_controls import controls as read_controls
from onlyfans_runtime.contextual_mode import mode as contextual_mode
from . import sales_decisions, sales_invitation
from .sales_action import ActionDecision, ActionInputs, Counts, count_since_last_card, decide, EXCLUDED, BLOCKED, ANSWER

log = logging.getLogger(__name__)
_WINDOW = 150


def active(cfg) -> bool:
    return contextual_mode(cfg or {}) in ("shadow", "live")


async def ready_shelf(lane, fan_id: int) -> set[int]:
    from .product_readiness import ready_ids
    from . import pack_sender
    async with get_session() as s:
        rows = (await s.execute(select(VaultItem).where(
            VaultItem.account_id == lane.account_id, VaultItem.removed_at.is_(None),
            VaultItem.search_text.is_not(None), VaultItem.search_text != "")
            .order_by(VaultItem.created_at.desc()).limit(400))).scalars().all()
    return ready_ids(rows, account_id=str(lane.account_id), cfg=lane.cfg,
                     bought=await pack_sender._bought_media(lane.account_id, fan_id),
                     offered=set(await pack_sender.offered_media(lane.account_id, fan_id)),
                     limits=(lane.cfg or {}).get("limits") or (), price_source="pack_ladder")


async def counts(account_id: str, fan_id: int) -> Counts:
    async with get_session() as s:
        rows = (await s.execute(select(Message.direction, Message.price_cents, Message.body).where(
            Message.account_id == str(account_id), Message.fan_id == int(fan_id), Message.is_unsent.is_(False))
            .order_by(Message.message_id.desc()).limit(_WINDOW))).all()
    return count_since_last_card([(d, bool(p and p > 0 and d == "out"), b) for d, p, b in reversed(rows)])


@dataclass(frozen=True)
class Accepted:
    invitation_id: str
    media_ids: list
    subject: str | None
    resolution: object
    contract: object


async def accept_invitation(lane, candidate, fan) -> Accepted | None:
    """Live only. Resolves the stored invitation a bare yes/thumbs-up accepts, then revalidates that the invited
    items are STILL ready (available, unowned, unoffered, in boundary, priced). A failed revalidation sends nothing
    and records why; it never falls back to some other item."""
    if contextual_mode(lane.cfg or {}) != "live":
        return None
    try:
        await sales_invitation.settle(lane.account_id, fan.fan_id)
        acceptance, why = await sales_invitation.resolve_acceptance(lane.account_id, fan.fan_id)
        if acceptance is None:
            return None
        shelf = await ready_shelf(lane, fan.fan_id)
        ids = list(acceptance.media_ids)
        if not set(ids) <= shelf:
            d = ActionDecision(ANSWER, EXCLUDED, "acceptance_not_revalidated", "none", None,
                               {**read_controls(lane.cfg).as_dict(), "mode": "live"}, acceptance.invitation_id)
            await record(lane, candidate, fan, d, evidence={"invited": ids, "ready": sorted(set(ids) & shelf)})
            return None
        from .content_contract import Contract, Resolution
        async with get_session() as s:
            subject = (await s.execute(select(sales_invitation.OfSalesInvitation.subject).where(
                sales_invitation.OfSalesInvitation.account_id == str(lane.account_id),
                sales_invitation.OfSalesInvitation.invitation_id == acceptance.invitation_id))).scalar()
        contract = Contract(is_ask=False, subject=subject, strict=True, quote="")
        return Accepted(acceptance.invitation_id, ids, subject, Resolution(media_ids=ids, contract=contract), contract)
    except Exception:
        log.exception("sales_turn accept_invitation failed account=%s", getattr(lane, "account_id", None))
        return None


async def record(lane, candidate, fan, decision: ActionDecision, *, evidence: dict | None = None, stage: str = "decide"):
    """Persist one decision row for this fan turn. Off writes nothing."""
    if not active(lane.cfg):
        return False
    try:
        mid = getattr(candidate, "last_in_mid", None)
        if not mid:
            return False
        c = await counts(lane.account_id, fan.fan_id)
        return await sales_decisions.record(lane.account_id, fan.fan_id, int(mid), decision, counts=c,
                                            evidence=evidence, stage=stage)
    except Exception:
        return False


def inputs_for(lane, counts_: Counts, **kw) -> ActionInputs:
    return ActionInputs(mode=contextual_mode(lane.cfg or {}), controls=read_controls(lane.cfg),
                        grouped_turns=counts_.grouped_turns, prior_card=counts_.prior_card, **kw)
