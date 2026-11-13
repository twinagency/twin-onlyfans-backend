"""The ONE conversation-hold policy (R1): who may send what into a conversation right now.

Pure and I/O free. Two readers gather the durable facts and both call ``decide``:

* the worker plane, ``TextAuthority._eligibility`` (SQLAlchemy session, PostgreSQL), which runs at plan time
  (``prepare``), at grant time (``admit``) and immediately before the wire (``consume_dispatch``);
* the FASTT runner plane, ``native_pilot.service.fan_hold_reason`` (qmark connection), which runs before
  admission of every bubble and again inside ``physical_post``.

They are callers of this module, not parallel authorities. The facts they read are the existing
``of_conversation_state`` / ``skip_list`` / ``fan_safety_flags`` rows plus the open operator cases
(``operator_cases``). Nothing here decides an action from message text: the detectors
(``native_pilot.safety_detect``) write holds, this module only honours them.

Brief section 6 table, as implemented:

====================================  ===========================================================================
condition (hold)                      automated / campaign / engagement behaviour (manual: see below)
====================================  ===========================================================================
stop contact (mode ``no_contact`` or  nothing is sent, and a manual courtesy message is refused too; later
open ``stop_contact`` case)           contact needs the audited control with recorded renewed consent
identity / human request              paused (the existing pause-and-flag policy); a human answers under takeover
serious distress, consent concern     all automated sending stops; a human answers under takeover and resolves
hardship with essentials              commercial actions (card, offer, campaign, follow-up) stop; plain chat may go on
material complaint                    unrelated selling stops; plain chat to resolve the complaint may go on
uncertain delivery                    NOTHING sends, manual included, except the unresolved attempt itself
====================================  ===========================================================================

Manual / operator origin is allowed only through the correct takeover state (``human_controlled`` with the
takeover ``none`` or ``effective``) and the existing audited route (``staff_owns_takeover`` stays with the
caller). A hard no-contact restriction and an unresolved delivery are never bypassed by a manual message.

``ignore_brakes``, a teaser, a campaign or a fallback cannot reach this module: it has no parameter that lifts a
hold. The only way a hold ends is the audited control (resume / release / consent) or resolving the operator case.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ORIGINS = ("automated", "campaign", "engagement", "manual")
PROACTIVE = ("campaign", "engagement")

# Hold categories an operator case can carry, and the scope each one has by design.
SCOPE_ALL = "all"
SCOPE_COMMERCIAL = "commercial"
CATEGORY_SCOPE = {
    "stop_contact": SCOPE_ALL,
    "identity": SCOPE_ALL,
    "distress": SCOPE_ALL,
    "consent": SCOPE_ALL,
    "minor": SCOPE_ALL,                 # minor-implying roleplay / stated age under 18: hard stop
    "external_sender": SCOPE_ALL,       # another tool (Infloww, the app) wrote to this chat: one active sender
    "hardship": SCOPE_COMMERCIAL,
    "complaint": SCOPE_COMMERCIAL,
}
# Cases that also forbid resuming automation until a human resolves them (distress, consent, minor and no-contact).
# Identity keeps its existing resume rules (the human answers and hands back).
RESUME_BLOCKING = frozenset({"stop_contact", "distress", "consent", "minor"})

# skip_list reasons that mean "no automation for this fan" (service.native_pilot.service.STOP_REASONS plus ladder_stop,
# which ai_chatter already treats as a hard skip).
SKIP_STOP = frozenset({"manual_restrict", "of_restricted", "muted_creator"})
SKIP_STRONG = frozenset({"of_restricted", "ladder_stop"})

CHAT, COMMERCIAL, MANUAL = "chat", "commercial", "manual"


def action_of(origin: str, catalog_item_id=None) -> str:
    """The action class a send represents. Proactive origins and anything carrying a catalogue item sell."""
    if origin == "manual":
        return MANUAL
    if origin in PROACTIVE or catalog_item_id is not None:
        return COMMERCIAL
    return CHAT


@dataclass(frozen=True)
class Facts:
    """Everything the policy needs to know about one conversation, read by the caller."""
    mode: str | None = None                 # of_conversation_state.mode; None = no control row
    takeover_state: str = "none"
    muted: bool = False
    skip_reason: str | None = None
    unresolved: bool = False                # an unresolved delivery that is NOT the caller's own attempt
    critical_flag: bool = False             # an unresolved critical fan_safety_flag
    # open operator cases as (category, scope); status open/acknowledged only
    cases: frozenset = field(default_factory=frozenset)
    has_fan: bool = True


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None               # fixed code, never message text
    detail: str | None = None               # mode for mode-based denials (the FASTT plane renders "fan-<mode>")

    def __bool__(self):                      # truthy == allowed
        return self.allowed


ALLOW = Decision(True)


def _deny(reason, detail=None):
    return Decision(False, reason, detail)


def decide(facts: Facts, origin: str, action: str | None = None, *, strict: bool = False) -> Decision:
    """Allow or deny one send. ``strict`` (the FASTT all-fans mode) treats a missing control row as "never enabled"."""
    if action is None:
        action = MANUAL if origin == "manual" else CHAT
    state_missing = facts.mode is None
    if not facts.has_fan:
        return _deny("conversation_unavailable")
    if strict and state_missing:
        return _deny("not_enabled")
    proactive_or_ai = origin != "manual"
    if facts.muted:
        return _deny("contact_blocked", "muted")

    # Durable stops the AI planes already honoured in the FASTT runner. They never block the human takeover route,
    # except the platform-level restriction (the creator cannot message that fan at all).
    if proactive_or_ai and facts.skip_reason in SKIP_STOP:
        return _deny("fan_restricted")
    if facts.skip_reason == "of_restricted" and not proactive_or_ai:
        return _deny("fan_restricted")
    if not state_missing:
        if facts.mode in {"paused", "no_contact"}:
            return _deny("contact_blocked", facts.mode)
        if facts.takeover_state == "requested":
            return _deny("takeover_pending")
        if proactive_or_ai and (facts.mode != "automated" or facts.takeover_state != "none"):
            return _deny("owner_changed", facts.mode if facts.mode != "automated" else "takeover")
        if origin == "manual" and (facts.mode != "human_controlled" or facts.takeover_state not in {"none", "effective"}):
            return _deny("human_control_required")
    if facts.unresolved:
        return _deny("unresolved_delivery")

    # Operator cases. A stop-contact case denies everything including a manual courtesy message; the other
    # all-scope cases deny every automated origin and leave the human takeover route open.
    for category, scope in sorted(facts.cases):
        if category == "stop_contact":
            return _deny("case_stop_contact")
    if proactive_or_ai:
        for category, scope in sorted(facts.cases):
            if scope == SCOPE_ALL:
                return _deny("case_" + category)
        if facts.critical_flag:
            return _deny("safety_hold")
        if action == COMMERCIAL:
            for category, scope in sorted(facts.cases):
                if scope == SCOPE_COMMERCIAL:
                    return _deny("case_" + category)
    return ALLOW


def fan_hold_code(decision: Decision) -> str | None:
    """The FASTT plane's historic reason strings, so every existing consumer keeps working."""
    if decision.allowed:
        return None
    if decision.reason == "fan_restricted":
        return "fan-restricted"
    if decision.reason == "not_enabled":
        return "fan-not-enabled"
    if decision.reason == "contact_blocked" and decision.detail:
        return "fan-" + str(decision.detail).replace("_", "-")
    if decision.reason == "owner_changed" and decision.detail:
        return "fan-" + str(decision.detail).replace("_", "-")
    if decision.reason in {"takeover_pending", "human_control_required"}:
        return "fan-takeover"
    return "fan-" + str(decision.reason).replace("_", "-")
