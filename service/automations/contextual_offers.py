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

    Wraps the evaluation so every evaluated, excluded and blocked turn leaves one decision row (R5). With contextual
    mode off nothing is recorded and nothing differs from before.
    """
    trace: list = []
    result = await _plan_contextual(lane, authority, candidate, fan, blocked=blocked, voice=voice, _trace=trace)
    if mode(lane.cfg) != "off":
        try:
            from . import sales_turn
            decision = _final_decision(lane, trace, result)
            if decision is not None:
                await sales_turn.record(lane, candidate, fan, decision, evidence=_evidence(trace, result))
        except Exception:
            pass                                # observability never breaks the reply path
    return result


def invitation_line(bridge: str) -> str:
    """The creator's own bridge line turned into a question about THAT content (an invitation, not a card)."""
    line = " ".join(str(bridge or "").split())
    if "?" in line:
        return line
    return line.rstrip(" .!") + ", want me to show you?"


def _known_clarification(hook, contract, fan_text):
    """Verified price and an explicitly chosen media format need no requalification."""
    import re
    text = fan_text.lower()
    if hook.clarification == "product_question" and re.search(r"\b(price|cost|how much)\b", text):
        return True  # SellLane has already produced the canonical amount.
    if hook.clarification == "product_choice" and contract.media_kind:
        pattern = r"\b(photos?|pics?|pictures?)\b" if contract.media_kind == "photo" else r"\b(videos?|clips?|vids?)\b"
        return bool(re.search(pattern, text) and not re.search(r"\b(which|either|or|not sure)\b", text))
    return False


_BLOCKED_CODES = frozenset({"sales_eligibility_refused", "purchase_freshness_refused", "evaluation_internal_failure"})


def _evidence(trace, result):
    out = {"events": [{"stage": t[0], "reason": t[1], "details": t[2]} for t in trace if t[0] != "decision"][:8]}
    if result is not None:
        out["supporting_message_ids"] = list(result[2].supporting_message_ids)
    return out


def _final_decision(lane, trace, result):
    from . import sales_action as sa
    from onlyfans_runtime.commercial_controls import controls
    settings = {**controls(lane.cfg).as_dict(), "mode": mode(lane.cfg)}
    made = [t[1] for t in trace if t[0] == "decision"]
    # A proposed action that failed planning/eligibility is not an acted offer.
    if made and trace[-1][0] != "rejected":
        return made[-1]
    last = trace[-1] if trace else ("skipped", "disabled_blocked_or_stale_input", {})
    stage, reason = last[0], last[1]
    if result is not None:
        return sa.ActionDecision(sa.PROACTIVE_OFFER, sa.ACTED, "current_interest", "offer", None, settings)
    if stage == "selected" and reason == "explore_interest":
        return sa.ActionDecision(sa.DEVELOP, sa.ACTED, "explore_interest", "develop", None, settings)
    if stage == "selected":
        return sa.ActionDecision(sa.PROACTIVE_OFFER, sa.ACTED, "current_interest", "offer", None, settings)
    code = (last[2] or {}).get("failure_code") or reason
    outcome = sa.BLOCKED if code in _BLOCKED_CODES or stage == "skipped" else sa.EXCLUDED
    return sa.ActionDecision(sa.ANSWER, outcome, str(code)[:64], "none", None, settings)


async def _plan_contextual(lane, authority, candidate, fan, *, blocked=False, voice="her", _trace=None):
    """The evaluation itself (unchanged contract): (plan, permission, decision) or None.

    Existing explicit/affirmative asks are routed by the caller first. No prompt
    or normal draft is changed here; off makes no contextual inference call.
    """
    current_mode = mode(lane.cfg)

    account_id = getattr(lane, "account_id", None)

    def event(stage, reason, **extra):
        if _trace is not None:
            _trace.append((stage, reason, extra))
        if authority is not None:
            authority.emit({"event":"contextual-opportunity","stage":stage,"reason":reason,
                            "mode":current_mode,"account_id":account_id,"fan_id":fan.fan_id,
                            "source_message_id":getattr(authority,"source_message_id",None),**extra})

    def rejected(code, **extra):
        event("rejected", code, failure_code=code, **extra)
        if not hasattr(lane, "_progress_evaluated"):
            lane._progress_evaluated = {}
        lane._progress_evaluated[fan.fan_id] = code

    def refusal_code(reason):
        if reason in ("contextual-purchase-state-stale", "contextual-purchase-unlinked"):
            return "purchase_freshness_refused"
        return "sales_eligibility_refused"

    if current_mode == "off" or authority is None or not lane.on or blocked:
        event("skipped", "disabled_blocked_or_stale_input")
        return None
    context = recent_context(candidate, fan)
    if context is None or candidate.last_in_mid != authority.source_message_id:
        event("skipped", "disabled_blocked_or_stale_input")
        return None
    from onlyfans_runtime import commercial_progress as progress
    from . import sales_turn
    attempt = await sales_turn.prepare(lane, candidate, fan)
    context["commercial_progress"] = attempt
    context["commercial_settings"] = sales_turn.read_controls(lane.cfg).as_dict()
    try:
        permission = await asyncio.to_thread(authority.check, current_mode)
        from .sell_lane import Turn
        turn = Turn(text=candidate.last_in_text, at=candidate.last_in_at,
                    trigger="contextual_relevance", supporting_message_ids=(candidate.last_in_mid,))
        if not await lane.may_sell(fan.fan_id, turn, fan=fan, blocked=blocked):
            rejected("sales_eligibility_refused")
            return None
        # Product readiness (not "search_text is non-empty": the import writes the bare kind word) decides whether
        # the account has anything it may offer before a relevance read is paid for.
        from . import pack_sender, sales_turn
        shelf = await sales_turn.ready_shelf(lane, fan.fan_id, session=get_session)
        available = bool(shelf)
        if not available:
            authority.emit({"event":"contextual-decision","action":"chat","reason":"no_supported_shelf",
                            "mode":current_mode,"account_id":account_id,"fan_id":fan.fan_id})
            rejected("no_matching_media", detail="no_described_content")
            return None
        from . import content_prompts, content_resolver
        event("evaluated", "conversation_evidence")
        hook_rejections = []

        def hook_rejected(code):
            hook_rejections.append(code)
            rejected(code)

        hook = await content_prompts.read_hook(lane.account_id, fan.fan_id, voice=voice,
            contextual_message=context, on_reject=hook_rejected)
        if hook is None:
            if not hook_rejections:
                rejected("evaluation_failure")
            return None
        from . import sales_action as sa, sales_turn
        counts = await sales_turn.counts(lane.account_id, fan.fan_id)
        due = attempt is not None and progress.boundary(attempt, lane.cfg)["due"]
        extending = attempt is not None and attempt.get("extension_used")
        if hook.action == "explore" and not due and not extending:
            # Developing a topic is the existing behaviour. Only an explicit "lower" initiative with no pacing
            # pressure leaves the plain answer alone; every other setting (including the default) is unchanged.
            quiet = (sa.pacing(sales_turn.read_controls(lane.cfg), counts.grouped_turns, prior_card=counts.prior_card, raw_bubbles=counts.raw_bubbles).pressure == 0
                     and sales_turn.read_controls(lane.cfg).initiative == "lower")
            if quiet:
                event("evaluated", "insufficient_evidence")
                if _trace is not None:
                    _trace.append(("decision", sa.decide(sales_turn.inputs_for(lane, counts, evidence="none", ready_items=len(shelf)))))
                return None
            if current_mode == "live" and attempt and attempt["status"] not in ("closed_no_offer", "held", "awaiting_answer"):
                authority.reply_override = hook.bridge
            event("selected", "explore_interest", action="explore")
            return None
        contract = replace(hook.contract, strict=True, is_ask=False)
        # Own/pending media is never a substitute for a missing contextual match.
        seen = await pack_sender._bought_media(lane.account_id, fan.fan_id)
        seen |= set(await pack_sender.offered_media(lane.account_id, fan.fan_id))
        resolution = await content_resolver.resolve(lane.account_id, fan.fan_id,
            count=pack_sender.MAX_ITEMS, seen=seen, contract=contract, require_curated=False)
        if not resolution.ok:
            authority.emit({"event":"contextual-decision","action":"chat","reason":"no_verified_match",
                            "mode":current_mode,"account_id":account_id,"fan_id":fan.fan_id})
            rejected("no_matching_media")
            return None
        # A closed attempt only reopens after the existing evaluator AND resolver
        # establish a fresh opportunity. Another input or a config reload alone
        # is never sufficient.
        if attempt and attempt["status"] in ("closed_no_offer", "held"):
            if progress.fingerprint(candidate.last_in_text) == attempt.get("source_hash"):
                rejected("no_new_relevant_evidence")
                return None
            attempt = await sales_turn.prepare(lane, candidate, fan, new_opportunity=True)
        from . import sales_invitation
        history = await sales_invitation.history(lane.account_id, fan.fan_id, resolution.media_ids)
        choice = sa.decide(sales_turn.inputs_for(lane, counts, evidence="current_interest", matched=True,
                                                 ready_items=len(shelf), declined_recently=history["declined_same_item"],
                                                 recent_ignored_same_item=history["ignored_same_item"]))
        if _trace is not None:
            _trace.append(("decision", choice))
        if choice.reason in ("recent_refusal", "ignored_invitation_not_repeated", "identical_card_unresolved"):
            rejected(choice.reason)
            return None
        ambiguous_invitation = (hook.action == "clarify" and hook.clarification == "ambiguous_invitation"
            and history["open"] == 1 and len(history.get("open_matching", [])) == 1
            and sales_invitation.ambiguous_answer(candidate.last_in_text))
        if history["open"] and not ambiguous_invitation:
            rejected("awaiting_invitation_answer")
            return None
        if hook.action in ("invite", "explore") and not extending:
            choice = replace(choice, action=sa.INVITE, outcome=sa.ACTED, approach="invite", reason="specific_item_invitation")
            if _trace is not None:
                _trace.append(("decision", choice))
        # Planning still goes through SellLane before an invitation is made, so
        # it names a currently deliverable product and an actual canonical quote.
        async def read_contract():
            return contract
        plan = await lane.plan(fan.fan_id, turn, fan=fan, blocked=blocked,
                               contract=read_contract, resolution=resolution) if current_mode == "live" else None
        if current_mode == "live" and not plan:
            rejected("sales_eligibility_refused", detail=getattr(getattr(plan, "result", None), "reason", "planning_failed"))
            return None
        if hook.action == "clarify":
            if current_mode == "live":
                try:
                    await sales_turn.progress_decision(lane, candidate, fan, action="clarify",
                        item_ids=resolution.media_ids, clarification=hook.clarification, missing=hook.missing,
                        evidence={"supporting_message_id": candidate.last_in_mid, "fan_text": candidate.last_in_text,
                                  "verified_match": True,
                                  "answer_known": _known_clarification(hook, contract, candidate.last_in_text),
                                  "question_already_ignored": bool(attempt and attempt.get("missing") == hook.missing
                                      and attempt.get("last_decision", {}).get("source_id") != candidate.last_in_mid)})
                except progress.ProgressRefused as exc:
                    rejected(str(exc))
                    return None
                if ambiguous_invitation:
                    await sales_invitation.associate_clarification(lane.account_id, fan.fan_id,
                        history["open_matching"][0], candidate.last_in_mid, hook.bridge)
                    from telegram_runtime.capabilities import active_telegram_capabilities
                    content = getattr(active_telegram_capabilities(), "content", None)
                    if content is not None:
                        content.commercial_text = True
                authority.reply_override = hook.bridge
                authority.reply_override_action = "clarify"
            if _trace is not None:
                _trace.append(("decision", replace(choice, action=sa.CLARIFY, outcome=sa.ACTED,
                               approach="none", reason="necessary_product_clarification")))
            event("selected", "necessary_product_clarification", action="clarify")
            return None
        if choice.action == sa.INVITE:
            # A lower initiative with no pacing pressure invites instead of presenting the card: the invitation is
            # stored against these exact items and this fan-side evidence, so a later short yes accepts THAT item.
            if current_mode == "live":
                p = plan.delivery.plan
                from onlyfans_runtime.quote import format_price
                line = f"I have a set showing {contract.subject}. Want me to show you that paid set?"
                terms = {"media_ids": list(p.attached), "preview_ids": list(p.previews), "price_cents": p.price_cents,
                         "item_id": p.item_id, "inventory_revision": await sales_turn.inventory_revision(lane.account_id, p.attached)}
                invitation_id = await sales_invitation.propose(lane.account_id, fan.fan_id, p.media,
                    source_message_id=candidate.last_in_mid, subject=contract.subject, offer_terms=terms, invitation_body=line)
                await sales_turn.progress_decision(lane, candidate, fan, action="invite", item_ids=p.media,
                    invitation_id=invitation_id, evidence={"verified_match": True, "supporting_message_id": candidate.last_in_mid})
                authority.reply_override = line
                authority.reply_override_action = "invite"
                # The existing Gateway applies commercial holds to this text
                # again immediately before dispatch, including plan/wire races.
                from telegram_runtime.capabilities import active_telegram_capabilities
                content = getattr(active_telegram_capabilities(), "content", None)
                if content is not None:
                    content.commercial_text = True
                if _trace is not None:
                    _trace.append(("decision", replace(choice, invitation_id=invitation_id)))
            event("selected", "invitation", action="invite", supporting_message_ids=list(hook.supporting_message_ids))
            return None
        if not choice.sends_card:
            rejected(choice.reason)
            return None
        decision = ContentDecision("send_ppv", "contextual_relevance", hook.supporting_message_ids, hook.reason or hook.what[:120])
        authority.emit({"event":"contextual-decision","mode":current_mode,"action":decision.action,
            "trigger":decision.trigger,"supporting_message_ids":list(decision.supporting_message_ids),
            "reason":"verified_contextual_match","matched_media_ids":list(resolution.media_ids),
            "account_id":account_id,"fan_id":fan.fan_id,
            "pricing":"not_planned" if current_mode == "shadow" else "existing_fastt_rules"})
        event("selected", "contextual_match", action="send_ppv",
              supporting_message_ids=list(decision.supporting_message_ids))
        if current_mode == "shadow":
            return None

        await sales_turn.progress_decision(lane, candidate, fan, action="offer", item_ids=resolution.media_ids,
            evidence={"verified_match": True, "fan_interest": True, "supporting_message_ids": list(hook.supporting_message_ids)})
        if not plan:
            rejected("sales_eligibility_refused",
                     detail=getattr(getattr(plan, "result", None), "reason", "planning_failed"))
        return (plan, permission, decision) if plan else None
    except Refused as exc:
        code = refusal_code(exc.reason)
        rejected(code, detail=exc.reason)
        authority.emit({"event":"contextual-withheld","reason":exc.reason,
                        "failure_code":code,"mode":current_mode})
        return None
    except Exception:
        rejected("evaluation_internal_failure")
        authority.emit({"event":"contextual-withheld","reason":"contextual-evaluation-unavailable","mode":current_mode})
        return None

