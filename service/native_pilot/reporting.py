"""FASTT-path outcomes into Twin's existing reporting tables (what the dashboard reads).

ONE WRITER PER ROW, decided here:

* ``messages`` (+ ``chats``, ``content_offers``, ``vault_sends``, ``ladder_quote``):
  written by FASTT's own ``write_outbound_attribution`` / ``_record_*`` right after
  the accepted POST. Twin's ``TextAuthority.materialize`` is never run for this path
  (it needs an ``of_text_intent``), so those rows are never written twice and this
  module NEVER writes ``messages``.
* ``of_message_delivery``: written ONLY here (FASTT has no such table), one row per
  accepted provider message id, ``outcome='sent'``, ``origin='ai'``, upserted by its
  primary key and never overwritten, so a repeat call changes nothing.
* ``of_conversation_state``: a send with no proven provider id (delivery unknown, or
  still dispatching when the run ended) sets ``unresolved_attempt_id`` and
  ``requires_attention``. It never creates a ``messages`` row or a delivery row
  (the schema forbids a receipt without a message), and never reports it as sent;
  the dashboard shows it as an unresolved ``unknown`` incident. An error only raises
  ``requires_attention``. Mode, takeover and revisions are left to the control path.
* ``report_attention``: an unsupported fan input (photo, video, gif, tip, paid or locked
  message) the FASTT runner cannot read. Same single write as above: ``requires_attention``
  on ``of_conversation_state`` (a missing control row starts paused, as everywhere). No
  message, receipt or incident is invented and no money is written: a tip/purchase is
  reconciled by the existing ledger paths (``event_transcoder`` tip row, ``transaction_ingest``).
"""
from __future__ import annotations

from datetime import datetime, timezone

DETAIL = "The transport accepted this exact message. This is not a purchase or read receipt."


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def attempt_ref(attempt_id) -> str:
    return f"fastt-pilot-{attempt_id}"


async def _attention(s, account_id, fan_id, *, attempt=None, now):
    from db.models import Fan, OfConversationState
    state = await s.get(OfConversationState, (account_id, fan_id))
    if state is None:
        if await s.get(Fan, (account_id, fan_id)) is None:
            return False
        # Same stance as Store: a missing control row starts paused, with attention.
        state = OfConversationState(account_id=account_id, fan_id=fan_id, mode="paused",
                                    revision=0, input_revision=0, takeover_state="none",
                                    requires_attention=True, unresolved_attempt_id=attempt)
        s.add(state)
        return True
    state.requires_attention = True
    if attempt is not None and state.unresolved_attempt_id is None:
        state.unresolved_attempt_id = attempt
    state.updated_at = now
    return True


async def report_outcomes(*, account_id, fan_id, receipts, run_error=None, now=None) -> dict:
    """``account_id``/``fan_id`` are the DB ids. ``receipts`` are ledger rows with
    ``attempt_id``, ``state`` and ``provider_message_id``. Idempotent."""
    from db.engine import get_session
    from db.models import Message, OfMessageDelivery
    account_id, fan_id, now = str(account_id), int(fan_id), now or _now()
    out = {"recorded": 0, "unchanged": 0, "conflicts": 0, "missing_message": 0, "unknown": 0}
    async with get_session() as s:
        unknown_attempt = None
        attention = bool(run_error)
        for r in sorted(receipts, key=lambda x: int(x["attempt_id"])):
            ref = attempt_ref(r["attempt_id"])
            if r["state"] == "not_sent":
                continue  # certainly never sent (see Ledger.record_not_sent): nothing to attribute, nothing unknown
            if r["state"] != "sent":
                # delivery_unknown, or dispatching when the run ended: never "sent".
                out["unknown"] += 1
                unknown_attempt = unknown_attempt or ref
                continue
            mid = int(r["provider_message_id"])
            message = await s.get(Message, (account_id, fan_id, mid))
            if message is None or message.direction != "out":
                # The accepted send has no FASTT attribution row. Do not invent one.
                out["missing_message"] += 1
                attention = True
                continue
            row = await s.get(OfMessageDelivery, (account_id, fan_id, mid))
            if row is None:
                s.add(OfMessageDelivery(account_id=account_id, fan_id=fan_id, message_id=mid,
                                        outcome="sent", attempt_id=ref, evidence_ref=ref,
                                        detail=DETAIL, origin="ai", recorded_at=now))
                out["recorded"] += 1
            elif (row.outcome, row.attempt_id, row.origin) == ("sent", ref, "ai"):
                out["unchanged"] += 1
            else:
                out["conflicts"] += 1
                attention = True
        if unknown_attempt or attention:
            await s.flush()
            await _attention(s, account_id, fan_id, attempt=unknown_attempt, now=now)
    return out


async def report_attention(*, account_id, fan_id, reason, now=None) -> bool:
    """Flag the fan for a human. ``reason`` (``unsupported-input:<kind>``) rides in the runner
    event; the stored inbound row is the durable evidence. False when the fan row is unknown."""
    from db.engine import get_session
    async with get_session() as s:
        return await _attention(s, str(account_id), int(fan_id), now=now or _now())
