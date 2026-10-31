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
from datetime import datetime, timedelta

from . import recovery, runner
from .ledger import Refused, _dt

# An attempt whose payload was never frozen was never authorised to POST (the process died, or freeze failed, between
# begin_attempt and authorize_dispatch). It used to raise "frozen-dispatch-unavailable" every cycle forever.
NO_INTENT_MIN_AGE_S = 600.0
NO_INTENT_RECHECK_S = 300.0


async def _unfrozen(service, row, result, now) -> bool:
    """Settle or park an attempt with no frozen payload. Returns False while backing off (no provider read, no log).

    Not-sent only when the provider history is read completely back past the attempt AND the creator has NO outbound
    message of any kind since: a card sent by any path (including a legacy row) would show up there and block this.
    Otherwise the fan is parked in the existing ``attention`` state and re-checked at most every NO_INTENT_RECHECK_S."""
    from onlyfans_runtime.native_protocol import message
    attempt = row["attempt_id"]
    result["reason"] = "delivery-unresolved:frozen-dispatch-unavailable"
    backoff = service.__dict__.setdefault("_unfrozen_next", {})
    if backoff.get(attempt, now) > now:
        return False
    backoff[attempt] = now + timedelta(seconds=NO_INTENT_RECHECK_S)
    if (now - datetime.fromisoformat(_dt(row["created_at"]))).total_seconds() < NO_INTENT_MIN_AGE_S:
        return True
    probe = {"account_id": service.db, "fan_id": row["fan_id"], "created_at": row["created_at"]}
    history, complete = await asyncio.to_thread(recovery.read_window, service.client, intent=probe)
    owner = int(recovery.AccountScope.parse(service.db).provider_id)
    outbound = any(message(raw, owner_id=owner, fan_id=row["fan_id"], history=True).direction == "out" for raw in history)
    if complete and not outbound:
        await asyncio.to_thread(service.ledger.settle_absent, pilot_id=row["pilot_id"], attempt_id=attempt,
                                account_id=service.db, fan_id=row["fan_id"], now=runner.now())
        result.update(outcome="not_sent", reason="no-frozen-dispatch-no-outbound")
    elif outbound:
        result["reason"] = "delivery-unresolved:frozen-dispatch-outbound-present"
        await asyncio.to_thread(service.ledger.flag_attention, account_id=service.db, fan_id=row["fan_id"], now=runner.now())
    return True


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
            try:
                intent = await asyncio.to_thread(recovery.snapshot, ledger, pilot_id=pilot, attempt_id=attempt,
                                                 account_id=service.db, fan_id=fan)
            except Refused as exc:
                if exc.reason != "frozen-dispatch-unavailable":
                    raise
                if await _unfrozen(service, row, result, now):
                    runner.emit({"event": "delivery-reconcile", **result})
                out.append(result)
                continue
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
