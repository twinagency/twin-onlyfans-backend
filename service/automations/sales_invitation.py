"""Durable invitations and their short acceptance (R5).

An invitation is a creator message that names REAL, READY content and asks whether the fan wants it. It is stored
against (account, fan, item, conversation) in ``of_sales_invitation``. A later short affirmation ("yes", a thumbs-up)
accepts THAT invitation only when it is unambiguous:

  * exactly one invitation is open for the fan (a newer invitation supersedes older ones),
  * it reached him (a creator message sits between the invited turn and his reply),
  * his reply is a bare affirmation arriving after it, and nothing but that.

Acceptance authorises PRESENTING the offer. The caller must still revalidate availability, quote and delivery
eligibility before any send, and it never charges. A creator suggestion alone is never evidence of fan interest: the
fan-side message that supported the invitation is stored (``source_message_id``).
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from db.engine import get_session
from db.models import Message
from db.onlyfans_sales_models import OfSalesInvitation
from ._sell_signal import is_affirmation, strip_emojis

_TAGS = re.compile(r"<[^>]+>")
_THUMBS = re.compile(r"^[\s‍️]*(?:[\U0001F44D\U0001F44C\U0001F64C\U0001F525✅]"
                     r"[\U0001F3FB-\U0001F3FF]?[\s‍️]*)+$")
OPEN = "open"


def _text(body) -> str:
    return " ".join(_TAGS.sub(" ", str(body or "")).split())


def short_acceptance(body) -> bool:
    """A bare yes, or a bare thumbs-up style reaction. Anything with other content is not an acceptance."""
    text = _text(body)
    if not text:
        return False
    return is_affirmation(text) or bool(_THUMBS.match(text))


def ambiguous_answer(body):
    text = _text(body).lower()
    return bool(re.fullmatch(r"(?:maybe|perhaps|not sure|which one|what do you mean|what is it)[?.! ]*", text))


async def associate_clarification(account_id, fan_id, invitation_id, source_id, body):
    """Keep the one necessary clarification on the SAME item/quote association."""
    async with get_session() as session:
        row = await session.get(OfSalesInvitation, (str(account_id), invitation_id))
        if row is None or row.fan_id != int(fan_id) or row.state != OPEN:
            raise ValueError("clarification invitation changed")
        terms = json.loads(row.offer_terms_json or "{}")
        previous = terms.get("clarification")
        if previous and previous["source_id"] != int(source_id):
            raise ValueError("invitation clarification already used")
        terms["clarification"] = {"source_id": int(source_id), "body": body, "delivery_message_id": None}
        row.offer_terms_json = json.dumps(terms, sort_keys=True)
        await session.commit()


@dataclass(frozen=True)
class Acceptance:
    invitation_id: str
    media_ids: tuple
    source_message_id: int
    accepted_turn_id: int
    offer_terms: dict | None = None


async def propose(account_id: str, fan_id: int, media_ids, *, source_message_id: int, subject: str | None = None,
                  offer_terms: dict | None = None, invitation_body: str | None = None) -> str:
    """Record an invitation to real content. Supersedes any older open invitation: one open at a time keeps a
    later 'yes' unambiguous. ``source_message_id`` is the FAN message that supported it."""
    ids = sorted({int(m) for m in media_ids})
    if not ids:
        raise ValueError("an invitation must concern real content")
    now = datetime.utcnow()
    async with get_session() as s:
        for row in (await s.execute(select(OfSalesInvitation).where(
                OfSalesInvitation.account_id == str(account_id), OfSalesInvitation.fan_id == int(fan_id),
                OfSalesInvitation.state == OPEN))).scalars():
            if json.loads(row.media_ids_json) == ids and row.source_message_id == int(source_message_id):
                return row.invitation_id                            # same invitation, same turn: idempotent
            row.state, row.resolved_at = "superseded", now
        invitation = OfSalesInvitation(account_id=str(account_id), invitation_id=str(uuid.uuid4()), fan_id=int(fan_id),
            media_ids_json=json.dumps(ids), subject=(subject or "")[:200] or None,
            source_message_id=int(source_message_id), state=OPEN, created_at=now,
            offer_terms_json=json.dumps(offer_terms, sort_keys=True) if offer_terms else None,
            invitation_body=invitation_body)
        s.add(invitation)
        await s.commit()
        return invitation.invitation_id


async def _latest_inbound(s, account_id, fan_id):
    return (await s.execute(select(Message.message_id, Message.body).where(
        Message.account_id == str(account_id), Message.fan_id == int(fan_id), Message.direction == "in",
        Message.is_unsent.is_(False)).order_by(Message.message_id.desc()).limit(1))).first()


async def resolve_acceptance(account_id: str, fan_id: int) -> tuple[Acceptance | None, str]:
    """(acceptance, reason). Idempotent for the same accepting turn. Marks the invitation accepted."""
    async with get_session() as s:
        latest = await _latest_inbound(s, account_id, fan_id)
        if latest is None:
            return None, "no_fan_message"
        turn, body = int(latest[0]), latest[1]
        if not short_acceptance(body):
            return None, "not_a_short_acceptance"
        rows = (await s.execute(select(OfSalesInvitation).where(
            OfSalesInvitation.account_id == str(account_id), OfSalesInvitation.fan_id == int(fan_id),
            OfSalesInvitation.state.in_((OPEN, "accepted"))))).scalars().all()
        done = [r for r in rows if r.state == "accepted" and r.accepted_turn_id == turn]
        if done:
            r = done[0]
            return Acceptance(r.invitation_id, tuple(json.loads(r.media_ids_json)), r.source_message_id, turn,
                              json.loads(r.offer_terms_json) if r.offer_terms_json else None), "already_accepted_this_turn"
        opened = [r for r in rows if r.state == OPEN]
        if not opened:
            return None, "no_open_invitation"
        if len(opened) > 1:
            return None, "ambiguous_invitation"
        r = opened[0]
        if r.invitation_body and not r.delivery_message_id:
            return None, "invitation_never_reached_fan"
        if turn <= r.source_message_id:
            return None, "acceptance_before_invitation"
        terms = json.loads(r.offer_terms_json or "{}")
        clarification = terms.get("clarification") or {}
        anchor = clarification.get("delivery_message_id") or r.delivery_message_id
        if clarification and not clarification.get("delivery_message_id"):
            return None, "clarification_never_reached_fan"
        delivered = (await s.execute(select(Message.message_id).where(
            Message.account_id == str(account_id), Message.fan_id == int(fan_id), Message.direction == "out",
            *([Message.message_id == anchor] if anchor else []),
            Message.message_id > r.source_message_id, Message.message_id < turn, Message.is_unsent.is_(False)).limit(1))).first()
        if delivered is None:
            return None, "invitation_never_reached_fan"
        # Another fan message between the invitation and this yes means the 'yes' may answer something else.
        between = (await s.execute(select(Message.message_id).where(
            Message.account_id == str(account_id), Message.fan_id == int(fan_id), Message.direction == "in",
            Message.message_id > delivered[0], Message.message_id < turn, Message.is_unsent.is_(False)).limit(1))).first()
        if between is not None:
            return None, "fan_changed_subject"
        unrelated = (await s.execute(select(Message.message_id).where(
            Message.account_id == str(account_id), Message.fan_id == int(fan_id), Message.direction == "out",
            Message.message_id > delivered[0], Message.message_id < turn))).first()
        if unrelated is not None:
            return None, "intervening_creator_message"
        r.state, r.accepted_turn_id, r.resolved_at = "accepted", turn, datetime.utcnow()
        await s.commit()
        return Acceptance(r.invitation_id, tuple(json.loads(r.media_ids_json)), r.source_message_id, turn,
                          json.loads(r.offer_terms_json) if r.offer_terms_json else None), "accepted"


async def bind_delivery(account_id, fan_id, source_message_id, message_id, body):
    """Only confirmed delivery of the specific invitation makes a later yes binding."""
    async with get_session() as s:
        rows = (await s.execute(select(OfSalesInvitation).where(OfSalesInvitation.account_id == str(account_id),
            OfSalesInvitation.fan_id == int(fan_id), OfSalesInvitation.source_message_id <= int(source_message_id),
            OfSalesInvitation.state == OPEN))).scalars().all()
        for r in rows:
            earlier = (await s.execute(select(Message.body).where(Message.account_id == str(account_id),
                Message.fan_id == int(fan_id), Message.direction == "out",
                Message.message_id > int(source_message_id), Message.message_id <= int(message_id))
                .order_by(Message.message_id))).scalars().all()
            words = lambda value: " ".join(re.findall(r"\w+", _text(value).lower()))
            if r.source_message_id == int(source_message_id) and r.invitation_body and words(r.invitation_body) in words(" ".join(earlier)):
                r.delivery_message_id = int(message_id)
            terms = json.loads(r.offer_terms_json or "{}")
            clarification = terms.get("clarification")
            if (clarification and clarification["source_id"] == int(source_message_id)
                    and words(clarification["body"]) in words(" ".join(earlier))):
                clarification["delivery_message_id"] = int(message_id)
                r.offer_terms_json = json.dumps(terms, sort_keys=True)
        await s.commit()


async def settle(account_id: str, fan_id: int) -> None:
    """Close open invitations the fan answered with something other than acceptance: a refusal is 'declined',
    anything else is 'ignored' so the same invitation is not chased."""
    from . import _language
    async with get_session() as s:
        opened = (await s.execute(select(OfSalesInvitation).where(
            OfSalesInvitation.account_id == str(account_id), OfSalesInvitation.fan_id == int(fan_id),
            OfSalesInvitation.state == OPEN))).scalars().all()
        for r in opened:
            reply = (await s.execute(select(Message.body).where(
                Message.account_id == str(account_id), Message.fan_id == int(fan_id), Message.direction == "in",
                Message.message_id > r.source_message_id, Message.is_unsent.is_(False))
                .order_by(Message.message_id.asc()))).scalars().all()
            sent = (await s.execute(select(Message.message_id).where(
                Message.account_id == str(account_id), Message.fan_id == int(fan_id), Message.direction == "out",
                Message.message_id > r.source_message_id).limit(1))).first()
            if sent is None or not reply:
                continue
            after = [b for b in reply]
            if any(short_acceptance(b) for b in after):
                continue
            if len(after) == 1 and ambiguous_answer(after[0]):
                continue  # one item-specific ambiguity can use the bounded clarification path
            r.state = "declined" if any(_language.is_hard_decline(_text(b)) for b in after) else "ignored"
            r.resolved_at = datetime.utcnow()
        await s.commit()


async def history(account_id: str, fan_id: int, media_ids=()) -> dict:
    """Recent invitation state for the planner: do not chase the same ignored/declined invitation."""
    want = {int(m) for m in media_ids}
    try:
        async with get_session() as s:
            rows = (await s.execute(select(OfSalesInvitation).where(
                OfSalesInvitation.account_id == str(account_id), OfSalesInvitation.fan_id == int(fan_id))
                .order_by(OfSalesInvitation.created_at.desc()))).scalars().all()
    except Exception:
        return {"open": 0, "ignored_same_item": False, "declined_same_item": False, "recent_total": 0, "open_matching": []}
    same = [r for r in rows if want and want & set(json.loads(r.media_ids_json))]
    return {"open": sum(r.state == OPEN for r in rows),
            "ignored_same_item": any(r.state == "ignored" for r in same),
            "declined_same_item": any(r.state == "declined" for r in same),
            "recent_total": len(rows),
            "open_matching": [r.invitation_id for r in same if r.state == OPEN]}
