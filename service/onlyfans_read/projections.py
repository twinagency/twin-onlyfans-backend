"""Relay rows -> the dashboard's read models.

Every mapping decision that could over-claim is made here, once, with the
reason beside it. The three worth reading before changing anything:

1. **An absent delivery row is not "no delivery".** What it means depends on
   the direction of the message, and the rule is stated in
   `delivery_for_message` rather than defaulted to one value. This is the field
   an operator acts on, so a plausible default is the most expensive kind of
   wrong.

2. **Freshness is never `now`.** The anchor is the newest activity this relay
   has actually observed for the account, or the binding's own creation when it
   has observed nothing. A read that stamped itself with the current time would
   always look fresh, which is the one thing the field exists to prevent.

3. **A value outside the dashboard's vocabulary is a bug, not a value.** Each
   mapping is checked against `contract`'s tuples and falls back to the
   conservative member of the set, with the raw value kept out of the payload.
   A stored `mode` of "humn_controlled" must not reach a UI that has five
   known states.

Nothing here writes, and nothing here is awaited — this module is pure so the
offline suite can assert every mapping directly.
"""

from __future__ import annotations

from datetime import datetime

from .contract import (
    CAPABILITY_AVAILABLE,
    CAPABILITY_NOT_CONFIGURED,
    DELIVERY_OUTCOMES,
    MONEY_CURRENCY,
    UNRESOLVED_OUTCOMES,
    age_seconds,
    conversation_id_for,
    iso,
    utc_now,
)
from .repo import (
    BindingRow,
    ConversationRow,
    DeliveryRow,
    FactRow,
    MessageRow,
    OwnedRow,
    StateRow,
)

#: How old an account projection may be before it must render as stale. An
#: initial product policy, not a platform rule: the dashboard shows "Last known
#: <age> ago (this view may be out of date)" rather than "Updated just now".
FRESHNESS_MAX_AGE_SECONDS = 1800

#: The label a fan is shown under when the relay has no name for them.
_UNNAMED_FAN = "Unnamed fan"


def freshness(as_of: datetime | None, *, now: datetime | None = None) -> dict:
    """The freshness triple, with the age computed here rather than in the UI."""
    reference = now or utc_now()
    age = age_seconds(as_of, now=reference)
    return {
        "asOf": iso(as_of) or iso(reference),
        "maxAgeSeconds": FRESHNESS_MAX_AGE_SECONDS,
        "stale": age is None or age > FRESHNESS_MAX_AGE_SECONDS,
    }


def connection_health(binding: BindingRow) -> tuple[str, str | None]:
    """Connection state and its human reason.

    The relay records one failure mode per account — a dead session — and
    nothing else. So the mapping is deliberately small: a dead session is
    `expired_session` with the relay's own reason attached, and everything else
    is `connected`. Inventing `reconnecting` or `revoked` here would be a claim
    about the provider that nothing in this database can support; those states
    arrive when the connection lifecycle milestone records them.
    """
    if binding.session_dead_at is not None:
        reason = (binding.health_reason or "").strip()
        return (
            "expired_session",
            reason or "OnlyFans rejected the stored session for this account. It needs re-linking.",
        )
    return "connected", None


def capabilities() -> dict:
    """What this service can actually do for an account, honestly.

    `viewConversations` is available because this package implements it.
    Everything else is `not_configured`: there is no delivery authority, no
    content pipeline and no sales read in this milestone, so the dashboard must
    render those controls as not configured rather than as available-and-idle.
    """
    return {
        "viewConversations": CAPABILITY_AVAILABLE,
        "sendManualMessage": CAPABILITY_NOT_CONFIGURED,
        "pauseAutomation": CAPABILITY_NOT_CONFIGURED,
        "requestTakeover": CAPABILITY_NOT_CONFIGURED,
        "manageContent": CAPABILITY_NOT_CONFIGURED,
        "managePrices": CAPABILITY_NOT_CONFIGURED,
        "viewSales": CAPABILITY_NOT_CONFIGURED,
        "resolveDeliveryEvidence": CAPABILITY_NOT_CONFIGURED,
    }


def account_model(
    binding: BindingRow, *, anchor: datetime | None, now: datetime | None = None
) -> dict:
    health, health_reason = connection_health(binding)
    # An account with no observed activity is anchored at its own binding time,
    # so it reads "last known <when it was bound>" rather than "updated now".
    as_of = anchor or binding.created_at
    return {
        "accountId": binding.account_id,
        "binding": {
            "bindingId": binding.binding_id,
            "creatorId": binding.creator_id,
            "creatorName": binding.creator_name,
            "handle": binding.handle,
            "channel": "onlyfans",
        },
        "health": health,
        "healthReason": health_reason,
        # No control record exists yet for any account in this milestone, and
        # `automated` is the truth for that: nothing has paused it. Milestone 3
        # and the operator commands write the mode.
        "automationMode": "automated",
        "capabilities": capabilities(),
        "syncFreshness": freshness(as_of, now=now),
    }


def _priority(conversation: ConversationRow) -> str:
    return "priority" if (conversation.is_priority or conversation.is_pinned) else "normal"


def _fan_name(conversation: ConversationRow) -> str:
    for candidate in (conversation.fan_display_name, conversation.fan_username):
        text = (candidate or "").strip()
        if text:
            return text
    return _UNNAMED_FAN


def _mode(state: StateRow | None) -> str:
    raw = (state.mode if state else None) or "automated"
    return raw if raw in ("automated", "human_controlled", "paused", "no_contact") else "automated"


def _takeover(state: StateRow | None) -> str:
    raw = (state.takeover_state if state else None) or "none"
    return raw if raw in ("none", "requested", "effective") else "none"


def _outcome(raw: str | None, fallback: str = "none") -> str:
    value = (raw or "").strip()
    return value if value in DELIVERY_OUTCOMES else fallback


def delivery_outcome(raw: str | None, fallback: str = "none") -> str:
    """Coerce a stored outcome into the dashboard's vocabulary.

    Public because the service needs it for the conversation-list row (which
    projects only the last message's outcome rather than a whole thread), and a
    second copy of this check is how a list row and a detail header would come
    to disagree about the same conversation.
    """
    return _outcome(raw, fallback)


def delivery_for_message(
    message: MessageRow, recorded: DeliveryRow | None
) -> tuple[str, str | None]:
    """The delivery outcome of one bubble, and its evidence reference.

    The rules, in order, and why each one is what it is:

      • A recorded outcome wins. That row is the receipt; nothing else on this
        path has a better claim to know what the provider did.
      • An inbound message has no send receipt, so it is `none`. The dashboard's
        vocabulary is about what left the building.
      • An outbound row carrying a `temp_id` is a local placeholder written
        optimistically at send time and not yet reconciled with the provider's
        own id (the relay's mass-broadcast path, and the one place it does
        this). `dispatching` is the honest reading: a request is in flight and
        has not been confirmed.
      • Any other outbound row came from OnlyFans' own history — the scrape, the
        websocket or the inbox poll wrote it with the provider's message id —
        so it was accepted. That is `sent`, and it says nothing about read or
        purchased, which the dashboard separately models.

    Known gap, recorded rather than papered over: the relay can mark a message
    `is_unsent` (we retracted it after it was accepted). The dashboard's outcome
    vocabulary has no "retracted" member, so a retracted message projects as
    `sent`. Adding one is a contract change, and it belongs with the topic that
    owns retraction rather than being smuggled in as a different outcome here.
    """
    if recorded is not None:
        return _outcome(recorded.outcome), recorded.evidence_ref
    if message.direction == "in":
        return "none", None
    if message.temp_id:
        return "dispatching", None
    return "sent", None


def message_origin(message: MessageRow) -> str:
    """Who wrote this bubble, from what the relay records.

    `automation_kind` is set for every automation send and `sent_by_employee_id`
    for every human one, so those two answer the question directly. An outbound
    row with neither is history we did not write — legacy rows, and rows that
    arrived from OnlyFans without a local author — and `imported` is the honest
    bucket for that rather than guessing a human sent it.
    """
    if message.direction == "in":
        return "fan"
    if message.automation_kind:
        return "ai"
    if message.sent_by_employee_id is not None:
        return "human"
    return "imported"


def message_model(
    *, conversation_id: str, message: MessageRow, recorded: DeliveryRow | None
) -> dict:
    outcome, evidence_ref = delivery_for_message(message, recorded)
    return {
        "messageId": str(message.message_id),
        "conversationId": conversation_id,
        "direction": "inbound" if message.direction == "in" else "outbound",
        "origin": message_origin(message),
        "body": message.body or "",
        "occurredAt": iso(message.created_at) or "",
        "delivery": outcome,
        "evidenceRef": evidence_ref,
    }


def conversation_summary(
    *,
    conversation: ConversationRow,
    state: StateRow | None,
    last_delivery: str,
    anchor: datetime | None,
    now: datetime | None = None,
) -> dict:
    """The list row and the detail header, from one mapping.

    Built once and reused for both reads: the dashboard asserts that the two
    agree, and two mappings would be two chances to disagree.
    """
    mode = _mode(state)
    unresolved = (state.unresolved_attempt_id if state else None) is not None or (
        last_delivery in UNRESOLVED_OUTCOMES
    )
    requires_attention = bool(
        unresolved
        or (state.requires_attention if state else False)
        or mode in ("paused", "no_contact")
    )
    as_of = conversation.last_message_at or anchor
    return {
        "conversationId": conversation_id_for(conversation.account_id, conversation.fan_id),
        "accountId": conversation.account_id,
        "revision": int(state.revision) if state else 0,
        "fan": {
            "fanId": str(conversation.fan_id),
            "displayName": _fan_name(conversation),
            "shortRef": str(conversation.fan_id),
        },
        "lastMessagePreview": (conversation.last_message_preview or "")[:160],
        "lastActivityAt": iso(as_of) or "",
        "unreadCount": int(conversation.unread_count or 0),
        "priority": _priority(conversation),
        "automationMode": mode,
        "takeoverState": _takeover(state),
        "delivery": _outcome(last_delivery),
        "requiresAttention": requires_attention,
        "freshness": freshness(as_of, now=now),
    }


def delivery_incident(
    *, state: StateRow | None, messages: list[MessageRow], deliveries: dict[int, DeliveryRow]
) -> dict | None:
    """The structured resolution path for an unresolved send, or None.

    There are exactly two ways a conversation holds an unresolved outcome: the
    control row names an attempt, or a message in the thread carries an outcome
    that is not settled. Both are surfaced, because an operator needs the
    evidence path whether or not the control write happened first.
    """
    attempt_id = state.unresolved_attempt_id if state else None
    if attempt_id:
        for message in reversed(messages):
            recorded = deliveries.get(message.message_id)
            if recorded is not None and recorded.attempt_id == attempt_id:
                return {
                    "attemptId": attempt_id,
                    "outcome": _outcome(recorded.outcome, "unknown"),
                    "recordedAt": iso(recorded.recorded_at) or iso(utc_now()),
                    "detail": recorded.detail
                    or "This send was not confirmed by OnlyFans. It is held until the outcome is resolved.",
                }
        return {
            "attemptId": attempt_id,
            "outcome": "unknown",
            "recordedAt": iso(state.updated_at if state else None) or iso(utc_now()),
            "detail": "A send on this conversation is unresolved. It is held until the outcome is resolved.",
        }
    for message in reversed(messages):
        recorded = deliveries.get(message.message_id)
        if recorded is not None and recorded.outcome in UNRESOLVED_OUTCOMES:
            return {
                "attemptId": recorded.attempt_id or f"message:{message.message_id}",
                "outcome": _outcome(recorded.outcome, "unknown"),
                "recordedAt": iso(recorded.recorded_at) or iso(utc_now()),
                "detail": recorded.detail
                or "This send was not confirmed by OnlyFans. It is held until the outcome is resolved.",
            }
    return None


def fact_models(facts: list[FactRow]) -> list[dict]:
    """Fan facts, with their provenance stated.

    Everything the relay holds about a fan here was extracted from the
    conversation or ingested with it, so the source is `imported`. The plan
    distinguishes creator facts, fan corrections and purchase-derived facts, and
    the dashboard's union has a member for each; none of those three is what
    this milestone reads, so claiming one would be a provenance bug.
    """
    return [{"label": fact.label, "value": fact.value, "source": "imported"} for fact in facts]


def owned_media_models(rows: list[OwnedRow]) -> list[dict]:
    return [
        {
            "mediaId": str(media_id),
            "label": row.label,
            "purchasedAt": iso(row.purchased_at) or "",
            "price": {"currency": MONEY_CURRENCY, "minorUnits": int(row.price_cents or 0)},
        }
        for row in rows
        for media_id in row.media_ids
    ]


def detail_model(
    *,
    conversation: ConversationRow,
    state: StateRow | None,
    messages: list[MessageRow],
    deliveries: dict[int, DeliveryRow],
    facts: list[FactRow],
    owned: list[OwnedRow],
    anchor: datetime | None,
    now: datetime | None = None,
) -> dict:
    reference = now or utc_now()
    conversation_id = conversation_id_for(conversation.account_id, conversation.fan_id)
    last_outcome = "none"
    if messages:
        last_outcome, _ = delivery_for_message(messages[-1], deliveries.get(messages[-1].message_id))
    summary = conversation_summary(
        conversation=conversation,
        state=state,
        last_delivery=last_outcome,
        anchor=anchor,
        now=reference,
    )
    as_of = conversation.last_message_at or anchor
    return {
        "conversationId": conversation_id,
        "accountId": conversation.account_id,
        "summary": summary,
        "messages": [
            message_model(
                conversation_id=conversation_id,
                message=message,
                recorded=deliveries.get(message.message_id),
            )
            for message in messages
        ],
        "facts": fact_models(facts),
        "ownedMedia": owned_media_models(owned),
        "deliveryIncident": delivery_incident(state=state, messages=messages, deliveries=deliveries),
        "freshness": freshness(as_of, now=reference),
    }
