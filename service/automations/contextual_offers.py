"""One optional decision inside the existing fan turn, with no extra sender."""
import asyncio
from dataclasses import replace

from sqlalchemy import select, or_

from db.engine import get_session
from db.models import VaultItem
from native_pilot.ledger import Refused
from onlyfans_runtime.contextual_mode import mode
from ._sell_signal import ContentDecision
from .contextual_evidence import recent_context


async def plan_contextual(lane, authority, candidate, fan, *, blocked=False, voice="her"):
    """Return (plan, permission, decision), or None. Shadow never calls a planner.

    Existing explicit/affirmative asks are routed by the caller first. No prompt
    or normal draft is changed here; off makes no contextual inference call.
    """
    current_mode = mode(lane.cfg)
    context = recent_context(candidate, fan)
    def event(stage, reason, **extra):
        if authority is not None:
            authority.emit({"event":"contextual-opportunity","stage":stage,"reason":reason,"mode":current_mode,"fan":fan.fan_id,**extra})
    if (current_mode == "off" or authority is None or not lane.on or blocked or context is None
            or candidate.last_in_mid != authority.source_message_id):
        event("skipped", "disabled_blocked_or_stale_input")
        return None
    try:
        permission = await asyncio.to_thread(authority.check, current_mode)
        from .sell_lane import Turn
        turn = Turn(text=candidate.last_in_text, at=candidate.last_in_at,
                    trigger="contextual_relevance", supporting_message_ids=(candidate.last_in_mid,))
        if not await lane.may_sell(fan.fan_id, turn, fan=fan, blocked=blocked):
            event("rejected", "sales_eligibility")
            return None
        # Confirm an account-owned description before paying for a relevance read.
        async with get_session() as s:
            available = (await s.execute(select(VaultItem.media_id).where(
                VaultItem.account_id == lane.account_id, VaultItem.removed_at.is_(None),
                VaultItem.search_text.is_not(None), VaultItem.search_text != "").limit(1))).first()
        if not available:
            authority.emit({"event":"contextual-decision","action":"chat","reason":"no_supported_shelf", "mode":current_mode})
            event("rejected", "no_described_content")
            return None
        from . import content_prompts, content_resolver, pack_sender
        event("evaluated", "conversation_evidence")
        hook = await content_prompts.read_hook(lane.account_id, fan.fan_id, voice=voice,
            contextual_message=context)
        if hook is None:
            event("rejected", "no_supported_interest_or_evaluation_failed")
            return None
        if hook.action == "explore":
            if current_mode == "live":
                authority.reply_override = hook.bridge
            event("selected", "explore_interest", action="explore")
            return None
        contract = replace(hook.contract, strict=True, is_ask=False)
        # Own/pending media is never a substitute for a missing contextual match.
        seen = await pack_sender._bought_media(lane.account_id, fan.fan_id)
        resolution = await content_resolver.resolve(lane.account_id, fan.fan_id,
            count=pack_sender.MAX_ITEMS, seen=seen, contract=contract, require_curated=False)
        if not resolution.ok:
            authority.emit({"event":"contextual-decision","action":"chat","reason":"no_verified_match", "mode":current_mode})
            event("rejected", "no_verified_match")
            return None
        decision = ContentDecision("send_ppv", "contextual_relevance", hook.supporting_message_ids, hook.reason or hook.what[:120])
        authority.emit({"event":"contextual-decision","mode":current_mode,"action":decision.action,
            "trigger":decision.trigger,"supporting_message_ids":list(decision.supporting_message_ids),
            "reason":decision.reason,"matched_media_ids":list(resolution.media_ids),
            "pricing":"not_planned" if current_mode == "shadow" else "existing_fastt_rules"})
        event("selected", decision.reason, action="send_ppv", supporting_message_ids=list(decision.supporting_message_ids))
        if current_mode == "shadow":
            return None

        async def read_contract():
            return contract
        plan = await lane.plan(fan.fan_id, turn, fan=fan, blocked=blocked,
                               contract=read_contract, resolution=resolution)
        if not plan:
            event("rejected", getattr(getattr(plan, "result", None), "reason", "planning_failed"))
        return (plan, permission, decision) if plan else None
    except Refused as exc:
        event("rejected", exc.reason)
        authority.emit({"event":"contextual-withheld","reason":exc.reason,"mode":current_mode})
        return None
    except Exception:
        event("rejected", "contextual_evaluation_unavailable")
        authority.emit({"event":"contextual-withheld","reason":"contextual-evaluation-unavailable","mode":current_mode})
        return None
