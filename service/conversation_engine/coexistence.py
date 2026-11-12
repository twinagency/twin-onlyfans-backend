"""Step 5.1: staff coexistence for ONE commercial account. A reader of the scope contract plus the small helpers its callers share.

The setting (`coexistence.mode == 'coexist'` in the account's own commercial.v1 scope row, see scope_contract) only changes what an
ordinary creator-account send that this engine did not make MEANS: it is recorded in context and attributed to the outside, it does
not request a human takeover of that fan, and it withdraws every unsent draft of that fan so the next plan starts from the current
conversation. Nothing else is relaxed. Explicit takeover, fan stop/no-contact, unresolved delivery, freshness, duplicate protection,
authentication and every unknown, malformed, conflicting or financial event keep their holds, and the hold on one fan or creator
never reaches another.

What an outside tool does is NOT under our locks. An outside reply can reach the fan while our own wire is in flight, or just
after it, and nothing in this process can prevent that; the engine only detects it afterwards (an unresolved send of ours keeps the
outside evidence pending instead of being classified) and never claims exclusive control of the chat.

The legacy two-fan trial exception (pilot_scope.trial_takeover_exception) is a separate, scope_id 1 only mechanism and is not
generalised here.
"""
from sqlalchemy import select

from onlyfans_runtime.store import Refused

OUTSIDE_RECORDED = 'outside_activity_recorded_coexistence'
QUEUED_RECORDED = 'queued_send_outside_context_recorded'
INVALIDATION = 'outside_activity'          # the approval invalidation reason the scheduler replans from
ORIGIN_OUTSIDE = 'outside_engine'          # the attribution a history row carries when no engine/operator intent claims it


def enabled(s, runtime, fan=None):
    """True only for THIS account's validated commercial scope that explicitly says 'coexist' (and, when `fan` is given, only for a
    fan in its audience). A damaged, legacy, preparation or missing scope is never coexistence: it keeps the takeover rule."""
    from . import scope_contract as sc
    from .pilot_scope import current
    if runtime is None:
        return False
    try:
        row, config = current(s, runtime)
    except Refused:
        return False
    if row is None or row.scope_id == 1 or not sc.is_commercial(config) or sc.coexistence_mode(config) != sc.COEXISTENCE_COEXIST:
        return False
    return fan is None or fan in config['fans']


def unresolved_own_send(s, runtime, fan):
    """Our own send to this fan is in flight or of unknown outcome: outside evidence cannot be classified against it yet."""
    from db.onlyfans_delivery_models import OfTextIntent
    return s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == runtime.account_id,
        OfTextIntent.binding_id == runtime.binding_id, OfTextIntent.fan_id == fan,
        OfTextIntent.status.in_(('dispatching', 'unknown'))).limit(1)) is not None


def withdraw_unsent(s, account, fan):
    """Freshness for unsent work: every live approval of this fan is invalidated with the reason the scheduler replans from.

    Unsent decisions are cancelled by the existing retire/regenerate path (never here), queued-but-not-wired intents by the existing
    pre-wire guard, and nothing that reached the wire is touched. Returns the number of approvals withdrawn."""
    from .approval import invalidate_fan_in_session
    return invalidate_fan_in_session(s, account, fan, INVALIDATION)


def tag_outside_history(s, runtime, fan, history):
    """Attribute each outgoing history row of a COEXISTENCE account honestly. A row that no engine or operator intent of this
    account claims was sent outside this system and its sender is not established: it carries origin 'outside_engine'.
    Rows of other accounts are untouched (their snapshots keep their exact bytes)."""
    if not history or not enabled(s, runtime, fan):
        return history
    from db.onlyfans_delivery_models import OfTextIntent
    ids = [row['id'] for row in history if row.get('direction') == 'out']
    if not ids:
        return history
    ours = set(s.scalars(select(OfTextIntent.provider_message_id).where(OfTextIntent.account_id == runtime.account_id,
        OfTextIntent.binding_id == runtime.binding_id, OfTextIntent.fan_id == fan,
        OfTextIntent.provider_message_id.in_(ids))))
    for row in history:
        if row.get('direction') == 'out' and row['id'] not in ours:
            row['origin'] = ORIGIN_OUTSIDE
    return history
