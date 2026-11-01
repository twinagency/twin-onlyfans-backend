"""Bounded R5 attempts in OfConversationState, never a sender or a sales planner.

The contextual planner supplies evidence and proposes actions. This module owns
only durable counting and allowed transitions. Message/receipt projections are
the evidence of output; a draft, model decision or payment event counts nothing.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from sqlalchemy import select

from db.models import Message, OfConversationState
from db.engine import get_session
from .commercial_controls import controls

CONVERSATIONAL = frozenset({"ai_chatter", "ai_upseller", "autoreply", "native_reply", "onlyfans_native_text"})
CLARIFICATIONS = frozenset({"product_choice", "product_question", "ambiguous_invitation"})
DISPOSITIONS = frozenset({"no_current_interest", "no_matching_inventory", "refused_item", "general_refusal",
                          "budget", "support", "distress", "consent", "identity", "takeover",
                          "purchase_coverage", "delivery_uncertain", "unavailable", "review_required"})


class ProgressRefused(RuntimeError):
    pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def decode(raw):
    if not raw:
        return {"version": 1, "attempt": None, "completed": []}
    data = json.loads(raw)
    if data.get("version") != 1 or not isinstance(data.get("completed"), list):
        raise ProgressRefused("commercial-state-needs-review")
    return data


def start(source_id, source_body, *, prior_card=False, now=None):
    return {"id": str(uuid.uuid4()), "phase": "repeat" if prior_card else "first",
            "source_id": int(source_id), "started_at": (now or datetime.utcnow()).isoformat(),
            "source_hash": fingerprint(source_body), "status": "active", "bubbles": 0, "replies": 0,
            "extension_used": False, "extension_turn": None, "invitation_id": None,
            "item_ids": [], "reason": None, "resume_when": None, "decisions": []}


def count_messages(attempt, messages):
    """Count confirmed rows since start, grouping one fan turn's output once.

    No session timeout and no bounded history tail. Filler consumes allowance.
    Campaigns are not conversational progress; human activity is observed and
    recorded, and existing takeover/ownership guards invalidate obsolete work.
    """
    bubbles, turns, current_fan, card, external = 0, set(), None, None, []
    for m in messages:
        if m.message_id < attempt["source_id"]:
            continue
        if m.direction == "in":
            if not m.is_unsent:
                current_fan = m.message_id
            continue
        if m.direction != "out" or getattr(m, "is_tip", False):
            continue
        # A confirmed paid delivery is terminal even when it remains unpaid.
        if m.price_cents and m.price_cents > 0 and not m.is_unsent:
            card = m.message_id
            break
        if m.automation_kind not in CONVERSATIONAL and m.automation_kind is not None:
            continue
        if m.automation_kind is None:
            if not m.automation_kind:
                external.append(m.message_id)
        if m.is_unsent:
            # An unsent conversational bubble was still confirmed output. A
            # retraction cannot buy another pacing allowance.
            pass
        bubbles += 1
        turns.add(current_fan if current_fan is not None else attempt["source_id"])
    attempt["bubbles"], attempt["replies"] = bubbles, len(turns)
    if external:
        attempt["external_message_ids"] = external
    if card is not None:
        attempt.update(status="offered", card_message_id=card, reason="confirmed_paid_card", resume_when="new_fan_participation")
    return attempt


def boundary(attempt, cfg, planned_bubbles=1):
    c = controls(cfg)
    lo, hi = c.first_window if attempt["phase"] == "first" else c.repeat_window
    units = attempt["bubbles"] if attempt["phase"] == "first" else attempt["replies"]
    # Whole replies stay whole, including a response crossing the bubble bound.
    projected = units + (planned_bubbles if attempt["phase"] == "first" else 1)
    return {"lower": lo, "upper": hi, "units": "bubbles" if attempt["phase"] == "first" else "replies",
            "completed": units, "projected": projected, "due": projected >= hi}


def transition(attempt, *, action, source_id, cfg, evidence=None, item_ids=(), invitation_id=None,
               clarification=None, missing=None, disposition=None, resume_when=None, planned_bubbles=1):
    """Validate a proposed response. Never turns a count or a reason into a send."""
    evidence = evidence or {}
    b = boundary(attempt, cfg, planned_bubbles)
    resolving = action in {"offer", "disposition", "hold"}
    relevant_answer = int(source_id) != attempt.get("extension_turn")
    if attempt["status"] == "offered":
        raise ProgressRefused("commercial-attempt-already-offered")
    if action == "offer":
        if not evidence.get("verified_match") or not item_ids or not evidence.get("fan_interest"):
            raise ProgressRefused("commercial-offer-needs-verified-interest")
        # Delivery, never the proposal, closes an attempt.
        attempt["status"] = "offer_planned"
    elif action == "invite":
        if not item_ids or not evidence.get("verified_match") or not invitation_id:
            raise ProgressRefused("commercial-invitation-needs-verified-item")
        if attempt.get("extension_used") and relevant_answer:
            raise ProgressRefused("commercial-extension-must-resolve")
        attempt.update(status="awaiting_answer", invitation_id=invitation_id, item_ids=list(item_ids))
    elif action == "clarify":
        if (clarification not in CLARIFICATIONS or not missing or not item_ids
                or not evidence.get("supporting_message_id") or evidence.get("answer_known")
                or evidence.get("question_already_ignored")):
            raise ProgressRefused("commercial-clarification-not-necessary")
        if attempt.get("extension_used") and relevant_answer:
            raise ProgressRefused("commercial-clarification-extension-exhausted")
        import re
        fan_text = str(evidence.get("fan_text") or "")
        patterns = {
            "product_choice": r"\b(which|choose|choice|either|or|format|photo|video)\b",
            "product_question": r"\?|\b(price|cost|how much|include|length|long|preview)\b",
            "ambiguous_invitation": r"\b(maybe|perhaps|what|mean|sure about|not sure)\b",
        }
        if not re.search(patterns[clarification], fan_text, re.I):
            raise ProgressRefused("commercial-clarification-unsupported-by-fan")
        attempt.update(missing=missing, item_ids=list(item_ids), clarification=clarification)
        if b["due"]:
            attempt.update(extension_used=True, extension_turn=int(source_id), status="clarification_pending",
                           missing=missing, item_ids=list(item_ids), clarification=clarification)
    elif action in {"hold", "disposition"}:
        if disposition not in DISPOSITIONS or not resume_when or not evidence:
            raise ProgressRefused("commercial-disposition-needs-evidence-and-resumption")
        attempt.update(status="held" if action == "hold" else "closed_no_offer", reason=disposition,
                       resume_when=resume_when, blocked_evidence=evidence)
        if evidence.get("source_hash"):
            attempt["source_hash"] = evidence["source_hash"]
    elif action == "continue":
        if attempt.get("extension_used") and relevant_answer and not resolving:
            raise ProgressRefused("commercial-extension-must-resolve")
        if b["due"]:
            raise ProgressRefused("commercial-boundary-needs-decision")
    else:
        raise ProgressRefused("commercial-invalid-action")
    attempt["last_decision"] = {"source_id": int(source_id), "action": action, "boundary": b,
        "settings": controls(cfg).as_dict(), "evidence": evidence, "item_ids": list(item_ids),
        "invitation_id": invitation_id, "disposition": disposition, "resume_when": resume_when}
    # Immutable per-attempt decision entries, not replacements hidden by retries.
    entry = attempt["last_decision"]
    if not attempt["decisions"] or attempt["decisions"][-1] != entry:
        attempt["decisions"].append(entry)
    return attempt


async def delivery_unresolved(session, row):
    if not row.unresolved_attempt_id:
        return False
    from native_pilot.ledger import settled_pointer
    from sqlalchemy import inspect, text
    def check(sync):
        prefix = "fastt_runtime." if sync.bind.dialect.name == "postgresql" else ""
        schema = "fastt_runtime" if prefix else None
        if any(not inspect(sync.connection()).has_table(name, schema=schema)
               for name in ("native_pilot_receipts", "twin_bridge_send_attempts")):
            return True
        class Reader:
            def execute(self, sql, params):
                for i in range(len(params)):
                    sql = sql.replace("?", ":p"+str(i), 1)
                return sync.execute(text(sql), {"p"+str(i): v for i, v in enumerate(params)})
        return not settled_pointer(Reader(), row.account_id, row.fan_id, row.unresolved_attempt_id, prefix=prefix)
    return await session.run_sync(check)


async def load(account_id, fan_id, source_id, source_body, *, cfg, new_opportunity=False):
    """Begin/reconcile inside the existing row lock; no timer calls this function.

    new_opportunity is supplied only after the existing resolver verifies fresh
    fan evidence, or an existing authority verifies that a blocker has cleared.
    Merely receiving another input cannot reopen a closed attempt.
    """
    async with get_session() as s:
        row = (await s.execute(select(OfConversationState).where(
            OfConversationState.account_id == str(account_id), OfConversationState.fan_id == int(fan_id))
            .with_for_update())).scalar_one_or_none()
        if row is None:
            return None
        data = decode(row.commercial_state_json)
        attempt = data["attempt"]
        messages = (await s.execute(select(Message).where(Message.account_id == str(account_id),
            Message.fan_id == int(fan_id)).order_by(Message.message_id))).scalars().all()
        if attempt:
            count_messages(attempt, messages)
        owns = row.mode == "automated" and row.takeover_state == "none" and not await delivery_unresolved(s, row)
        if not owns:
            if attempt and attempt["status"] != "offered":
                attempt.update(status="held", reason="takeover" if row.mode != "automated" else "delivery_uncertain",
                               resume_when="existing_authority_cleared", ownership_revision=row.revision)
        elif attempt is None:
            prior = any(m.direction == "out" and (m.price_cents or 0) > 0 for m in messages)
            attempt = data["attempt"] = start(source_id, source_body, prior_card=prior)
        elif (attempt["status"] == "offered" and int(source_id) > attempt["card_message_id"]
              or attempt["status"] == "closed_no_offer" and new_opportunity):
            data["completed"].append(attempt)
            attempt = data["attempt"] = start(source_id, source_body, prior_card=(attempt["phase"] == "repeat" or attempt["status"] == "offered"))
        elif attempt["status"] == "held" and new_opportunity:
            # Preserve its age, counters and one-extension history across holds.
            attempt.update(status="active", reason=None, resumed_by=int(source_id))
        from .contextual_mode import mode
        if mode(cfg) == "shadow":
            # Diagnostics may project a hypothetical attempt but never modify
            # the live conversation's counters or allowed transitions.
            return attempt
        row.commercial_state_json = json.dumps(data, sort_keys=True)
        await s.commit()
        return attempt


async def save_decision(account_id, fan_id, source_id, source_body, *, cfg, **decision):
    async with get_session() as s:
        row = (await s.execute(select(OfConversationState).where(
            OfConversationState.account_id == str(account_id), OfConversationState.fan_id == int(fan_id))
            .with_for_update())).scalar_one_or_none()
        if row is None:
            return None
        source = (await s.execute(select(Message).where(Message.account_id == str(account_id),
            Message.fan_id == int(fan_id), Message.message_id == int(source_id)))).scalar_one_or_none()
        from automations.ai_chatter import _strip_html
        if (source is None or source.fan_id != int(fan_id) or source.direction != "in" or source.is_unsent
                or _strip_html(source.body or "") != source_body):
            raise ProgressRefused("commercial-input-revised")
        data = decode(row.commercial_state_json)
        attempt = data["attempt"]
        if attempt is None:
            raise ProgressRefused("commercial-attempt-missing")
        if decision["action"] in {"offer", "invite", "clarify"} and (
                row.mode != "automated" or row.takeover_state != "none" or await delivery_unresolved(s, row)):
            raise ProgressRefused("commercial-ownership-changed")
        transition(attempt, source_id=source_id, cfg=cfg, **decision)
        row.commercial_state_json = json.dumps(data, sort_keys=True)
        await s.commit()
        return attempt


def prompt(attempt, cfg):
    if attempt is None:
        return ""
    b = boundary(attempt, cfg)
    return ("ONLYFANS CONVERSATIONAL SELLING: Answer the fan's whole current message first. Commercial initiative "
            f"is {controls(cfg).initiative}. First window counts confirmed bubbles, repeat window counts grouped replies. "
            f"Current attempt: {json.dumps({k: attempt.get(k) for k in ('phase','status','bubbles','replies','extension_used','missing','reason','resume_when')})}. "
            f"Reference: {json.dumps(b)}. Prefer an appropriate verified offer, a specific available-item invitation, or "
            "naturally developing relevant interest. No mechanical rotation, obligatory invitation, generic pitch or "
            "rapport delay on a direct request. Never invent content, fan interest or prices. At the upper boundary "
            "choose an offer, specific invitation, one necessary product clarification, or an evidence-backed "
            "disposition/hold. No generic continue-warming-up beyond that point. A used clarification must resolve "
            "on the next relevant answer. A purchase alone starts no turn. Respect existing restrictions and holds.")
