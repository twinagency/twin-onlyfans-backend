"""The bridge worker: frozen turn -> Twin draft -> one checked send -> ack.

Manually invoked and bounded (`run_once`), never a scheduler. Every step is
persisted BEFORE the side effect it describes, and every transition is an atomic
conditional UPDATE against the current owner token + revision + state, so a
worker that lost its claim can no longer act or overwrite a replacement's work.

Fixed semantics (see the bridge README for the full table):

  frozen        -> claim, submit the FROZEN event once
  submitted     -> read status; never regenerate, never mint a new id
  draft_ready   -> dispatch under the last-instant gate, or wait/park/cancel
  dispatching   -> recovered as unknown (safety favours a possible duplicate
                   over a silent second POST)
  sent          -> retry only the idempotent ack/status, never the platform POST
  delivered     -> complete

Nothing here sleeps: the caller supplies `now`, and the delay between bounded
attempts is expressed as `next_action_at`, so tests drive time instead of waiting.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select, update

from db.engine import get_session
from db.models import Message, TwinBridgeInput, TwinBridgeTurn, TwinBridgeTurnMember

from . import turns as turns_mod
from . import contract
from .client import (
    CATEGORY_NOT_FOUND,
    TwinBridgeClient,
    TwinClientError,
)
from .errors import ContractError
from .coordination import (
    CONV_ACTIVE,
    CONV_ATTENTION,
    CONV_PAUSED,
    authorize_dispatch,
    claim_turn,
    ensure_conversation,
    load_conversation,
    pause_conversation,
    release_ownership,
    release_turn,
    reserve_ownership,
    revoke_dispatch,
    set_budget_limit,
    unresolved_turn,
)
from .delivery import (
    ATTEMPT_SENT,
    BudgetExhausted,
    AttemptExists,
    GateRefused,
    HistoryUnreadable,
    TURN_DELIVERED,
    TURN_DISPATCHING,
    TURN_DRAFT_READY,
    TURN_PARKED,
    TURN_SENT,
    TURN_UNKNOWN,
    begin_attempt,
    count_attempts,
    external_outbound_ids,
    known_provider_outbound_ids,
    mark_refused,
    mark_sent,
    mark_unknown,
    pause_for_attention,
    recover_dispatching,
)
from .platform import OnlyFansTextAdapter, PlatformError, PlatformRefused, PlatformUnknown
from .scope import BridgeScope, load_scope

log = logging.getLogger("of-relay.twin_bridge.worker")

STATE_FROZEN = "frozen"
STATE_SUBMITTED = "submitted"
# A turn whose unsent Twin work is being cancelled. Kept in OPEN_STATES so the
# bounded cancellation reconciliation keeps running across passes, independently
# of whether the turn could ever be dispatched.
STATE_CANCELLING = "cancelling"

# Work the worker still owns. Deliberately EXCLUDES sent/unknown/attention: those
# carry delivery evidence and are never overwritten by cancellation or resume.
OPEN_STATES = (STATE_FROZEN, STATE_SUBMITTED, STATE_CANCELLING,
               TURN_DRAFT_READY, TURN_DISPATCHING, TURN_SENT)

MAX_SUBMISSIONS = 3
MAX_STATUS_POLLS = 60
MAX_ACK_ATTEMPTS = 3
MAX_CANCEL_ATTEMPTS = 3

SUBMIT_DELAYS = (2.0, 5.0)
ACK_DELAYS = (2.0, 5.0)
POLL_DELAY_S = 2.0

OWNER_TTL_S = 120.0

# One stable owner token per PROCESS. Re-acquiring with the same token is a
# renewal (it keeps the conversation revision, so an accepted draft stays valid
# across passes), while a different process gets a different token and advances
# the fence when it takes over.
_PROCESS_OWNER_TOKEN = f"bridge-{uuid.uuid4().hex}"


@dataclass
class WorkReport:
    """What one bounded run did. Counts and fixed reasons only."""

    conversations: int = 0
    frozen: int = 0
    submitted: int = 0
    polls: int = 0
    dispatched: int = 0
    acked: int = 0
    delivered: int = 0
    parked: int = 0
    unknown: int = 0
    held: int = 0
    actions: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def send_enabled(env: dict | None = None) -> bool:
    """Send enablement is SEPARATE from worker enablement and off by default."""
    src = os.environ if env is None else env
    flag = str(src.get("TWIN_BRIDGE_SEND_ENABLED", "")).strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return False
    limit = str(src.get("TWIN_BRIDGE_SEND_LIMIT", "")).strip()
    return limit.isdigit() and int(limit) > 0


def send_limit(env: dict | None = None) -> int:
    src = os.environ if env is None else env
    limit = str(src.get("TWIN_BRIDGE_SEND_LIMIT", "")).strip()
    return int(limit) if limit.isdigit() else 0


def _delay(counters: tuple[float, ...], attempt: int) -> float:
    """The documented schedule: 2s then 5s, then the last value again."""
    if attempt <= 0:
        return counters[0]
    return counters[min(attempt - 1, len(counters) - 1)]


def _fresh_clock(now: datetime):
    """A monotonic-clock callable that advances in real time FROM `now`.

    `now` may be injected (tests, a scheduled pass), but a decision taken after
    an awaited step must not reuse that frozen instant: authority and leases can
    expire while we wait. This returns a fresh instant on the same basis as the
    pass began, so an injected past `now` does not by itself look expired while
    real elapsed time still advances the decision clock.
    """
    base = _monotonic()

    def clock() -> datetime:
        return now + timedelta(seconds=_monotonic() - base)

    return clock


def _monotonic() -> float:
    import time
    return time.monotonic()


async def _transition_turn(
    *, account_id: str, fan_id: int, turn_id: str, owner_token: str,
    from_states: tuple[str, ...], values: dict,
) -> bool:
    """A conditional turn write: current owner + one of the expected states."""
    async with get_session() as session:
        res = await session.execute(
            update(TwinBridgeTurn)
            .where(
                TwinBridgeTurn.account_id == str(account_id),
                TwinBridgeTurn.fan_id == int(fan_id),
                TwinBridgeTurn.turn_id == str(turn_id),
                TwinBridgeTurn.owner_token == str(owner_token),
                TwinBridgeTurn.state.in_(list(from_states)),
            )
            .values(**values)
        )
        return (res.rowcount or 0) > 0


async def run_once(
    *,
    now: datetime | None = None,
    scope: BridgeScope | None = None,
    batch_size: int = turns_mod.DEFAULT_BATCH_SIZE,
    client: TwinBridgeClient | None = None,
    adapter=None,
    enable_send: bool | None = None,
    owner_token: str | None = None,
    recover: bool = True,
    send_limit_override: int | None = None,
    clock=None,
) -> WorkReport:
    """One bounded pass. Safe to call repeatedly; a re-run never resets a budget.

    `send_limit_override` is the CLI's explicit durable cap. It is applied as a
    MINIMUM against the persisted cap (`set_budget_limit`), never a raise.
    `clock` is an optional fresh-clock callable used at decisions taken AFTER an
    awaited step (a wait must not leave a stale `now` behind).
    """
    report = WorkReport()
    active = scope if scope is not None else load_scope()
    if not active.enabled or active.activation_cutoff is None:
        return report
    now = now or datetime.utcnow()
    owner_token = owner_token or _PROCESS_OWNER_TOKEN
    sends_permitted = send_enabled() if enable_send is None else bool(enable_send)
    client = client or _default_client()
    effective_limit = send_limit_override if (
        send_limit_override is not None and int(send_limit_override) > 0
    ) else send_limit()
    # Default the decision clock to a FRESH, advancing instant on the same basis
    # as `now`. A fixed pass start time must not be reused for authority checks
    # taken after awaited steps (a claim/lease can expire while we wait), and it
    # must not be replaced by the raw wall clock either (an injected past `now`
    # would then look instantly expired for every test and backfill).
    clock = clock or _fresh_clock(now)

    if recover:
        await recover_dispatching(now=now, account_id=active.account_id)

    formed = await turns_mod.form_turns(now=now, batch_size=batch_size, scope=active)
    report.frozen = formed.turns_frozen
    report.held += formed.held_open

    for fan_id in await _conversations_with_work(active):
        report.conversations += 1
        try:
            await _advance_conversation(
                report, active=active, fan_id=fan_id, now=now, client=client,
                adapter=adapter, sends_permitted=sends_permitted, owner_token=owner_token,
                send_limit_value=effective_limit, clock=clock,
            )
        except Exception:  # noqa: BLE001 - one conversation must not kill the pass
            log.exception("twin bridge conversation failed: account=%s fan=%s",
                          active.account_id, fan_id)
            await pause_for_attention(
                account_id=active.account_id, fan_id=fan_id,
                reason="worker-error", now=now,
            )
            report.reasons.append("worker-error")
    return report


async def _tombstone_pre_boundary(report: WorkReport, active, fan_id: int,
                                  boundary: datetime, now: datetime, *, client) -> None:
    """Move UNSENT work frozen before `boundary` into bounded cancellation.

    Only work that has not reached the provider is affected. A turn that is
    `dispatching`, `sent`, `delivery_unknown` or already `parked` carries
    delivery evidence and is left alone - cancellation must never erase a
    confirmed/unknown send or unschedule its acknowledgement.

    Cancellation is a DURABLE, BOUNDED reconciliation (`cancelling` +
    `cancel_attempts`), not a one-shot local tombstone: a lost submit response
    returns no request id, and the frozen event is the identity Twin needs, so
    the worker keeps reconciling across passes until Twin settles or the bound
    is spent. The turn is never dispatchable while it is `cancelling`.
    """
    unsent = (STATE_FROZEN, STATE_SUBMITTED, TURN_DRAFT_READY)
    async with get_session() as session:
        turns = list((await session.execute(
            select(TwinBridgeTurn).where(
                TwinBridgeTurn.account_id == active.account_id,
                TwinBridgeTurn.fan_id == int(fan_id),
                TwinBridgeTurn.state.in_(list(unsent)),
                TwinBridgeTurn.created_at < boundary,
            )
        )).scalars())
    for turn in turns:
        async with get_session() as session:
            # Move to `cancelling` ONLY from an unsent state: a concurrent
            # dispatch that already reached `dispatching`/`sent` must win.
            await session.execute(
                update(TwinBridgeTurn)
                .where(
                    TwinBridgeTurn.account_id == active.account_id,
                    TwinBridgeTurn.fan_id == int(fan_id),
                    TwinBridgeTurn.turn_id == turn.turn_id,
                    TwinBridgeTurn.state.in_(list(unsent)),
                )
                .values(state=STATE_CANCELLING, reason="cancelled-pre-boundary",
                        owner_token=None, owner_expires_at=None, updated_at=now)
            )
            member_ids = await turns_mod.member_ids(turn)
            if member_ids:
                await session.execute(
                    update(TwinBridgeInput)
                    .where(
                        TwinBridgeInput.account_id == active.account_id,
                        TwinBridgeInput.fan_id == int(fan_id),
                        TwinBridgeInput.provider_message_id.in_(member_ids),
                    )
                    .values(state=turns_mod.INPUT_PARKED)
                )
        report.reasons.append("cancelling-pre-boundary")


async def _conversations_with_work(scope: BridgeScope) -> list[int]:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(TwinBridgeTurn.fan_id)
                .where(
                    TwinBridgeTurn.account_id == scope.account_id,
                    TwinBridgeTurn.state.in_(list(OPEN_STATES)),
                )
                .distinct()
                .order_by(TwinBridgeTurn.fan_id)
            )
        ).scalars().all()
    return [int(v) for v in rows]


async def _advance_conversation(
    report: WorkReport, *, active: BridgeScope, fan_id: int, now: datetime,
    client, adapter, sends_permitted: bool, owner_token: str,
    send_limit_value: int = 0, clock=None,
) -> None:
    async with get_session() as session:
        conversation = await load_conversation(session, active.account_id, fan_id)
    if conversation is None:
        async with get_session() as session:
            conversation = await ensure_conversation(
                session, account_id=active.account_id, fan_id=fan_id,
                creator_slug=active.creator_slug, conversation_id=str(fan_id),
                scope_fingerprint=turns_mod.scope_fingerprint(active), now=now,
                send_budget_limit=send_limit_value,
            )
    # A resume or an operator cancel opens a hard boundary: any work frozen
    # before it is tombstoned (and Twin-cancelled where it ever got a draft), so
    # a resumed conversation never sends the old backlog and a late Twin
    # completion cannot re-open it. This runs even while the conversation is
    # PAUSED (cancel sets paused), so a cancelled backlog is still tombstoned.
    # The NEWEST applicable control boundary, not whichever timestamp happens to
    # be non-null first: after a pause/resume then a later cancel, the cancel is
    # the boundary that must invalidate the newer draft.
    boundary = max(
        (t for t in (conversation.reset_at, conversation.cancelled_at) if t is not None),
        default=None,
    )
    if boundary is not None:
        await _tombstone_pre_boundary(
            report, active, fan_id, boundary, now, client=client,
        )

    if conversation.state != CONV_ACTIVE:
        # A paused / attention conversation never silently resumes, never falls
        # back to the older automation writers, and never generates or dispatches.
        # But OUTSTANDING EVIDENCE must still be resolved: a confirmed send
        # awaiting its acknowledgement is acked, and cancelled unsent work is
        # reconciled - neither is abandoned because the conversation is paused.
        async with get_session() as session:
            pending = await unresolved_turn(session, active.account_id, fan_id)
        if pending is not None and pending.state == TURN_SENT:
            await _ack(report, active, pending, now, client, owner_token)
        elif pending is not None and pending.state == STATE_CANCELLING:
            await _reconcile_cancel(report, active, pending, now, client, owner_token)
        report.held += 1
        return

    # The trusted scope may have changed since this conversation was created
    # (creator slug, cutoff, allowlist). A frozen draft that belongs to the OLD
    # scope must never be dispatched under the new one; park and require an
    # operator, rather than relabelling it with the new fingerprint.
    expected_fp = turns_mod.scope_fingerprint(active)
    if conversation.scope_fingerprint != expected_fp:
        async with get_session() as session:
            turn = await unresolved_turn(session, active.account_id, fan_id)
        if turn is not None and turn.state in (STATE_FROZEN, STATE_SUBMITTED,
                                               TURN_DRAFT_READY):
            await _park(report, active, turn, owner_token, now, "scope-changed")
            await pause_for_attention(
                account_id=active.account_id, fan_id=fan_id,
                reason="scope-changed", now=now,
            )
        report.held += 1
        return

    # The configured pilot cap is authoritative for this conversation, and it is
    # stored durably so a restart cannot raise it. Zero/absent config means no
    # send is possible at all (`begin_attempt` then refuses).
    configured = send_limit_value
    if configured > 0 and int(conversation.send_budget_limit) != configured:
        await set_budget_limit(account_id=active.account_id, fan_id=fan_id,
                               limit=configured, now=now)

    if not await reserve_ownership(
        account_id=active.account_id, fan_id=fan_id, owner_token=owner_token,
        now=now, ttl_s=OWNER_TTL_S,
    ):
        report.held += 1
        return

    try:
        async with get_session() as session:
            turn = await unresolved_turn(session, active.account_id, fan_id)
            turn_id = turn.turn_id if turn is not None else None
        if turn is None:
            return
        if turn.state == STATE_FROZEN:
            await _submit(report, active, turn, now, client, owner_token)
        elif turn.state == STATE_SUBMITTED:
            await _poll(report, active, turn, now, client, owner_token)
        elif turn.state == TURN_DRAFT_READY:
            await _dispatch(
                report, active, turn, now, client=client, adapter=adapter,
                sends_permitted=sends_permitted, owner_token=owner_token,
                clock=clock,
            )
        elif turn.state == TURN_SENT:
            await _ack(report, active, turn, now, client, owner_token)
        elif turn.state == STATE_CANCELLING:
            await _reconcile_cancel(report, active, turn, now, client, owner_token)
    finally:
        # Release the turn claim every pass: an expired/unreleased claim must not
        # be able to block the next run, and a claim that already dispatched keeps
        # its ledger evidence regardless.
        if turn_id is not None:
            await release_turn(
                account_id=active.account_id, fan_id=fan_id, turn_id=turn_id,
                owner_token=owner_token, now=now,
            )
        # The CONVERSATION reservation is the durable per-fan ownership: while an
        # unresolved turn exists the bridge keeps it, so no competing writer can
        # slip in between passes. Only a resolved conversation is released, which
        # is what lets a later inbound turn proceed.
        async with get_session() as session:
            still_open = await unresolved_turn(session, active.account_id, fan_id)
        if still_open is None:
            await release_ownership(
                account_id=active.account_id, fan_id=fan_id,
                owner_token=owner_token, now=now,
            )


def _default_client():
    """The client from trusted configuration; never from a message."""
    base = os.environ.get("TWIN_BRIDGE_BASE_URL", "")
    token = os.environ.get("TWIN_BRIDGE_TOKEN", "")
    if not base or not token:
        raise RuntimeError(
            "TWIN_BRIDGE_BASE_URL and TWIN_BRIDGE_TOKEN are required to call Twin"
        )
    return TwinBridgeClient(base, token)


async def _acquire_fan_lease(account_id: str, fan_id: int, holder: str) -> bool:
    """Participate in the SHARED per-fan send lease used by the automations.

    The bridge holds the same `fan_lease` row as A05/A06/A07/A11, so a fan owned
    by another live automation can never be messaged twice. A missing automation
    layer is treated as "no lease available" and does not block the bridge in a
    deployment that has no automations.
    """
    try:
        from automation_executor import acquire_fan_lease
    except Exception:  # pragma: no cover - optional component
        return True
    try:
        return bool(await acquire_fan_lease(account_id, int(fan_id), holder))
    except Exception:  # noqa: BLE001 - a lease-layer fault must not double-send
        log.exception("twin bridge fan-lease acquisition failed")
        return False


async def _release_fan_lease(account_id: str, fan_id: int, holder: str) -> None:
    """Drop ONLY our own lease (narrowed by holder) after a non-send outcome."""
    try:
        from automation_executor import release_fan_lease
    except Exception:  # pragma: no cover - optional component
        return
    try:
        await release_fan_lease(account_id, int(fan_id), holder=holder)
    except Exception:  # noqa: BLE001
        log.exception("twin bridge fan-lease release failed")


async def _fan_eligible(account_id: str, fan_id: int) -> bool:
    """The EXISTING fan eligibility gates, in addition to the shared lease.

    Two gates the bridge must not bypass:

    * `automation_executor.fan_on_cooldown` - `fans.automation_paused_until` is
      respected exactly as every other sender respects it. This reads the
      existing value; it does not introduce a new post-reply cooldown.
    * the existing per-account `skip_list` ("don't auto-reply") row.

    A fault in either gate refuses (fail closed) rather than sending.
    """
    try:
        from automation_executor import fan_on_cooldown
    except Exception:  # pragma: no cover - optional component
        fan_on_cooldown = None
    if fan_on_cooldown is not None:
        try:
            if await fan_on_cooldown(account_id, int(fan_id)):
                return False
        except Exception:  # noqa: BLE001 - never send on an unknown eligibility
            log.exception("twin bridge fan-cooldown check failed")
            return False
    try:
        from db.models import SkipList
        async with get_session() as session:
            row = (
                await session.execute(
                    select(SkipList.reason).where(
                        SkipList.account_id == str(account_id),
                        SkipList.fan_id == int(fan_id),
                    ).limit(1)
                )
            ).first()
        if row is not None:
            return False
    except Exception:  # noqa: BLE001
        log.exception("twin bridge skip-list check failed")
        return False
    return True


# ── generation ────────────────────────────────────────────────────────

def _event_of(turn: TwinBridgeTurn) -> dict:
    import json
    return json.loads(turn.event_json)


async def _claim(report, active, turn, now, owner_token, from_state) -> bool:
    return await claim_turn(
        account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
        owner_token=owner_token, now=now, expected_state=from_state, ttl_s=OWNER_TTL_S,
    )


async def _submit(report: WorkReport, active, turn, now, client, owner_token) -> None:
    if turn.owner_token is not None and turn.owner_expires_at is not None and turn.owner_expires_at > now:
        if turn.owner_token != owner_token:
            report.held += 1
            return
    if turn.next_action_at is not None and turn.next_action_at > now:
        report.held += 1
        return
    if not await _claim(report, active, turn, now, owner_token, STATE_FROZEN):
        report.held += 1
        return

    attempt = int(turn.generation_attempts) + 1
    if attempt > MAX_SUBMISSIONS:
        await _park(report, active, turn, owner_token, now,
                    "generation-attempts-exhausted", conv_state=CONV_ATTENTION)
        return

    # Persist the durable retry bound BEFORE the external call. A crash between
    # the request and the response must leave the attempt count already spent,
    # so the promised retry budget is real across an interruption instead of
    # resetting to zero on every restart.
    await _transition_turn(
        account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
        owner_token=owner_token, from_states=(STATE_FROZEN,),
        values={
            "state": STATE_SUBMITTED,
            "generation_attempts": attempt,
            "next_action_at": now,
            "updated_at": now,
        },
    )

    event = _event_of(turn)
    try:
        result = await client.request_draft(event)
    except TwinClientError as exc:
        if exc.category == CATEGORY_NOT_FOUND and attempt < MAX_SUBMISSIONS:
            # A verified not-found AFTER an uncertain admission: a bounded
            # re-submission of the SAME event id is permitted (Twin dedupes).
            await _transition_turn(
                account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
                owner_token=owner_token, from_states=(STATE_SUBMITTED,),
                values={
                    "state": STATE_SUBMITTED,
                    "generation_attempts": attempt,
                    "next_action_at": now + timedelta(seconds=_delay(SUBMIT_DELAYS, attempt)),
                    "updated_at": now,
                },
            )
            report.submitted += 1
            return
        await _transition_turn(
            account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
            owner_token=owner_token, from_states=(STATE_SUBMITTED,),
            values={
                "state": STATE_SUBMITTED if exc.uncertain else TURN_PARKED,
                "reason": f"submit-{exc.category}",
                "generation_attempts": attempt,
                "next_action_at": now + timedelta(seconds=_delay(SUBMIT_DELAYS, attempt)),
                "updated_at": now,
            },
        )
        report.reasons.append(f"submit-{exc.category}")
        if exc.uncertain:
            report.submitted += 1
        else:
            report.parked += 1
            await pause_for_attention(
                account_id=active.account_id, fan_id=int(turn.fan_id),
                reason=f"submit-{exc.category}", now=now,
            )
        return

    await _accept_draft(report, active, turn, owner_token, now, result, attempt)


async def _accept_draft(report, active, turn, owner_token, now, result, attempt) -> None:
    async with get_session() as session:
        conversation = await load_conversation(session, active.account_id, int(turn.fan_id))
    revision = int(conversation.revision) if conversation is not None else 1

    if not result.deliverable:
        # failed / interrupted / unsupported / stale / completed-without-a-draft:
        # record attention and pause. No fallback text, no fresh id, no send.
        await _transition_turn(
            account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
            owner_token=owner_token, from_states=(STATE_SUBMITTED,),
            values={
                "state": TURN_PARKED,
                "reason": f"draft-{result.status}",
                "request_id": result.request_id,
                "epoch": result.epoch,
                "generation_attempts": attempt,
                "updated_at": now,
            },
        )
        report.parked += 1
        report.reasons.append(f"draft-{result.status}")
        await pause_for_attention(
            account_id=active.account_id, fan_id=int(turn.fan_id),
            reason=f"draft-{result.status}", now=now,
        )
        return

    await _transition_turn(
        account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
        owner_token=owner_token, from_states=(STATE_SUBMITTED,),
        values={
            "state": TURN_DRAFT_READY,
            "reason": None,
            "request_id": result.request_id,
            "draft_text": result.text,
            "epoch": result.epoch,
            "dispatch_revision": revision,
            "generation_attempts": attempt,
            "next_action_at": None,
            "updated_at": now,
        },
    )
    report.submitted += 1


async def _poll(report: WorkReport, active, turn, now, client, owner_token) -> None:
    if turn.next_action_at is not None and turn.next_action_at > now:
        report.held += 1
        return
    if not await _claim(report, active, turn, now, owner_token, STATE_SUBMITTED):
        report.held += 1
        return
    async with get_session() as session:
        conversation = await load_conversation(session, active.account_id, int(turn.fan_id))
    fence_revision = int(conversation.revision) if conversation is not None else 0
    polls = int(turn.status_polls) + 1
    if polls > MAX_STATUS_POLLS:
        await _park(report, active, turn, owner_token, now, "status-polls-exhausted",
                    conv_state=CONV_ATTENTION)
        return

    # Persist the poll bound BEFORE the read, so an interrupted status call still
    # counts against the durable budget instead of being lost on restart.
    await _transition_turn(
        account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
        owner_token=owner_token, from_states=(STATE_SUBMITTED,),
        values={"status_polls": polls, "updated_at": now},
    )

    event = _event_of(turn)
    try:
        result = await client.read_status(event)
    except TwinClientError as exc:
        if exc.category == CATEGORY_NOT_FOUND:
            # Proven absent: one bounded re-submission of the exact same event.
            if int(turn.generation_attempts) < MAX_SUBMISSIONS:
                await _transition_turn(
                    account_id=active.account_id, fan_id=int(turn.fan_id),
                    turn_id=turn.turn_id, owner_token=owner_token,
                    from_states=(STATE_SUBMITTED,),
                    values={
                        "state": STATE_FROZEN,
                        "generation_attempts": int(turn.generation_attempts),
                        "status_polls": polls,
                        "next_action_at": now,
                        "updated_at": now,
                    },
                )
                return
            await _park(report, active, turn, owner_token, now, "status-not-found",
                        conv_state=CONV_ATTENTION)
            return
        # A read-only status failure is NOT evidence about any earlier send.
        await _transition_turn(
            account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
            owner_token=owner_token, from_states=(STATE_SUBMITTED,),
            values={
                "status_polls": polls,
                "next_action_at": now + timedelta(seconds=POLL_DELAY_S),
                "updated_at": now,
            },
        )
        report.polls += 1
        return

    report.polls += 1
    if result.status in ("queued", "in_progress") or not result.deliverable:
        if result.status == "cancelled":
            await _transition_turn(
                account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
                owner_token=owner_token, from_states=(STATE_SUBMITTED,),
                values={"state": "cancelled", "reason": "twin-cancelled",
                        "status_polls": polls, "updated_at": now},
            )
            report.parked += 1
            return
        if result.status in ("failed", "interrupted", "unsupported"):
            await _park(report, active, turn, owner_token, now, f"twin-{result.status}",
                        conv_state=CONV_ATTENTION, polls=polls)
            return
        await _transition_turn(
            account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
            owner_token=owner_token, from_states=(STATE_SUBMITTED,),
            values={
                "state": STATE_SUBMITTED,
                "status_polls": polls,
                "next_action_at": now + timedelta(seconds=POLL_DELAY_S),
                "updated_at": now,
            },
        )
        return

    if result.status == "delivered":
        await _transition_turn(
            account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
            owner_token=owner_token, from_states=(STATE_SUBMITTED,),
            values={"state": TURN_SENT, "request_id": result.request_id, "epoch": result.epoch,
                    "status_polls": polls, "next_action_at": now, "updated_at": now},
        )
        return

    await _transition_turn(
        account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
        owner_token=owner_token, from_states=(STATE_SUBMITTED,),
        values={
            "state": TURN_DRAFT_READY,
            "reason": None,
            "request_id": result.request_id,
            "draft_text": result.text,
            "epoch": result.epoch,
            "dispatch_revision": fence_revision,
            "status_polls": polls,
            "next_action_at": None,
            "updated_at": now,
        },
    )


# ── dispatch ──────────────────────────────────────────────────────────

async def _dispatch(report: WorkReport, active, turn, now, *, client, adapter,
                    sends_permitted: bool, owner_token, clock=None) -> None:
    if not sends_permitted:
        report.held += 1
        return
    if adapter is None:
        report.held += 1
        return
    if not await _claim(report, active, turn, now, owner_token, TURN_DRAFT_READY):
        report.held += 1
        return
    if turn.draft_text is None:
        await _park(report, active, turn, owner_token, now, "draft-missing",
                    conv_state=CONV_ATTENTION)
        return

    fan_id = int(turn.fan_id)

    async def _authority_ok(decision_now: datetime) -> tuple[bool, str]:
        """Re-read the durable authority THIS attempt must still hold.

        Called again after every awaited step, because a replacement worker, a
        takeover, a pause or an explicit release can land while we wait. The
        decision is a conditional statement of the whole identity - account,
        fan, turn, claim, revision, reservation, state and claim EXPIRY - not a
        `purpose` string. `decision_now` is a FRESH clock, so a claim that
        expired while we waited is refused here rather than POSTed under.
        """
        async with get_session() as session:
            conv = await load_conversation(session, active.account_id, fan_id)
            row = await session.get(
                TwinBridgeTurn, (active.account_id, fan_id, turn.turn_id)
            )
        if conv is None or row is None:
            return False, "claim-lost"
        if conv.state != CONV_ACTIVE or not bool(conv.reserved):
            return False, "conversation-not-active"
        if conv.owner_token != owner_token:
            return False, "claim-lost"
        if conv.owner_expires_at is not None and conv.owner_expires_at <= decision_now:
            # The claim we hold has EXPIRED. A fresh clock is what makes this
            # observable: a stale `now` would happily dispatch under a dead lease.
            return False, "authority-expired"
        if int(conv.revision) != int(row.dispatch_revision or 0):
            return False, "draft-obsolete"
        if row.owner_token != owner_token or row.state not in (TURN_DRAFT_READY, TURN_DISPATCHING):
            return False, "claim-lost"
        return True, ""

    async with get_session() as session:
        conversation = await load_conversation(session, active.account_id, fan_id)
    if conversation is None or conversation.state != CONV_ACTIVE:
        report.held += 1
        return
    if conversation.owner_token != owner_token:
        report.held += 1
        return
    if int(conversation.revision) != int(turn.dispatch_revision or 0):
        # Ownership changed (takeover / pause / reset) after the draft was
        # accepted, so the draft is obsolete and must not be dispatched.
        await _park(report, active, turn, owner_token, now, "draft-obsolete")
        return

    if not await _sources_still_valid(active, turn, now):
        await _park(report, active, turn, owner_token, now, "source-invalidated")
        return

    if adapter is not None:
        try:
            history_since, source_ids = await _history_source_boundary(active, turn)
            history = await adapter.preflight_history(
                fan_id, since=history_since, source_ids=source_ids,
            )
        except HistoryUnreadable:
            await _park(report, active, turn, owner_token, now, "history-unreadable")
            return
        except PlatformError as exc:
            # An unreadable history is a refusal, never an assumption.
            await _park(report, active, turn, owner_token, now, "history-unreadable")
            report.reasons.append(exc.reason)
            return
        ids = [int(i.get("id")) for i in history if str(i.get("id", "")).isdigit()]
        # ONLY the bridge's OWN proven sent ids are exempt from the takeover
        # check. A message that Fastt merely recorded as outbound (a human reply,
        # another writer) is still human activity and must park the draft.
        known = await known_provider_outbound_ids(active.account_id, fan_id, ids)
        try:
            external = external_outbound_ids(
                history, known, creator_id=active.account_id, fan_id=fan_id,
                since=history_since,
            )
        except HistoryUnreadable:
            await _park(report, active, turn, owner_token, now, "history-unreadable")
            return
        if external:
            # An observed human/outbound message the bridge did not send: park the
            # draft rather than talking over a human.
            await _park(report, active, turn, owner_token, now, "external-outbound")
            return

    # The source may have changed DURING the awaited history read (unsent,
    # deleted, reclassified). Re-validate as part of the final admission gate so
    # an invalidated source yields zero POSTs rather than a race-dependent one.
    if not await _sources_still_valid(active, turn, now):
        await _park(report, active, turn, owner_token, now, "source-invalidated")
        return

    # ── final authorization, AFTER every awaited step ────────────────────
    decision_now = clock() if clock is not None else now
    ok, reason = await _authority_ok(decision_now)
    if not ok:
        await _park(report, active, turn, owner_token, decision_now, reason)
        return

    # Re-validate the Twin draft at the last instant. A draft Twin cancelled or
    # superseded between generation and here must NOT be POSTed - the first
    # refusal would otherwise be the after-the-fact acknowledgement.
    event = _event_of(turn)
    try:
        status = await client.read_status(event)
    except TwinClientError as exc:
        # A read-only status failure is not evidence either way; park rather than
        # guess, and never send.
        await _park(report, active, turn, owner_token, decision_now, f"status-{exc.category}")
        return
    if not status.deliverable:
        await _park(report, active, turn, owner_token, decision_now,
                    f"draft-{status.status}", conv_state=CONV_ATTENTION)
        return

    # Shared per-fan lease: the bridge participates in the SAME gate the
    # automations use, so it can never double-message a fan another writer holds.
    # The existing cooldown / "don't auto-reply" gates are checked FIRST, exactly
    # as every other sender checks them, before the lease is taken.
    if not await _fan_eligible(active.account_id, fan_id):
        report.held += 1
        report.reasons.append("fan-not-eligible")
        return
    lease_holder = f"twin_bridge:{turn.turn_id}:{owner_token}"
    lease_ok = await _acquire_fan_lease(active.account_id, fan_id, lease_holder)
    if not lease_ok:
        # Another live writer owns the fan: hold (never dispatch) and let a later
        # pass retry once the lease expires.
        report.held += 1
        report.reasons.append("fan-lease-held")
        return

    # The lease acquisition above was another awaited step, and the draft's
    # canonical source can be invalidated at ANY point up to the POST. Re-read
    # BOTH the authority (on a fresh clock) and the source here, immediately
    # before the one POST, so neither an expired claim nor a since-unsent source
    # can slip a message out.
    decision_now = clock() if clock is not None else now
    ok, reason = await _authority_ok(decision_now)
    if not ok:
        await _release_fan_lease(active.account_id, fan_id, lease_holder)
        await _park(report, active, turn, owner_token, decision_now, reason)
        return
    if not await _sources_still_valid(active, turn, decision_now):
        await _release_fan_lease(active.account_id, fan_id, lease_holder)
        await _park(report, active, turn, owner_token, decision_now, "source-invalidated")
        return

    attempt_no = await count_attempts(active.account_id, fan_id, turn.turn_id) + 1
    # The frozen members are validated INSIDE begin_attempt's admission
    # transaction, so a source invalidated during the count above (or any earlier
    # await) refuses before any attempt row / budget unit / POST.
    member_ids = await turns_mod.member_ids(turn)
    try:
        attempt = await begin_attempt(
            account_id=active.account_id, fan_id=fan_id, turn_id=turn.turn_id,
            attempt_no=attempt_no, owner_token=owner_token,
            revision=int(conversation.revision), now=decision_now,
            member_ids=member_ids,
        )
    except BudgetExhausted:
        await _release_fan_lease(active.account_id, fan_id, lease_holder)
        await _park(report, active, turn, owner_token, now, "send-budget-exhausted",
                    conv_state=CONV_ATTENTION)
        return
    except AttemptExists:
        await _release_fan_lease(active.account_id, fan_id, lease_holder)
        await _park(report, active, turn, owner_token, now, "attempt-already-recorded",
                    conv_state=CONV_ATTENTION)
        return
    except GateRefused as exc:
        # Our claim was replaced between the last check and the durable write.
        await _release_fan_lease(active.account_id, fan_id, lease_holder)
        await _park(report, active, turn, owner_token, now, exc.reason)
        return

    moved = await _transition_turn(
        account_id=active.account_id, fan_id=fan_id, turn_id=turn.turn_id,
        owner_token=owner_token, from_states=(TURN_DRAFT_READY,),
        values={"state": TURN_DISPATCHING, "updated_at": now},
    )
    if not moved:
        # A conditional transition that did NOT apply means the claim/state moved
        # underneath us (a park, a takeover, a replacement). Never authorize a
        # POST on the strength of a failed transition: hold and let a later,
        # correctly-owned pass decide.
        await _release_fan_lease(active.account_id, fan_id, lease_holder)
        report.held += 1
        report.reasons.append("dispatch-transition-lost")
        return

    # Last-instant authority re-read, immediately before the POST.
    decision_now = clock() if clock is not None else now
    ok, reason = await _authority_ok(decision_now)
    if not ok:
        await _release_fan_lease(active.account_id, fan_id, lease_holder)
        await _park(report, active, turn, owner_token, decision_now, reason)
        return

    # Bind the ONE authorization the transport may accept to this exact attempt.
    dispatch_token = authorize_dispatch(
        active.account_id, fan_id, turn_id=turn.turn_id, attempt_no=attempt.attempt_no,
        owner_token=owner_token, revision=int(conversation.revision),
    )
    try:
        sent = await adapter.send_text(
            fan_id, turn.draft_text, dispatch_token=dispatch_token,
        )
    except PlatformRefused as exc:
        revoke_dispatch(active.account_id, fan_id, dispatch_token)
        await mark_refused(attempt_id=attempt.id, reason=exc.reason, now=now)
        await _park(report, active, turn, owner_token, now, f"send-{exc.reason}",
                    conv_state=CONV_ATTENTION)
        return
    except PlatformUnknown as exc:
        revoke_dispatch(active.account_id, fan_id, dispatch_token)
        await _release_fan_lease(active.account_id, fan_id, lease_holder)
        await mark_unknown(attempt_id=attempt.id, reason=exc.reason, now=now)
        await _transition_turn(
            account_id=active.account_id, fan_id=fan_id, turn_id=turn.turn_id,
            owner_token=owner_token, from_states=(TURN_DISPATCHING,),
            values={"state": TURN_UNKNOWN, "reason": exc.reason, "updated_at": now},
        )
        await pause_conversation(
            account_id=active.account_id, fan_id=fan_id,
            reason="delivery-unknown", now=now,
        )
        report.unknown += 1
        report.reasons.append(exc.reason)
        return
    except PlatformError as exc:
        revoke_dispatch(active.account_id, fan_id, dispatch_token)
        await _release_fan_lease(active.account_id, fan_id, lease_holder)
        await mark_unknown(attempt_id=attempt.id, reason=exc.reason, now=now)
        await _transition_turn(
            account_id=active.account_id, fan_id=fan_id, turn_id=turn.turn_id,
            owner_token=owner_token, from_states=(TURN_DISPATCHING,),
            values={"state": TURN_UNKNOWN, "reason": exc.reason, "updated_at": now},
        )
        await pause_conversation(account_id=active.account_id, fan_id=fan_id,
                                 reason="delivery-unknown", now=now)
        report.unknown += 1
        return

    # Evidence first: ledger, then attribution/bookkeeping, then ack.
    revoke_dispatch(active.account_id, fan_id, dispatch_token)
    await mark_sent(
        attempt_id=attempt.id, provider_message_id=sent.provider_message_id,
        evidence=sent.evidence, now=now,
    )
    await _transition_turn(
        account_id=active.account_id, fan_id=fan_id, turn_id=turn.turn_id,
        owner_token=owner_token, from_states=(TURN_DISPATCHING,),
        values={
            "state": TURN_SENT,
            "reason": None,
            "provider_message_id": sent.provider_message_id,
            "delivered_at": now,
            "updated_at": now,
        },
    )
    report.dispatched += 1
    try:
        await _bookkeep(active, turn, sent, now)
    except Exception:  # noqa: BLE001 - bookkeeping never weakens the ledger
        log.exception("twin bridge bookkeeping failed after a confirmed send")

    # The provider POST is settled, so the shared per-fan lease is released: the
    # bridge's own durable conversation reservation still excludes every other
    # writer for this fan, and holding the lease for the full automation TTL
    # would wrongly starve the next turn.
    await _release_fan_lease(active.account_id, fan_id, lease_holder)

    await _ack(report, active, turn, now, client, owner_token)


async def _history_source_boundary(active, turn) -> tuple[datetime, set[int]]:
    """Use the earliest provider time of ALL frozen inputs, not receipt order.

    Receipt order can differ from provider order. The full immutable member set
    also lets the adapter prove that its bounded history walk covers this turn.
    Missing or malformed receipt evidence never permits a send.
    """
    members = await turns_mod.member_ids(turn)
    ids = set(members)
    if not ids or len(ids) != len(members):
        raise HistoryUnreadable("frozen source identity is incomplete")
    async with get_session() as session:
        rows = (await session.execute(
            select(TwinBridgeInput.provider_message_id, TwinBridgeInput.occurred_at)
            .where(
                TwinBridgeInput.account_id == active.account_id,
                TwinBridgeInput.fan_id == int(turn.fan_id),
                TwinBridgeInput.provider_message_id.in_(ids),
            )
        )).all()
    if {int(row.provider_message_id) for row in rows} != ids:
        raise HistoryUnreadable("frozen source receipt is missing")
    times = [row.occurred_at for row in rows]
    if any(not isinstance(t, datetime) or t.tzinfo is not None for t in times):
        raise HistoryUnreadable("frozen source time is unusable")
    earliest = min(times)
    return earliest.replace(microsecond=(earliest.microsecond // 1000) * 1000), ids


async def _sources_still_valid(active, turn, now) -> bool:
    """Every source input is still assigned to THIS turn and still eligible.

    Membership alone is not enough: the assignment also promised that the
    CANONICAL source is still a plain inbound DM. A row that has since been
    unsent, reclassified as outbound, or turned into a paid/tip message must
    not be answered from a draft frozen over its old self.
    """
    async with get_session() as session:
        ids = await turns_mod.member_ids(turn)
        rows = (
            await session.execute(
                select(TwinBridgeTurnMember.provider_message_id, TwinBridgeTurnMember.turn_id)
                .where(
                    TwinBridgeTurnMember.account_id == active.account_id,
                    TwinBridgeTurnMember.fan_id == int(turn.fan_id),
                )
            )
        ).all()
        assignment = {int(r.provider_message_id): r.turn_id for r in rows}
        if any(assignment.get(mid) != turn.turn_id for mid in ids):
            return False
        states = (
            await session.execute(
                select(TwinBridgeInput.provider_message_id, TwinBridgeInput.state)
                .where(
                    TwinBridgeInput.account_id == active.account_id,
                    TwinBridgeInput.fan_id == int(turn.fan_id),
                    TwinBridgeInput.provider_message_id.in_(ids),
                )
            )
        ).all()
        if len(states) != len(ids):
            return False
        if not all(r.state == turns_mod.INPUT_ASSIGNED for r in states):
            return False

        canonical = (
            await session.execute(
                select(
                    Message.message_id, Message.direction, Message.is_unsent,
                    Message.price_cents, Message.is_tip,
                ).where(
                    Message.account_id == active.account_id,
                    Message.fan_id == int(turn.fan_id),
                    Message.message_id.in_(ids),
                )
            )
        ).all()
        if len(canonical) != len(ids):
            return False
        for r in canonical:
            if r.direction != "in":
                return False
            if bool(r.is_unsent):
                return False
            if int(r.price_cents or 0) != 0:
                return False
            if bool(r.is_tip):
                return False
        return True


async def _bookkeep(active, turn, sent, now) -> None:
    """Existing attribution/cache behaviour, best-effort by design.

    A failure here is retried independently and NEVER turns a confirmed provider
    send into a failed send or a second POST.
    """
    try:
        import attribution
    except Exception:  # pragma: no cover - optional component
        return
    writer = getattr(attribution, "write_outbound_attribution", None)
    if writer is None:
        return
    await writer(
        account_id=active.account_id,
        fan_id=int(turn.fan_id),
        message_id=int(sent.provider_message_id),
        # None resolves to the system Automation employee inside attribution,
        # which is the right owner for a background worker send; the
        # `automation_kind` below is what identifies this writer.
        sent_by_employee_id=None,
        body=turn.draft_text or "",
        price_cents=0,
        created_at=now,
        automation_kind="twin_bridge",
        # A background worker opts in to the synthetic live event; a failure here
        # is caught by _dispatch and never becomes a failed platform send.
        emit_live=True,
    )


# ── acknowledgement ───────────────────────────────────────────────────

async def _reconcile_cancel(report: WorkReport, active, turn, now, client, owner_token) -> None:
    """Boundedly reconcile a `cancelling` turn with Twin, then park it.

    The FROZEN EVENT is the cancellation identity, so a request whose submit
    response was lost (no returned request id) is still cancellable. Nothing
    here can ever dispatch the turn: it moves `cancelling -> parked`, and a
    late admission after a `not-found` therefore cannot resurrect obsolete work.
    """
    if turn.next_action_at is not None and turn.next_action_at > now:
        report.held += 1
        return
    if not await _claim(report, active, turn, now, owner_token, STATE_CANCELLING):
        report.held += 1
        return
    attempts = int(turn.cancel_attempts) + 1
    if attempts > MAX_CANCEL_ATTEMPTS:
        await _park(report, active, turn, owner_token, now, "cancel-attempts-exhausted",
                    conv_state=CONV_ATTENTION)
        return

    # Persist the cancellation bound BEFORE the external call.
    await _transition_turn(
        account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
        owner_token=owner_token, from_states=(STATE_CANCELLING,),
        values={"cancel_attempts": attempts, "updated_at": now},
    )

    event = _event_of(turn)
    # A cancellation can arrive BEFORE a still-in-flight submission is admitted,
    # so a single `not-found` is NOT proof Twin never saw the request. Reconcile
    # against the FROZEN identity: a status read first, then retry the cancel
    # within the bound. A late admission is DISCOVERED here and cancelled; the
    # turn is `cancelling`, which is never dispatchable, so obsolete work cannot
    # be resurrected even after a not-found.
    status_seen = False
    try:
        await client.read_status(event)
        status_seen = True
    except TwinClientError:
        status_seen = False
    try:
        await client.cancel_request(event)
    except TwinClientError as exc:
        if exc.category == CATEGORY_NOT_FOUND and not status_seen:
            # Twin has not admitted it YET. Keep bounded reconciliation so a late
            # admission is still cancelled rather than abandoned.
            await _transition_turn(
                account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
                owner_token=owner_token, from_states=(STATE_CANCELLING,),
                values={
                    "cancel_attempts": attempts,
                    "reason": "cancel-not-found-pending",
                    "next_action_at": now + timedelta(seconds=_delay(ACK_DELAYS, attempts)),
                    "updated_at": now,
                },
            )
            report.reasons.append("cancel-not-found-pending")
            return
        # Any other failure is not settled: retry within the bound. The turn is
        # still never dispatchable.
        await _transition_turn(
            account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
            owner_token=owner_token, from_states=(STATE_CANCELLING,),
            values={
                "cancel_attempts": attempts,
                "reason": f"cancel-{exc.category}",
                "next_action_at": now + timedelta(seconds=_delay(ACK_DELAYS, attempts)),
                "updated_at": now,
            },
        )
        report.reasons.append(f"cancel-{exc.category}")
        return

    await _park(report, active, turn, owner_token, now, "cancelled")


async def _ack(report: WorkReport, active, turn, now, client, owner_token) -> None:
    if turn.next_action_at is not None and turn.next_action_at > now:
        report.held += 1
        return
    if not await _claim(report, active, turn, now, owner_token, TURN_SENT):
        report.held += 1
        return
    attempts = int(turn.ack_attempts) + 1
    if attempts > MAX_ACK_ATTEMPTS:
        await _park(report, active, turn, owner_token, now, "ack-attempts-exhausted",
                    conv_state=CONV_ATTENTION)
        return

    # Persist the acknowledgement bound BEFORE the idempotent call, so an
    # interruption still counts against the durable ack budget.
    await _transition_turn(
        account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
        owner_token=owner_token, from_states=(TURN_SENT,),
        values={"ack_attempts": attempts, "updated_at": now},
    )

    event = _event_of(turn)
    try:
        decision = await client.acknowledge_delivery(event)
    except TwinClientError as exc:
        # Only the idempotent ACK is retried; the platform POST never is.
        await _transition_turn(
            account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
            owner_token=owner_token, from_states=(TURN_SENT,),
            values={
                "ack_attempts": attempts,
                "reason": f"ack-{exc.category}",
                "next_action_at": now + timedelta(seconds=_delay(ACK_DELAYS, attempts)),
                "updated_at": now,
            },
        )
        report.reasons.append(f"ack-{exc.category}")
        return

    # The ack only records Twin's delivery DECISION; the transcript commit is
    # Twin's own follow-up, so read the status back before calling it complete.
    committed_at = None
    try:
        status = await client.read_status(event)
        if status.committed_at:
            # The wire value is an ISO string; the column is a DateTime.
            committed_at = contract.parse_canonical_timestamp(status.committed_at, "committedAt")
    except (TwinClientError, ContractError):
        status = None

    await _transition_turn(
        account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
        owner_token=owner_token, from_states=(TURN_SENT,),
        values={
            "state": TURN_DELIVERED,
            "reason": None,
            "ack_attempts": attempts,
            "delivered_at": turn.delivered_at or now,
            "committed_at": committed_at,
            "updated_at": now,
        },
    )
    report.acked += 1
    report.delivered += 1
    report.actions.append(f"acked:{decision.status}")


async def _park(report: WorkReport, active, turn, owner_token, now, reason: str,
                conv_state: str | None = None, polls: int | None = None) -> None:
    values = {
        "state": TURN_PARKED,
        "reason": reason,
        "owner_token": None,
        "owner_expires_at": None,
        "updated_at": now,
    }
    if polls is not None:
        values["status_polls"] = polls
    await _transition_turn(
        account_id=active.account_id, fan_id=int(turn.fan_id), turn_id=turn.turn_id,
        owner_token=owner_token,
        from_states=(STATE_FROZEN, STATE_SUBMITTED, STATE_CANCELLING,
                     TURN_DRAFT_READY, TURN_DISPATCHING, TURN_SENT),
        values=values,
    )
    report.parked += 1
    report.reasons.append(reason)
    if conv_state is not None:
        await pause_for_attention(
            account_id=active.account_id, fan_id=int(turn.fan_id), reason=reason, now=now,
        )
