"""Record and report R5 decisions (observability only; never read to authorise a send).

One row per (account, fan, source message, stage). Excluded and blocked turns are rows too, with their reason, so
report denominators include them. First-offer and repeat-offer pacing are reported separately; grouped turns and raw
bubbles are separate columns and are never added together. Invitations, paid cards, delivered cards and confirmed
purchases are four different counts.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select, func

from db.engine import get_session
from db.onlyfans_sales_models import OfSalesDecision, OfSalesInvitation
from .sales_action import ActionDecision, Counts


async def record(account_id: str, fan_id: int, source_message_id: int, decision: ActionDecision, *,
                 counts: Counts | None = None, evidence: dict | None = None, stage: str = "decide") -> bool:
    """Idempotent per turn+stage. Returns True when a new row was written. Never raises into the reply path."""
    try:
        async with get_session() as s:
            exists = (await s.execute(select(OfSalesDecision.id).where(
                OfSalesDecision.account_id == str(account_id), OfSalesDecision.fan_id == int(fan_id),
                OfSalesDecision.source_message_id == int(source_message_id), OfSalesDecision.stage == stage))).first()
            if exists:
                return False
            p = decision.pacing
            s.add(OfSalesDecision(account_id=str(account_id), fan_id=int(fan_id), source_message_id=int(source_message_id),
                stage=stage, mode=decision.settings.get("mode", "live"), action=decision.action,
                approach=decision.approach, outcome=decision.outcome, reason=decision.reason[:64],
                settings_json=json.dumps({**decision.settings, **({"phase": p.phase, "position": p.position, "range": [p.lo, p.hi]} if p else {})}, sort_keys=True),
                evidence_json=json.dumps(evidence or {}, sort_keys=True, default=str)[:4000],
                grouped_turns=counts.grouped_turns if counts else (p.grouped_turns if p else None),
                raw_bubbles=counts.raw_bubbles if counts else None,
                invitation_id=decision.invitation_id, created_at=datetime.utcnow()))
            await s.commit()
            return True
    except Exception:
        return False


async def report(account_id: str, since: datetime | None = None) -> dict:
    """Denominators include excluded/blocked turns. First and repeat pacing are separate sections."""
    async with get_session() as s:
        q = select(OfSalesDecision).where(OfSalesDecision.account_id == str(account_id), OfSalesDecision.stage == "decide")
        if since:
            q = q.where(OfSalesDecision.created_at >= since)
        rows = (await s.execute(q)).scalars().all()
        inv = (await s.execute(select(OfSalesInvitation.state, func.count()).where(
            OfSalesInvitation.account_id == str(account_id)).group_by(OfSalesInvitation.state))).all()
    out = {"turns_evaluated": len(rows), "by_outcome": {}, "by_action": {}, "no_offer_reasons": {},
           "pacing": {"first": {"turns": 0, "cards": 0, "invitations": 0, "grouped_turns": [], "raw_bubbles": []},
                      "repeat": {"turns": 0, "cards": 0, "invitations": 0, "grouped_turns": [], "raw_bubbles": []}},
           "invitations_by_state": {k: v for k, v in inv},
           "shadow_turns": sum(r.mode == "shadow" for r in rows)}
    for r in rows:
        out["by_outcome"][r.outcome] = out["by_outcome"].get(r.outcome, 0) + 1
        out["by_action"][r.action] = out["by_action"].get(r.action, 0) + 1
        card = r.action in ("direct_offer", "proactive_offer") and r.outcome == "acted"
        if not card:
            out["no_offer_reasons"][r.reason] = out["no_offer_reasons"].get(r.reason, 0) + 1
        phase = (json.loads(r.settings_json or "{}").get("phase") or "first")
        sec = out["pacing"][phase if phase in ("first", "repeat") else "first"]
        sec["turns"] += 1
        sec["cards"] += int(card)
        sec["invitations"] += int(r.action == "invite" and r.outcome == "acted")
        if r.grouped_turns is not None:
            sec["grouped_turns"].append(r.grouped_turns)
        if r.raw_bubbles is not None:
            sec["raw_bubbles"].append(r.raw_bubbles)
    return out
