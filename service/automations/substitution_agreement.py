"""F04: a materially different alternative may be dispatched only after the fan agreed to THAT alternative.

Owner: ``of_substitution_agreement`` (one row per proposal). A proposal records the media that would be
sent and the fan message it answered. Agreement requires all of:
  * a later fan message that is a bare affirmation (not a generic old 'yes', not our own caption),
  * our proposal actually reached him (an outbound message sits between the proposal turn and the yes),
  * the media now being sent is a subset of the proposed media,
and is valid for that one agreeing turn only. A 'not exactly X but ...' caption is not agreement.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import select

from db.engine import get_session
from db.models import Message
from db.onlyfans_substitution_models import OfSubstitutionAgreement
from ._sell_signal import is_affirmation

_TAGS = re.compile(r"<[^>]+>")


def _text(body) -> str:
    return " ".join(_TAGS.sub(" ", str(body or "")).split())


async def _latest_inbound(s, account_id: str, fan_id: int):
    return (await s.execute(select(Message.message_id, Message.body).where(
        Message.account_id == str(account_id), Message.fan_id == int(fan_id), Message.direction == "in")
        .order_by(Message.message_id.desc()).limit(1))).first()


async def propose(account_id: str, fan_id: int, media_ids, *, subject: str | None = None) -> int:
    """Record the alternative about to be offered, against the current turn. Returns the turn id."""
    ids = sorted({int(m) for m in media_ids})
    async with get_session() as s:
        latest = await _latest_inbound(s, account_id, fan_id)
        turn = int(latest[0]) if latest else 0
        row = await s.get(OfSubstitutionAgreement, (str(account_id), int(fan_id), turn))
        if row is None:
            s.add(OfSubstitutionAgreement(account_id=str(account_id), fan_id=int(fan_id), proposed_turn_id=turn,
                  proposed_media_json=json.dumps(ids), requested_subject=(subject or "")[:200] or None,
                  state="proposed", created_at=datetime.utcnow()))
        elif row.state == "proposed":
            row.proposed_media_json = json.dumps(ids)
        await s.commit()
    return turn


async def is_agreed(account_id: str, fan_id: int, media_ids) -> bool:
    want = {int(m) for m in media_ids}
    if not want:
        return False
    async with get_session() as s:
        latest = await _latest_inbound(s, account_id, fan_id)
        if latest is None:
            return False
        turn, body = int(latest[0]), latest[1]
        rows = (await s.execute(select(OfSubstitutionAgreement).where(
            OfSubstitutionAgreement.account_id == str(account_id),
            OfSubstitutionAgreement.fan_id == int(fan_id)))).scalars().all()
        for row in rows:
            if not want <= set(json.loads(row.proposed_media_json)):
                continue                                    # agreement is tied to THIS alternative
            if row.state == "agreed" and row.agreed_turn_id == turn:
                return True
            if (row.state == "proposed" and row.proposed_turn_id < turn and is_affirmation(_text(body))):
                delivered = (await s.execute(select(Message.message_id).where(
                    Message.account_id == str(account_id), Message.fan_id == int(fan_id), Message.direction == "out",
                    Message.message_id > row.proposed_turn_id, Message.message_id < turn).limit(1))).first()
                if delivered is None:
                    continue                                # the proposal never reached him
                row.state, row.agreed_turn_id, row.agreed_at = "agreed", turn, datetime.utcnow()
                await s.commit()
                return True
        return False
