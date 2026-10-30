"""One optional decision inside the existing fan turn, with no extra sender."""
import asyncio
from dataclasses import replace

from sqlalchemy import select, or_

from db.engine import get_session
from db.models import VaultItem
from native_pilot.ledger import Refused
from onlyfans_runtime.contextual_mode import mode
from ._sell_signal import ContentDecision, contextual_terms


async def plan_contextual(lane, authority, candidate, fan, *, blocked=False, voice="her"):
    """Return (plan, permission, decision), or None. Shadow never calls a planner.

    Existing explicit/affirmative asks are routed by the caller first. No prompt
    or normal draft is changed here; off makes no contextual inference call.
    """
    current_mode = mode(lane.cfg)
    terms = contextual_terms(candidate.last_in_text)
    if (current_mode == "off" or authority is None or not lane.on or blocked or not terms
            or candidate.last_in_mid != authority.source_message_id):
        return None
    try:
        permission = await asyncio.to_thread(authority.check, current_mode)
        from .sell_lane import Turn
        turn = Turn(text=candidate.last_in_text, at=candidate.last_in_at,
                    trigger="contextual_relevance", supporting_message_ids=(candidate.last_in_mid,))
        if not await lane.may_sell(fan.fan_id, turn, fan=fan, blocked=blocked):
            return None
        # Confirm an account-owned description before paying for a relevance read.
        async with get_session() as s:
            available = (await s.execute(select(VaultItem.media_id).where(
                VaultItem.account_id == lane.account_id, VaultItem.removed_at.is_(None),
                or_(*(VaultItem.search_text.ilike("%" + t + "%") for t in terms))).limit(1))).first()
        if not available:
            authority.emit({"event":"contextual-decision","action":"chat","reason":"no_supported_shelf", "mode":current_mode})
            return None
        from . import content_prompts, content_resolver, pack_sender
        hook = await content_prompts.read_hook(lane.account_id, fan.fan_id, voice=voice,
            contextual_message={"id":candidate.last_in_mid,"text":candidate.last_in_text})
        if hook is None or hook.source != "situation":
            return None
        contract = replace(hook.contract, strict=True, is_ask=False)
        if not set(terms) & set(contract.terms):
            authority.emit({"event":"contextual-decision","action":"chat","reason":"topic_not_supported_by_current_message","mode":current_mode})
            return None
        # Own/pending media is never a substitute for a missing contextual match.
        seen = await pack_sender._bought_media(lane.account_id, fan.fan_id)
        resolution = await content_resolver.resolve(lane.account_id, fan.fan_id,
            count=pack_sender.MAX_ITEMS, seen=seen, contract=contract, require_curated=False)
        if not resolution.ok:
            authority.emit({"event":"contextual-decision","action":"chat","reason":"no_verified_match", "mode":current_mode})
            return None
        decision = ContentDecision("send_ppv", "contextual_relevance", (candidate.last_in_mid,), hook.what[:120])
        authority.emit({"event":"contextual-decision","mode":current_mode,"action":decision.action,
            "trigger":decision.trigger,"supporting_message_ids":list(decision.supporting_message_ids),
            "reason":decision.reason,"matched_media_ids":list(resolution.media_ids),
            "pricing":"not_planned" if current_mode == "shadow" else "existing_fastt_rules"})
        if current_mode == "shadow":
            return None

        async def read_contract():
            return contract
        plan = await lane.plan(fan.fan_id, turn, fan=fan, blocked=blocked,
                               contract=read_contract, resolution=resolution)
        return (plan, permission, decision) if plan else None
    except Refused as exc:
        authority.emit({"event":"contextual-withheld","reason":exc.reason,"mode":current_mode})
        return None
    except Exception:
        authority.emit({"event":"contextual-withheld","reason":"contextual-evaluation-unavailable","mode":current_mode})
        return None
