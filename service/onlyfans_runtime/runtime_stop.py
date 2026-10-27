"""The one per-fan AI stop: FASTT's ``skip_list(reason='manual_restrict')``.

Twin's dashboard writes conversation controls through ``Store.control``. FASTT's
engine honours exactly one durable per-fan stop, the ``manual_restrict`` row in
``skip_list`` (``automations/_common.py``). This module makes that row the
*enforced* authority and keeps Twin's ``of_conversation_state`` as its display
projection, written in the SAME transaction as the control change, so there is
no state in which the dashboard says "automated" while the runtime is stopped
(or the reverse) after a command committed.

Rules, all in one place:

* The stop is wanted whenever the conversation is not (``automated`` AND no
  takeover): paused, human_controlled (requested or effective) and no_contact.
  A requested takeover already stops the AI, before any manual-send state is
  exposed; it only becomes ``effective`` once the earlier send is resolved.
* Release deletes ONLY ``manual_restrict``. ``of_restricted`` and ``ladder_stop``
  are stronger decisions: they are never overwritten, and a resume that would
  leave one of them in force is refused rather than displayed as "automated".
  A ``muted_creator`` row replaced by the stop is restored on release when the
  fan is still muted.
* ``resume_after_manual_hours`` in ai_chatter is a timed cooldown that is ANDed
  with the skip_list check; it can only add silence, never lift this row.
* Drift is repaired fail-closed and never auto-resumes: a wanted stop that is
  missing is re-written; a stop found without a Twin command (set by FASTT's own
  restrict route) is shown as ``paused``. Nothing here ever releases a stop.

Account ids: live FASTT tables (messages, fans, skip_list) are keyed by the same
DB id as Twin (``of-<id>``), which is also what ai_chatter queries with (see
native_pilot.scope). The stop row therefore uses the account id unchanged.
``to_fastt_account_id`` only normalises ids for scope matching.
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.models import Fan, SkipList

MANUAL_RESTRICT = "manual_restrict"
MUTED_CREATOR = "muted_creator"
# Decisions stronger than a Twin pause: never overwritten, never released here.
STRONGER = frozenset({"of_restricted", "ladder_stop"})
# Reasons that make ai_chatter skip the fan outright (mirrors HARD_SKIP_REASONS).
HARD = frozenset({MANUAL_RESTRICT, MUTED_CREATOR, *STRONGER})


def to_fastt_account_id(account_id: str) -> str:
    """Twin ``of-176774528`` -> FASTT ``176774528``; any other id is unchanged."""
    tail = account_id[3:] if account_id.startswith("of-") else ""
    return tail if tail.isdecimal() else account_id


def stop_wanted(state) -> bool:
    return state.mode != "automated" or state.takeover_state != "none"


def current(s, account_id, fan_id):
    """The fan's skip_list reason, or None."""
    return s.scalar(select(SkipList.reason).where(
        SkipList.account_id == account_id, SkipList.fan_id == fan_id))


def stronger_hold(s, account_id, fan_id):
    reason = current(s, account_id, fan_id)
    return reason if reason in STRONGER else None


def hold(s, account_id, fan_id, now):
    """Upsert the durable stop; never demote a stronger decision."""
    s.execute(pg_insert(SkipList).values(
        account_id=account_id, fan_id=fan_id, reason=MANUAL_RESTRICT, added_at=now,
    ).on_conflict_do_update(
        index_elements=["account_id", "fan_id"],
        set_={"reason": MANUAL_RESTRICT, "added_at": now},
        where=(SkipList.reason.is_(None) | SkipList.reason.not_in(tuple(STRONGER))),
    ))


def release(s, account_id, fan_id, now):
    """Remove ONLY the manual stop, restoring an auto mute it displaced."""
    fastt = account_id
    s.execute(delete(SkipList).where(SkipList.account_id == fastt, SkipList.fan_id == fan_id,
                                     SkipList.reason == MANUAL_RESTRICT))
    fan = s.get(Fan, (account_id, fan_id))
    if fan is not None and fan.is_muted and current(s, account_id, fan_id) is None:
        s.execute(pg_insert(SkipList).values(account_id=fastt, fan_id=fan_id, reason=MUTED_CREATOR, added_at=now)
                  .on_conflict_do_nothing(index_elements=["account_id", "fan_id"]))


def project(s, account_id, fan_id, state, now):
    """Make the runtime stop match the (already updated) Twin state."""
    if stop_wanted(state):
        hold(s, account_id, fan_id, now)
    else:
        release(s, account_id, fan_id, now)
