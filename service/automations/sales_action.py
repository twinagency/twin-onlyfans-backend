"""ONE explicit per-turn action decision (R5). Pure: no I/O, no model call, no send.

The existing lanes (SellLane, ContextualAuthority, content_resolver, pack_sender, conversation policy, operator
cases) stay the SUPPLIERS of facts and the only senders. This module combines their signals into a single typed
result so that every eligible turn leaves exactly one observable decision, including the reason for not offering.

Commercial controls (initiative, first/repeat reference pacing) only weight the choice among actions the evidence
already supports. Reaching or passing a reference range is a reassessment, never a trigger: no action here is chosen
because of silence, elapsed time or a message count alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from onlyfans_runtime.commercial_controls import Controls

# actions
ANSWER, CLARIFY = "answer", "clarify"
DIRECT_OFFER, PROACTIVE_OFFER = "direct_offer", "proactive_offer"
INVITE, DEVELOP = "invite", "develop"
ALTERNATIVE, SUPPORT, HANDOVER = "unavailable_alternative", "support", "handover_stop"
ACTIONS = (ANSWER, CLARIFY, DIRECT_OFFER, PROACTIVE_OFFER, INVITE, DEVELOP, ALTERNATIVE, SUPPORT, HANDOVER)
CARD_ACTIONS = (DIRECT_OFFER, PROACTIVE_OFFER)
# outcomes
ACTED, EVALUATED, EXCLUDED, BLOCKED = "acted", "evaluated", "excluded", "blocked"
# approaches (the three the brief names, plus none)
APPROACH = {DIRECT_OFFER: "offer", PROACTIVE_OFFER: "offer", INVITE: "invite", DEVELOP: "develop"}

EVIDENCE = ("none", "background", "current_interest")


@dataclass(frozen=True)
class Pacing:
    ref: int
    phase: str            # first | repeat
    lo: int
    hi: int
    position: str         # before | within | beyond
    pressure: int         # 0..2, a weight on the choice, never a trigger
    grouped_turns: int


def pacing(controls: Controls, grouped_turns: int, *, prior_card: bool, raw_bubbles: int | None = None) -> Pacing:
    """Use independent, explicitly configured bubble/reply windows; never derive one unit from the other."""
    ref = controls.repeat_offer_ref if prior_card else controls.first_offer_ref
    lo, hi = controls.repeat_window if prior_card else controls.first_window
    # First-offer observations are bubbles. Repeat references are grouped replies.
    # Unknown bubble counts supply no pressure; never convert turns into bubbles.
    units = grouped_turns if prior_card else (raw_bubbles if raw_bubbles is not None else 0)
    position = "before" if units < lo else ("within" if units <= hi else "beyond")
    base = {"before": 0, "within": 1, "beyond": 2}[position]
    shift = {"lower": -1, "standard": 0, "active": 1}[controls.initiative]
    pressure = max(0, min(2, base + shift)) if position != "before" else (1 if controls.initiative == "active" and units >= max(1, lo - 1) else 0)
    return Pacing(ref, "repeat" if prior_card else "first", lo, hi, position, pressure, grouped_turns)


@dataclass(frozen=True)
class ActionInputs:
    mode: str = "live"                       # off | shadow | live
    controls: Controls = field(default_factory=Controls)
    grouped_turns: int = 0
    prior_card: bool = False
    raw_bubbles: int = 0
    # safety / ownership (from the shared conversation policy and the R1 case model; never re-decided here)
    hold: str | None = None                  # stop_contact, distress, takeover, identity, ...
    support: str | None = None               # complaint, delivery_issue, fulfilment_question, open_custom
    # sale suppliers
    direct_request: bool = False
    request_supported: bool = True           # inside what the creator actually offers (customs/live are not)
    matched: bool = False                    # verified ready content matches the request / interest
    ready_items: int = 0
    readiness_missing: tuple = ()
    purchase_coverage_fresh: bool = True
    transport_uncertain: bool = False
    identical_unresolved_card: bool = False
    budget_blocked: bool = False
    declined_recently: bool = False          # a genuine refusal of what was just shown
    evidence: str = "none"                   # fan-side evidence level; the creator's own suggestion is NEVER evidence
    accepted_invitation: str | None = None   # invitation id a short affirmation unambiguously accepted
    acceptance_revalidated: bool = True      # availability, quote and delivery eligibility rechecked
    alternative: str = "none"                # none | proposed | agreed
    recent_ignored_same_item: bool = False


@dataclass(frozen=True)
class ActionDecision:
    action: str
    outcome: str
    reason: str
    approach: str = "none"
    pacing: Pacing | None = None
    settings: dict = field(default_factory=dict)
    invitation_id: str | None = None

    @property
    def sends_card(self) -> bool:
        return self.action in CARD_ACTIONS and self.outcome == ACTED


def _d(i, action, outcome, reason, pace, **kw):
    return ActionDecision(action, outcome, reason, kw.pop("approach", APPROACH.get(action, "none")), pace,
                          {**i.controls.as_dict(), "mode": i.mode}, **kw)


def decide(i: ActionInputs) -> ActionDecision:
    pace = pacing(i.controls, i.grouped_turns, prior_card=i.prior_card, raw_bubbles=i.raw_bubbles)
    if i.mode == "off":
        return _d(i, ANSWER, EXCLUDED, "disabled", pace)
    # 1. stops, identity, takeover: nothing commercial, whatever the settings say.
    if i.hold:
        return _d(i, HANDOVER, BLOCKED, f"hold:{i.hold}", pace)
    # 2. support: complaints, delivery problems, open customs. Resolution, never a sale.
    if i.support:
        return _d(i, SUPPORT, BLOCKED, f"support:{i.support}", pace)
    # 3. state that makes any commercial step unsafe right now.
    if i.transport_uncertain:
        return _d(i, ANSWER, BLOCKED, "transport_uncertain", pace)
    if not i.purchase_coverage_fresh:
        return _d(i, ANSWER, BLOCKED, "missing_purchase_coverage", pace)
    if i.budget_blocked:
        return _d(i, ANSWER, EXCLUDED, "budget_limited", pace)
    # 4. an accepted invitation presents THAT offer (never an auto-charge, never a re-proof of interest).
    if i.accepted_invitation:
        if not i.acceptance_revalidated:
            return _d(i, ANSWER, EXCLUDED, "acceptance_not_revalidated", pace, invitation_id=i.accepted_invitation)
        if i.identical_unresolved_card:
            return _d(i, ANSWER, BLOCKED, "identical_card_unresolved", pace, invitation_id=i.accepted_invitation)
        return _d(i, DIRECT_OFFER, ACTED, "invitation_accepted", pace, approach="offer", invitation_id=i.accepted_invitation)
    # 5. a direct request never waits for warm-up or pacing.
    if i.direct_request:
        if not i.request_supported:
            return _d(i, CLARIFY, EXCLUDED, "request_not_supported", pace)
        if i.identical_unresolved_card:
            return _d(i, ANSWER, BLOCKED, "identical_card_unresolved", pace)
        if i.matched and i.ready_items:
            return _d(i, DIRECT_OFFER, ACTED, "direct_request_ready", pace)
        if i.alternative == "agreed" and i.ready_items:
            return _d(i, DIRECT_OFFER, ACTED, "agreed_alternative", pace)
        if i.ready_items and i.alternative == "none":
            return _d(i, ALTERNATIVE, ACTED, "no_exact_match_alternative_proposed", pace)
        if i.alternative == "proposed":
            return _d(i, CLARIFY, EXCLUDED, "alternative_awaiting_agreement", pace)
        return _d(i, CLARIFY, EXCLUDED, "not_ready" if i.readiness_missing else "no_match", pace)
    # 6. proactive: only from fan-side evidence, weighted by the controls. Nothing is chosen by count alone.
    if not i.ready_items:
        return _d(i, ANSWER, EXCLUDED, "not_ready" if i.readiness_missing else "no_match", pace)
    if i.declined_recently:
        return _d(i, ANSWER, EXCLUDED, "recent_refusal", pace)
    if i.recent_ignored_same_item and i.evidence != "current_interest":
        return _d(i, ANSWER, EXCLUDED, "ignored_invitation_not_repeated", pace)
    ev, p, init = i.evidence, pace.pressure, i.controls.initiative
    if ev == "current_interest":
        if i.identical_unresolved_card:
            return _d(i, ANSWER, BLOCKED, "identical_card_unresolved", pace)
        if init == "lower" and p == 0:
            return _d(i, INVITE, ACTED, "current_interest_gentle", pace)
        return _d(i, PROACTIVE_OFFER, ACTED, "current_interest", pace)
    if ev == "background":
        if init == "active" or p >= 1 and init != "lower":
            return _d(i, INVITE, ACTED, "background_interest_invite", pace)
        if p >= 1:
            return _d(i, DEVELOP, ACTED, "background_interest_develop", pace)
        return _d(i, ANSWER, EVALUATED, "insufficient_evidence", pace)
    if p >= 2 and init != "lower" and pace.position != "beyond":
        return _d(i, DEVELOP, ACTED, "beyond_range_develop_topic", pace)
    if pace.position == "beyond":
        return _d(i, ANSWER, EVALUATED, "no_suitable_opportunity_beyond_range", pace)
    return _d(i, ANSWER, EVALUATED, "insufficient_evidence", pace)


# ── conversation counting: grouped turns and raw bubbles are recorded separately and never interchanged ─────────
@dataclass(frozen=True)
class Counts:
    grouped_turns: int
    raw_bubbles: int
    prior_card: bool


_FILLER_MIN_CHARS = 3


def count_since_last_card(rows) -> Counts:
    """rows: chronological (direction, is_priced_card, body). Counts creator output since the most recent paid card
    (the card itself excluded): a grouped turn is one run of creator bubbles between fan messages; raw bubbles is the
    plain bubble count. A bubble with no word characters (emoji only) is a bubble but not a substantive turn."""
    grouped = raw = 0
    in_run = False
    prior = False
    for direction, card, body in rows:
        if direction == "out":
            if card:
                grouped = raw = 0
                in_run = False
                prior = True
                continue
            raw += 1
            # Filler fails quality elsewhere, but cannot freeze this allowance.
            if not in_run:
                grouped += 1
            in_run = True
        else:
            in_run = False
    return Counts(grouped, raw, prior)
