"""Durable merged turns: freeze a burst of inbound DMs into ONE Twin event.

The quiet window is a pure function of PERSISTED receipts, never of a sleep:

  * consecutive inputs whose `received_at` values are less than seven seconds
    apart belong to one burst; a gap of seven seconds or more starts another;
  * a burst is not closed until its LAST receipt is at least seven seconds old.

Because both facts come from columns already on the intake rows, restarting the
process recomputes exactly the same membership and never restarts the window.
Nothing here holds a database transaction across a model or network call, and
nothing here sleeps.

Freezing is idempotent by construction. `turn_id` is derived from the frozen
identity, so the same burst always produces the same id and the same event bytes
- a re-run after a crash inserts nothing new. Membership rows are keyed on the
SOURCE INPUT's own identity, so the database - not a preliminary SELECT - is what
stops two workers assigning one input to two different turns. The turn row and
all of its membership rows are written in ONE transaction, so a turn can never
exist without its intended members.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.engine import get_session
from db.models import TwinBridgeConversation, TwinBridgeInput, TwinBridgeTurn, TwinBridgeTurnMember

from . import contract
from .errors import ContractError
from .scope import BridgeScope, load_scope

log = logging.getLogger("of-relay.twin_bridge.turns")

QUIET_WINDOW_S = 7.0
DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 500
# How far one bounded pass will WALK a single burst's continuation before it
# stops. Distinct from the formation page size: the page bounds how much work a
# pass STARTS, while this bounds how far it looks to prove where a burst ENDS.
# Reaching it is never treated as a quiet boundary - the burst is split at a
# deterministic source boundary instead, so a burst longer than any page still
# makes progress and is never starved forever.
MAX_BURST_WALK = 5000
_WALK_CHUNK = 500

# Turn namespace for the synthetic `messageId`. Provider ids admitted by intake
# are positive integers, so a `twin-turn-v1-…` string can never alias a real
# source message id.
TURN_ID_PREFIX = "twin-turn-v1-"

INPUT_PENDING = "pending"
INPUT_ASSIGNED = "assigned"
INPUT_PARKED = "parked"

STATE_FROZEN = "frozen"


def scope_fingerprint(scope: BridgeScope) -> str:
    """A stable hash of the trusted scope an input was admitted under.

    Copied onto each conversation and turn so a later configuration change is
    visible as a mismatch instead of silently reassigning old work.
    """
    payload = json.dumps(
        {
            "version": scope.version,
            "account": scope.account_id,
            "creator": scope.creator_slug,
            "cutoff": scope.activation_cutoff.isoformat() if scope.activation_cutoff else None,
            "fans": sorted(scope.fan_allowlist),
        },
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def turn_id_for(*, account_id: str, creator_slug: str, fan_id: str, conversation_id: str,
                scope_fingerprint_value: str, member_ids: list[int]) -> str:
    """The deterministic id for one frozen burst.

    The hash covers a domain/version marker plus every identity component, so the
    same burst always yields the same id and any change to membership, account,
    creator, conversation or scope yields a different one.
    """
    canonical = json.dumps(
        {
            "domain": TURN_ID_PREFIX,
            "accountId": account_id,
            "creatorSlug": creator_slug,
            "fanId": fan_id,
            "conversationId": conversation_id,
            "scope": scope_fingerprint_value,
            "members": [int(m) for m in member_ids],
        },
        sort_keys=True, separators=(",", ":"),
    )
    return TURN_ID_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class FormReport:
    """What one bounded formation pass did. Counts and ids only - no fan text."""

    scanned: int = 0
    turns_frozen: int = 0
    inputs_assigned: int = 0
    inputs_parked: int = 0
    already_frozen: int = 0
    held_open: int = 0
    turn_ids: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Input:
    account_id: str
    fan_id: int
    provider_message_id: int
    creator_slug: str
    conversation_id: str
    activation_cutoff: datetime
    normalized_text: str
    occurred_at: datetime
    received_at: datetime
    admitted_at: datetime


async def _pending_inputs(session, scope: BridgeScope, limit: int) -> list[_Input]:
    """The oldest pending inputs in this scope, in deterministic order."""
    stmt = (
        select(
            TwinBridgeInput.account_id,
            TwinBridgeInput.fan_id,
            TwinBridgeInput.provider_message_id,
            TwinBridgeInput.creator_slug,
            TwinBridgeInput.conversation_id,
            TwinBridgeInput.activation_cutoff,
            TwinBridgeInput.normalized_text,
            TwinBridgeInput.occurred_at,
            TwinBridgeInput.received_at,
            TwinBridgeInput.admitted_at,
        )
        .where(TwinBridgeInput.account_id == scope.account_id)
        .where(TwinBridgeInput.state == INPUT_PENDING)
        .where(TwinBridgeInput.fan_id.in_(list(scope.fan_ids_for_sql())))
        # Deterministic within a conversation; the tie-break is the provider id,
        # so equal receipts still have a stable order.
        .order_by(TwinBridgeInput.received_at, TwinBridgeInput.provider_message_id)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        _Input(
            account_id=str(r.account_id),
            fan_id=int(r.fan_id),
            provider_message_id=int(r.provider_message_id),
            creator_slug=str(r.creator_slug),
            conversation_id=str(r.conversation_id),
            activation_cutoff=r.activation_cutoff,
            normalized_text=str(r.normalized_text or ""),
            occurred_at=r.occurred_at,
            received_at=r.received_at,
            admitted_at=r.admitted_at,
        )
        for r in rows
    ]


def _burst(rows: list[_Input]) -> list[_Input]:
    """The leading burst: consecutive receipts less than the window apart.

    Only the LEADING run matters - a later gap starts a different burst, which is
    handled on the next pass (or by the caller looking at the remainder).
    """
    if not rows:
        return []
    out = [rows[0]]
    for nxt in rows[1:]:
        if (nxt.received_at - out[-1].received_at).total_seconds() < QUIET_WINDOW_S:
            out.append(nxt)
        else:
            break
    return out


async def _conversation_boundaries(session, scope: BridgeScope,
                                   fan_ids: list[int]) -> dict[int, datetime]:
    """Per-fan resume/cancel boundaries in force for these conversations.

    The LATER of `reset_at` and `cancelled_at` is the input receipt boundary:
    inputs received at or after it are the only ones a resumed conversation may
    merge, so a pre-resume pending input cannot be frozen (or sent) later.
    """
    if not fan_ids:
        return {}
    rows = (
        await session.execute(
            select(
                TwinBridgeConversation.fan_id,
                TwinBridgeConversation.reset_at,
                TwinBridgeConversation.cancelled_at,
            ).where(
                TwinBridgeConversation.account_id == scope.account_id,
                TwinBridgeConversation.fan_id.in_([int(f) for f in fan_ids]),
            )
        )
    ).all()
    out: dict[int, datetime] = {}
    for r in rows:
        stamps = [t for t in (r.reset_at, r.cancelled_at) if t is not None]
        if stamps:
            out[int(r.fan_id)] = max(stamps)
    return out


async def _next_pending_batch(session, scope: BridgeScope, last: _Input,
                              limit: int) -> list[_Input]:
    """Up to `limit` pending inputs for this fan after `last`, in order."""
    stmt = (
        select(
            TwinBridgeInput.account_id,
            TwinBridgeInput.fan_id,
            TwinBridgeInput.provider_message_id,
            TwinBridgeInput.creator_slug,
            TwinBridgeInput.conversation_id,
            TwinBridgeInput.activation_cutoff,
            TwinBridgeInput.normalized_text,
            TwinBridgeInput.occurred_at,
            TwinBridgeInput.received_at,
            TwinBridgeInput.admitted_at,
        )
        .where(
            TwinBridgeInput.account_id == scope.account_id,
            TwinBridgeInput.fan_id == last.fan_id,
            TwinBridgeInput.state == INPUT_PENDING,
            or_(
                TwinBridgeInput.received_at > last.received_at,
                and_(
                    TwinBridgeInput.received_at == last.received_at,
                    TwinBridgeInput.provider_message_id > last.provider_message_id,
                ),
            ),
        )
        .order_by(TwinBridgeInput.received_at, TwinBridgeInput.provider_message_id)
        .limit(max(1, int(limit)))
    )
    return [
        _Input(
            account_id=str(r.account_id), fan_id=int(r.fan_id),
            provider_message_id=int(r.provider_message_id), creator_slug=str(r.creator_slug),
            conversation_id=str(r.conversation_id), activation_cutoff=r.activation_cutoff,
            normalized_text=str(r.normalized_text or ""),
            occurred_at=r.occurred_at, received_at=r.received_at,
            admitted_at=r.admitted_at,
        )
        for r in (await session.execute(stmt)).all()
    ]


async def _fan_tail_pending(session, scope: BridgeScope, fan_id: int) -> datetime | None:
    """The NEWEST pending receipt for one fan, or None when none is pending.

    Used to prove a burst is genuinely closed when the bounded walk stopped at
    its work cap: the walk's own last member is NOT the tail, so only the true
    newest pending receipt can show the chain has gone quiet.
    """
    return (
        await session.execute(
            select(TwinBridgeInput.received_at)
            .where(
                TwinBridgeInput.account_id == scope.account_id,
                TwinBridgeInput.fan_id == int(fan_id),
                TwinBridgeInput.state == INPUT_PENDING,
            )
            .order_by(
                TwinBridgeInput.received_at.desc(),
                TwinBridgeInput.provider_message_id.desc(),
            )
            .limit(1)
        )
    ).scalars().first()


async def _extend_burst(session, scope: BridgeScope,
                        burst: list[_Input]) -> tuple[list[_Input], bool]:
    """Walk a burst that reached the end of a TRUNCATED page.

    Returns `(members, ended)` where `ended` is True only when the 7-second
    chain was OBSERVED to stop (a real gap, or no further pending rows). The walk
    stops after `MAX_BURST_WALK` members and returns `ended=False`: a cap is NOT
    a quiet boundary, so the caller must not treat the truncated end as final.
    """
    if not burst:
        return burst, True
    extended = list(burst)
    last = extended[-1]
    while True:
        if len(extended) >= MAX_BURST_WALK:
            return extended, False
        batch = await _next_pending_batch(
            session, scope, last, min(_WALK_CHUNK, MAX_BURST_WALK - len(extended))
        )
        if not batch:
            return extended, True
        for nxt in batch:
            if (nxt.received_at - last.received_at).total_seconds() >= QUIET_WINDOW_S:
                return extended, True
            extended.append(nxt)
            last = nxt
            if len(extended) >= MAX_BURST_WALK:
                return extended, False


def _fits(members: list[_Input], candidate: _Input, *, account_id: str, creator_slug: str,
          conversation_id: str, scope_fp: str) -> bool:
    """Would adding `candidate` still satisfy Twin's two size limits?"""
    proposed = members + [candidate]
    try:
        event = build_turn_event(
            account_id=account_id, creator_slug=creator_slug,
            conversation_id=conversation_id, fan_id=str(candidate.fan_id),
            member_ids=[m.provider_message_id for m in proposed],
            text="\n".join(m.normalized_text for m in proposed),
            occurred_at=proposed[0].occurred_at, received_at=proposed[-1].received_at,
            scope_fingerprint_value=scope_fp,
        )
    except ContractError:
        return False
    return len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= contract.MAX_TOTAL_BYTES


def build_turn_event(*, account_id: str, creator_slug: str, conversation_id: str, fan_id: str,
                     member_ids: list[int], text: str, occurred_at: datetime, received_at: datetime,
                     scope_fingerprint_value: str) -> dict:
    """The frozen version-1 Twin input for one merged turn.

    The accepted wire shape is unchanged: `conversationId` and `fanId` stay the
    mapped fan strings, the synthetic turn id is `messageId`, `text` is the frozen
    joined text, `occurredAt` is the FIRST member's provider instant and
    `receivedAt` is the LAST member's receipt.
    """
    turn_id = turn_id_for(
        account_id=account_id, creator_slug=creator_slug, fan_id=fan_id,
        conversation_id=conversation_id, scope_fingerprint_value=scope_fingerprint_value,
        member_ids=member_ids,
    )
    return {
        "schemaVersion": contract.SCHEMA_VERSION,
        "channel": contract.CHANNEL,
        "accountId": contract.validate_id(account_id, "accountId"),
        "creatorSlug": contract.validate_id(creator_slug, "creatorSlug"),
        "conversationId": contract.validate_id(conversation_id, "conversationId"),
        "fanId": contract.validate_id(fan_id, "fanId"),
        "messageId": contract.validate_id(turn_id, "messageId"),
        "occurredAt": contract.canonical_timestamp(occurred_at),
        "receivedAt": contract.canonical_timestamp(received_at),
        "direction": contract.DIRECTION,
        "text": contract.validate_text(text),
    }


async def form_turns(
    *,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    scope: BridgeScope | None = None,
) -> FormReport:
    """Freeze at most `batch_size` pending inputs into their merged turns.

    Explicitly invoked, bounded, and safe to re-run: an input already assigned to
    a turn is never re-assigned, and a burst whose membership has not changed
    produces the same (already present) turn instead of a second one.
    """
    active = scope if scope is not None else load_scope()
    report = FormReport()
    if not active.enabled or active.activation_cutoff is None:
        return report

    size = max(1, min(int(batch_size), MAX_BATCH_SIZE))
    now = now or datetime.utcnow()
    scope_fp = scope_fingerprint(active)

    async with get_session() as session:
        # Fetch ONE extra row: a full page is the only thing that can hide a
        # burst continuation, and the probe distinguishes "the query saw the
        # whole pending set" from "the page cut it".
        fetched = await _pending_inputs(session, active, size + 1)
        truncated = len(fetched) > size
        rows = fetched[:size]
        report.scanned = len(rows)
        by_fan: dict[int, list[_Input]] = {}
        for row in rows:
            by_fan.setdefault(row.fan_id, []).append(row)

        # A resume / cancel boundary excludes inputs RECEIVED before it, so a
        # pre-boundary pending input can never be frozen (and sent) by a later
        # pass. This is an INPUT receipt boundary, not a turn-creation one.
        boundaries = await _conversation_boundaries(session, active, list(by_fan))
        for fan_id, fan_rows in by_fan.items():
            boundary = boundaries.get(fan_id)
            if boundary is not None:
                kept: list[_Input] = []
                for row in fan_rows:
                    if row.received_at < boundary:
                        await _park(session, row, "pre-boundary-input")
                        report.inputs_parked += 1
                    else:
                        kept.append(row)
                fan_rows = kept
            burst = _burst(fan_rows)
            if not burst:
                continue
            # The end of a limited query is NOT the end of a burst. When the
            # page was truncated and this fan's leading run reaches the end of
            # what we fetched, walk its true continuation before deciding.
            if truncated and len(burst) == len(fan_rows):
                burst, ended = await _extend_burst(session, active, burst)
            else:
                ended = True
            # A burst is only closed once its LAST receipt is OBSERVED to be old
            # enough. Until then the whole conversation waits; nothing freezes
            # early on the strength of a scan or work cap.
            if not ended:
                # The bounded walk stopped at its WORK cap without observing a
                # gap - that cap is NOT a quiet boundary. The only honest proof
                # of closure is the fan's NEWEST pending receipt being itself old
                # enough to be quiet; while it is not, nothing may freeze. Once
                # it IS quiet we make bounded progress on the walked prefix and
                # let the next pass handle the remainder, so a burst larger than
                # the cap neither freezes early nor starves.
                tail = await _fan_tail_pending(session, active, fan_id)
                if tail is None or (now - tail).total_seconds() < QUIET_WINDOW_S:
                    report.held_open += 1
                    continue
                report.reasons.append("burst-walk-bound")
            elif (now - burst[-1].received_at).total_seconds() < QUIET_WINDOW_S:
                report.held_open += 1
                continue
            # One savepoint per conversation: a lost membership race rolls back
            # ONLY this freeze (no turn row can survive without its members),
            # leaving every other conversation in the pass untouched.
            try:
                async with session.begin_nested():
                    await _freeze(session, report, burst, active, scope_fp, now)
            except _MembershipTaken:
                report.already_frozen += 1

    return report


async def _freeze(session, report: FormReport, burst: list[_Input], scope: BridgeScope,
                  scope_fp: str, now: datetime) -> None:
    """Freeze the deterministic maximal prefix of one closed burst."""
    members: list[_Input] = []
    for candidate in burst:
        if not candidate.normalized_text.strip():
            await _park(session, candidate, "empty-text")
            report.inputs_parked += 1
            continue
        if candidate.creator_slug != scope.creator_slug \
                or candidate.activation_cutoff != scope.activation_cutoff:
            # The trusted mapping (creator OR cutoff) moved since this input was
            # admitted. Park it for an operator rather than silently reassigning
            # the work under a new scope fingerprint.
            await _park(session, candidate, "scope-changed")
            report.inputs_parked += 1
            continue
        if not members:
            # A single input must satisfy the limits on its own; an oversized one
            # is parked explicitly rather than truncated into a valid-looking event.
            if not _fits(
                [], candidate,
                account_id=scope.account_id, creator_slug=scope.creator_slug,
                conversation_id=candidate.conversation_id, scope_fp=scope_fp,
            ):
                await _park(session, candidate, "unusable-text")
                report.inputs_parked += 1
                continue
            members = [candidate]
            continue
        if not _fits(
            members, candidate,
            account_id=scope.account_id, creator_slug=scope.creator_slug,
            conversation_id=candidate.conversation_id, scope_fp=scope_fp,
        ):
            # Deterministic split at a SOURCE-MESSAGE boundary: the overflow
            # input stays pending and starts the next turn. Nothing is truncated.
            break
        members.append(candidate)

    if not members:
        return

    fan_id = members[0].fan_id
    conversation_id = members[0].conversation_id
    member_ids = [m.provider_message_id for m in members]
    text = "\n".join(m.normalized_text for m in members)
    event = build_turn_event(
        account_id=scope.account_id, creator_slug=scope.creator_slug,
        conversation_id=conversation_id, fan_id=str(fan_id),
        member_ids=member_ids, text=text,
        occurred_at=members[0].occurred_at, received_at=members[-1].received_at,
        scope_fingerprint_value=scope_fp,
    )
    event_json = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    turn_id = event["messageId"]

    inserted = await session.execute(
        sqlite_insert(TwinBridgeTurn)
        .values(
            account_id=scope.account_id,
            fan_id=fan_id,
            turn_id=turn_id,
            creator_slug=scope.creator_slug,
            conversation_id=conversation_id,
            scope_fingerprint=scope_fp,
            member_ids_json=json.dumps(member_ids, separators=(",", ":")),
            text=text,
            event_json=event_json,
            occurred_at=members[0].occurred_at,
            received_at=members[-1].received_at,
            state=STATE_FROZEN,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        # The primary key is the dedupe: a re-freeze of the same burst inserts
        # nothing, so a crash between freeze and submit cannot double-send.
        .on_conflict_do_nothing(index_elements=["account_id", "fan_id", "turn_id"])
    )
    if not (inserted.rowcount or 0):
        # The turn already exists: a replay, a restart, or two workers computing
        # the same burst. Re-assert the input states for members that really do
        # belong to THIS turn (an idempotent repair), and create nothing.
        await _reassign_known_members(session, members, turn_id)
        report.already_frozen += 1
        return

    for position, member in enumerate(members):
        claimed = await session.execute(
            sqlite_insert(TwinBridgeTurnMember)
            .values(
                account_id=member.account_id,
                fan_id=member.fan_id,
                provider_message_id=member.provider_message_id,
                turn_id=turn_id,
                position=position,
                created_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["account_id", "fan_id", "provider_message_id"]
            )
        )
        if not (claimed.rowcount or 0):
            # This input is already a member of a DIFFERENT turn (a newer burst
            # closed first). Abandon this freeze entirely: the transaction rolls
            # back, so the turn row never exists without all of its members.
            raise _MembershipTaken(turn_id)
        await session.execute(
            update(TwinBridgeInput)
            .where(
                TwinBridgeInput.account_id == member.account_id,
                TwinBridgeInput.fan_id == member.fan_id,
                TwinBridgeInput.provider_message_id == member.provider_message_id,
            )
            .values(state=INPUT_ASSIGNED)
        )

    report.turns_frozen += 1
    report.inputs_assigned += len(members)
    report.turn_ids.append(turn_id)


class _MembershipTaken(Exception):
    """Internal: an input was already claimed by another turn; roll back."""


async def _reassign_known_members(session, members: list[_Input], turn_id: str) -> None:
    """Mark inputs assigned when the membership row already names this turn.

    Guarded by the membership row itself, so a replay can never steal an input
    that belongs to a different turn.
    """
    for member in members:
        row = (
            await session.execute(
                select(TwinBridgeTurnMember.turn_id).where(
                    TwinBridgeTurnMember.account_id == member.account_id,
                    TwinBridgeTurnMember.fan_id == member.fan_id,
                    TwinBridgeTurnMember.provider_message_id == member.provider_message_id,
                )
            )
        ).scalars().first()
        if row != turn_id:
            continue
        await session.execute(
            update(TwinBridgeInput)
            .where(
                TwinBridgeInput.account_id == member.account_id,
                TwinBridgeInput.fan_id == member.fan_id,
                TwinBridgeInput.provider_message_id == member.provider_message_id,
                TwinBridgeInput.state != INPUT_ASSIGNED,
            )
            .values(state=INPUT_ASSIGNED)
        )


async def _park(session, row: _Input, reason: str) -> None:
    await session.execute(
        update(TwinBridgeInput)
        .where(
            TwinBridgeInput.account_id == row.account_id,
            TwinBridgeInput.fan_id == row.fan_id,
            TwinBridgeInput.provider_message_id == row.provider_message_id,
        )
        .values(state=INPUT_PARKED, )
    )
    log.info("twin bridge input parked: account=%s fan=%s msg=%s reason=%s",
             row.account_id, row.fan_id, row.provider_message_id, reason)


async def load_turn(session, account_id: str, fan_id: int, turn_id: str) -> TwinBridgeTurn | None:
    return await session.get(TwinBridgeTurn, (account_id, int(fan_id), turn_id))


async def member_ids(turn: TwinBridgeTurn) -> list[int]:
    return [int(v) for v in json.loads(turn.member_ids_json or "[]")]


def next_action_at(now: datetime, *, after_s: float) -> datetime:
    return now + timedelta(seconds=after_s)
