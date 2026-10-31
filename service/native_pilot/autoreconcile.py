"""Settle admitted sends whose outcome is unknown, from the provider's own history. Never sends, never resends.

Two situations leave an admitted send unsettled: the process died between admission and the answer
(``dispatching``) and a timeout / unreadable reply (``delivery_unknown``). The ledger refuses to answer that fan
again until it is settled. Settling uses ONLY the provider's history, read completely and back past the attempt:

* exactly one message in that window matches the frozen payload (recipient, text, media, price, time window):
  the send is PROVEN delivered. The receipt and attempt become ``sent`` (with the proof as evidence), the fan is
  not answered a second time, and the fan resumes.
* nothing matches: that is not proof of non-delivery. By default the attempt stays unresolved, the fan is
  surfaced as ``delivery-unresolved`` with its age, and an operator decides. ``FASTT_AUTO_RESOLVE_ABSENT_AFTER_S``
  (default 0 = off, minimum 120 when set) opts in to treating a complete window with no match, read that long
  after the attempt, as not delivered.
* more than one match, a bad window or a read error: left unresolved, reason surfaced.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from . import recovery, runner
from .ledger import Refused, _dt


async def reconcile_unresolved(service) -> list[dict]:
    """Run one pass for the service's account. Returns one result dict per unresolved attempt looked at."""
    tuning, ledger = service.tuning, service.ledger
    if not tuning.auto_reconcile or service.client is None:
        return []
    now = runner.now()
    rows = await asyncio.to_thread(ledger.unresolved_attempts, account_id=service.db, now=now, grace_s=tuning.reconcile_wait_s)
    out = []
    for row in rows[:5]:
        fan, attempt, pilot = row["fan_id"], row["attempt_id"], row["pilot_id"]
        result = {"fan_id": fan, "attempt_id": attempt, "outcome": "unresolved", "reason": None}
        try:
            intent = await asyncio.to_thread(recovery.snapshot, ledger, pilot_id=pilot, attempt_id=attempt,
                                             account_id=service.db, fan_id=fan)
            history, complete = await asyncio.to_thread(recovery.read_window, service.client, intent=intent)
            matches = []
            for raw in history:
                try:
                    matches.append((recovery._matches(raw, intent), raw))
                except Exception:
                    continue
            if len(matches) == 1 and complete:
                event, raw = matches[0]
                proof = recovery.proof_hash(raw, intent)
                changed = await asyncio.to_thread(ledger.settle_by_proof, pilot_id=pilot, attempt_id=attempt,
                                                  account_id=service.db, fan_id=fan, provider_message_id=event.message_id,
                                                  raw=raw, evidence_hash=proof, now=runner.now())
                result.update(outcome="sent", changed=changed, provider_message_id=event.message_id)
                if ledger.pg:
                    try:
                        await recovery.project(ledger, pilot_id=pilot, attempt_id=attempt, account_id=service.db, fan_id=fan)
                        from .reporting import report_outcomes
                        await report_outcomes(account_id=service.db, fan_id=fan, receipts=ledger.receipts(pilot_id=pilot))
                    except Exception as exc:
                        result["projection"] = "failed:" + type(exc).__name__
            elif len(matches) > 1:
                result["reason"] = "delivery-unresolved-ambiguous"
            elif complete and tuning.absent_after_s and \
                    (now - datetime.fromisoformat(_dt(row["created_at"]))).total_seconds() >= tuning.absent_after_s:
                await asyncio.to_thread(ledger.settle_absent, pilot_id=pilot, attempt_id=attempt, account_id=service.db,
                                        fan_id=fan, now=runner.now())
                result.update(outcome="not_sent", reason="absent-in-complete-history")
            else:
                result["reason"] = "delivery-unresolved-awaiting-proof"
        except Refused as exc:
            result["reason"] = "delivery-unresolved:" + exc.reason
        except Exception as exc:
            result["reason"] = "delivery-unresolved:read-" + type(exc).__name__
        runner.emit({"event": "delivery-reconcile", **result})
        out.append(result)
    return out
