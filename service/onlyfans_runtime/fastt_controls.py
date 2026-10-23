"""One control authority for the FASTT runtime, and who counts as human.

`of_conversation_state` (written only by `Store.control`) stays the single
writable truth about whether AI may talk to a fan. FASTT's own stop mechanism is
`skip_list(reason='manual_restrict')`, honoured by every FASTT sender. This
module makes the second a *projection* of the first, written in the SAME
transaction as the control change, so the two cannot disagree or auto-resume one
another:

  * pause / takeover / no-contact (anything but automated+no takeover)
      -> upsert skip_list(manual_restrict); never demotes a stronger row;
  * explicit resume / hand-back (automated, no takeover)
      -> delete ONLY the manual_restrict row; of_restricted, muted_creator and
         ladder_stop rows are never touched; the state machine itself refuses a
         resume out of no_contact without recorded consent.

The restrict row has no expiry. `resume_after_manual_hours` in ai_chatter is a
candidate-selection cooldown after a recent human message; it never deletes
anything, so an explicit stop outlives it.

An unresolved previous send is NOT projected (it clears itself in many places);
`fastt_may_act` reads it from the same authority at the moment of asking.

Human attribution: Twin's canonical rows do not carry FASTT's
`sent_by_employee_id`. An outbound row with no automation kind and no AI
delivery evidence is "human": either a staff send made through Twin (manual
text intent, actor recorded) or an external send (OnlyFans app or an unknown
operator). The latter is "human, unattributed" and is never presented as a
named chatter.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, delete, exists, or_, select

from db.models import Fan, Message, OfConversationState, OfMessageDelivery, SkipList
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_models import OfRuntime
from db.upsert import dialect_insert

# Same literals as automations/_common.py (a test pins them). Kept here so the
# store can project without importing the FASTT engine.
MANUAL_RESTRICT = "manual_restrict"
OF_RESTRICTED = "of_restricted"
MUTED_CREATOR = "muted_creator"
LADDER_STOP = "ladder_stop"
HARD_SKIP_REASONS = frozenset({MANUAL_RESTRICT, OF_RESTRICTED, MUTED_CREATOR, LADDER_STOP})
OPERATOR_STOP_REASONS = frozenset({MANUAL_RESTRICT, OF_RESTRICTED, MUTED_CREATOR})


def _now():
    from .store import utcnow
    return utcnow()


# ── control projection ────────────────────────────────────────────────────────

def stop_wanted(state) -> bool:
    """True unless the conversation is automated with no takeover in progress."""
    return state.mode != "automated" or state.takeover_state != "none"


def project_stop(s, account_id, fan_id, state, *, now=None) -> str:
    """Write the FASTT stop that mirrors `state`. Call inside the control transaction.

    Returns "restricted", "released" or "unchanged" (a release with nothing to
    release). Never touches a non-manual row.
    """
    now = now or _now()
    if stop_wanted(state):
        s.execute(
            dialect_insert(SkipList, s)
            .values(account_id=account_id, fan_id=fan_id, reason=MANUAL_RESTRICT, added_at=now)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                set_={"reason": MANUAL_RESTRICT, "added_at": now},
                # A hard row already blocks FASTT and belongs to someone else
                # (OnlyFans restriction, muted creator): leave it as it is.
                where=or_(SkipList.reason.is_(None), SkipList.reason.not_in(sorted(HARD_SKIP_REASONS))),
            )
        )
        return "restricted"
    result = s.execute(delete(SkipList).where(
        SkipList.account_id == account_id, SkipList.fan_id == fan_id, SkipList.reason == MANUAL_RESTRICT))
    return "released" if result.rowcount else "unchanged"


def project_account(s, account_id, *, now=None) -> int:
    """Re-assert the projection for every fan with a control row (cutover/repair).

    Fans paused before this module existed have a state but no skip row. Returns
    the number of fans whose skip row changed.
    """
    changed = 0
    for state in s.scalars(select(OfConversationState).where(OfConversationState.account_id == account_id)).all():
        reason = select(SkipList.reason).where(SkipList.account_id == account_id, SkipList.fan_id == state.fan_id)
        before = s.scalar(reason)
        project_stop(s, account_id, state.fan_id, state, now=now)
        s.flush()
        s.expire_all()
        changed += before != s.scalar(reason)
    return changed


# ── the one predicate the runner and the send gate ask ───────────────────────

def stop_reason(s, account_id, fan_id):
    """None when FASTT may act on this fan right now, else a short reason.

    Reads the authority (`of_conversation_state`, unresolved evidence, runtime,
    mute) and the FASTT stop rows. A manual_restrict row while the state says
    automated is projection drift and fails closed ("projection_drift"); repair
    with `project_stop`.
    """
    from .store import Store
    runtime = s.get(OfRuntime, account_id)
    if runtime is None or not runtime.enabled:
        return "runtime_disabled"
    state = s.get(OfConversationState, (account_id, fan_id))
    if state is None:
        return "no_control_state"
    if state.mode != "automated":
        return state.mode
    if state.takeover_state != "none":
        return "takeover_" + state.takeover_state
    if Store._unresolved(s, account_id, fan_id, state):
        return "unresolved_delivery"
    fan = s.get(Fan, (account_id, fan_id))
    if fan is None or fan.is_muted:
        return "fan_muted"
    row = s.scalar(select(SkipList.reason).where(SkipList.account_id == account_id, SkipList.fan_id == fan_id))
    if row == MANUAL_RESTRICT:
        return "projection_drift"
    if row in OPERATOR_STOP_REASONS:
        return row
    return None


def fastt_may_act(session, account_id, fan_id) -> bool:
    """May the FASTT runtime reply to / send to this fan right now? (sync Session)"""
    return stop_reason(session, str(account_id), int(fan_id)) is None


# ── human attribution ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HumanSend:
    kind: str            # "staff" (sent through Twin) | "external_unattributed"
    actor: str | None    # authenticated staff identity for "staff", else None


def human_attribution(s, account_id, fan_id, message_id) -> HumanSend | None:
    """Classify one outbound message; None when it is not a human 1:1 send."""
    message = s.get(Message, (account_id, fan_id, message_id))
    if message is None or message.direction != "out" or message.automation_kind or message.mass_run_id is not None:
        return None
    intent = s.scalar(select(OfTextIntent).where(
        OfTextIntent.account_id == account_id, OfTextIntent.provider_message_id == message_id))
    receipt = s.get(OfMessageDelivery, (account_id, fan_id, message_id))
    if (intent is not None and intent.origin != "manual") or (receipt is not None and receipt.origin == "ai"):
        return None
    if intent is not None and intent.origin == "manual":
        return HumanSend("staff", intent.actor)
    if receipt is not None and receipt.origin == "human":
        return HumanSend("staff", None)
    return HumanSend("external_unattributed", None)


def ai_message_ids(account_id, fan_ids):
    """SELECT of provider message ids Twin knows were sent by an AI origin.

    Used to take Twin's own AI sends out of the "human" bucket when their row
    carries no automation kind (for instance poll ingestion won the race
    against receipt projection).
    """
    fans = list(fan_ids)
    intents = select(OfTextIntent.provider_message_id).where(
        OfTextIntent.account_id == account_id, OfTextIntent.fan_id.in_(fans),
        OfTextIntent.provider_message_id.is_not(None), OfTextIntent.origin != "manual")
    receipts = select(OfMessageDelivery.message_id).where(
        OfMessageDelivery.account_id == account_id, OfMessageDelivery.fan_id.in_(fans),
        OfMessageDelivery.origin == "ai")
    return intents.union(receipts)


def human_out_clause():
    """SQL over `messages`: an outbound 1:1 row Twin cannot attribute to an AI.

    Staff sends made through Twin and external sends both match. Combine with
    the caller's own account/fan/time filters.
    """
    ai_intent = exists().where(
        OfTextIntent.account_id == Message.account_id, OfTextIntent.provider_message_id == Message.message_id,
        OfTextIntent.origin != "manual")
    ai_receipt = exists().where(
        OfMessageDelivery.account_id == Message.account_id, OfMessageDelivery.fan_id == Message.fan_id,
        OfMessageDelivery.message_id == Message.message_id, OfMessageDelivery.origin == "ai")
    return and_(Message.direction == "out", Message.automation_kind.is_(None),
                Message.mass_run_id.is_(None), ~ai_intent, ~ai_receipt)
