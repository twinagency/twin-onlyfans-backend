"""The checked platform-send ledger and the last-instant dispatch gate.

Two rules shape this module:

* **Persist before the side effect.** The attempt row (and the send-budget
  reservation) is committed BEFORE the provider POST, so a crash on either side
  of the boundary is recoverable as "unknown" rather than silently treated as
  "nothing happened".
* **Uncertainty is not failure.** A missing/invalid provider id, a wrong
  recipient, a timeout or any ambiguous failure after the POST boundary becomes
  `delivery_unknown`: the conversation pauses, nothing is acknowledged, and
  nothing is automatically re-sent. Only a validated created-message object
  proves delivery.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.engine import get_session
from db.models import Message, TwinBridgeConversation, TwinBridgeSendAttempt, TwinBridgeTurn

from .coordination import (
    CONV_ACTIVE,
    CONV_ATTENTION,
    CONV_PAUSED,
    sql_dt,
    sqlite_path,
)
from .contract import parse_canonical_timestamp
from .errors import ContractError

log = logging.getLogger("of-relay.twin_bridge.delivery")

ATTEMPT_DISPATCHING = "dispatching"
ATTEMPT_SENT = "sent"
ATTEMPT_UNKNOWN = "delivery_unknown"
ATTEMPT_REFUSED = "refused"

# An attempt row younger than this is treated as a POSSIBLY LIVE dispatch and is
# never reclaimed as a crash. Only an attempt older than the bound is a genuine
# abandoned dispatch; a second `run_once` starting while the first is still
# posting must not tombstone a live send.
DISPATCH_STALE_S = 30.0

TURN_DRAFT_READY = "draft_ready"
TURN_DISPATCHING = "dispatching"
TURN_SENT = "sent"
TURN_UNKNOWN = "delivery_unknown"
TURN_DELIVERED = "delivered"
TURN_PARKED = "parked"


class BudgetExhausted(RuntimeError):
    """The pilot send budget is spent; nothing may dispatch."""


class AttemptExists(RuntimeError):
    """This attempt number is already recorded for the turn."""


class GateRefused(RuntimeError):
    """The last-instant dispatch gate refused. Carries a fixed reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Test/ops seam (mirrors `cli.ADAPTER_FACTORY`): invoked immediately after the
# admission transaction acquires its SQLite write ordering, BEFORE the
# canonical-source read. Tests hold the transaction open here to prove a
# competing write cannot commit inside the validation->admission window.
# Production leaves it None.
ADMISSION_ORDERING_SEAM = None


@dataclass(frozen=True)
class Attempt:
    id: int
    account_id: str
    fan_id: int
    turn_id: str
    attempt_no: int
    owner_token: str
    revision: int


async def begin_attempt(
    *, account_id: str, fan_id: int, turn_id: str, attempt_no: int, owner_token: str,
    revision: int, now: datetime, member_ids: list[int] | None = None,
) -> Attempt:
    """Reserve budget and record `dispatching` in ONE transaction, before the POST.

    The reservation is the durable cap: it is consumed even if the attempt ends
    unknown, so restarting a run cannot raise the effective limit.

    The budget reservation is CONDITIONAL on the durable authority the caller
    claims to hold - the conversation must still be active, still reserved, and
    still owned by exactly this `owner_token` at exactly this `revision`. A
    stale worker whose claim was replaced matches nothing and is refused
    (`GateRefused`) BEFORE any row is written or any POST is attempted, so the
    recorded owner/revision are validated, never merely stored.

    When `member_ids` is supplied, the frozen turn's CANONICAL source is
    validated under REAL SQLite write ordering: the admission connection takes an
    explicit `BEGIN IMMEDIATE` (the RESERVED lock) BEFORE that read, and holds it
    through the conditional budget reserve and the attempt insert, committing
    only at the end. A competing invalidation on another connection therefore
    cannot commit in the validation->admission window; a source invalidated
    before admission has no attempt row, no spent budget and no POST. No network
    call ever runs inside the transaction.
    """
    path = sqlite_path()
    if not path:
        # An ENABLED bridge must not admit dispatch without real coordination
        # ordering: the source check could not be serialized with admission.
        raise GateRefused("admission-storage-unavailable")
    try:
        con = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    except sqlite3.Error as exc:
        raise GateRefused("admission-storage-unavailable") from exc

    acct, fan, tid = str(account_id), int(fan_id), str(turn_id)
    now_s = sql_dt(now)
    attempt_id: int | None = None
    try:
        con.execute("BEGIN IMMEDIATE")
        if ADMISSION_ORDERING_SEAM is not None:
            ADMISSION_ORDERING_SEAM()

        # (1) canonical source validity - the FIRST read under the write lock.
        if member_ids:
            wanted = [int(m) for m in member_ids]
            marks = ",".join("?" for _ in wanted)
            rows = con.execute(
                f"SELECT message_id, direction, is_unsent, price_cents, is_tip "
                f"FROM messages WHERE account_id = ? AND fan_id = ? "
                f"AND message_id IN ({marks})",
                (acct, fan, *wanted),
            ).fetchall()
            invalid = len(rows) != len(wanted)
            if not invalid:
                for _mid, direction, is_unsent, price_cents, is_tip in rows:
                    if direction != "in" or bool(is_unsent) \
                            or int(price_cents or 0) != 0 or bool(is_tip):
                        invalid = True
                        break
            if invalid:
                con.execute("ROLLBACK")
                raise GateRefused("source-invalidated-before-admission")

        # (2) replaying a recorded attempt number must never spend another unit.
        existing = con.execute(
            "SELECT id FROM twin_bridge_send_attempts "
            "WHERE account_id = ? AND fan_id = ? AND turn_id = ? AND attempt_no = ?",
            (acct, fan, tid, int(attempt_no)),
        ).fetchone()
        if existing is not None:
            con.execute("ROLLBACK")
            raise AttemptExists(f"attempt {attempt_no} already recorded")

        # (3) the conditional budget + authority reservation.
        reserved = con.execute(
            "UPDATE twin_bridge_conversations "
            "SET send_budget_used = send_budget_used + 1, updated_at = ? "
            "WHERE account_id = ? AND fan_id = ? AND state = ? AND reserved = 1 "
            "AND owner_token = ? AND revision = ? "
            "AND (owner_expires_at IS NULL OR owner_expires_at > ?) "
            "AND send_budget_used < send_budget_limit",
            (now_s, acct, fan, CONV_ACTIVE, str(owner_token), int(revision), now_s),
        )
        if not (reserved.rowcount or 0):
            current = con.execute(
                "SELECT state, reserved, owner_token, revision, owner_expires_at "
                "FROM twin_bridge_conversations WHERE account_id = ? AND fan_id = ?",
                (acct, fan),
            ).fetchone()
            con.execute("ROLLBACK")
            if current is None:
                raise GateRefused("claim-lost-before-dispatch")
            _state, _reserved, _token, _revision, _expires = current
            if _token != str(owner_token) or int(_revision) != int(revision) \
                    or not bool(_reserved) or _state != CONV_ACTIVE:
                raise GateRefused("claim-lost-before-dispatch")
            if _expires is not None and str(_expires) <= now_s:
                raise GateRefused("authority-expired")
            raise BudgetExhausted("pilot send budget exhausted")

        # (4) record `dispatching` BEFORE the POST, inside the same transaction.
        inserted = con.execute(
            "INSERT INTO twin_bridge_send_attempts "
            "(account_id, fan_id, turn_id, attempt_no, state, owner_token, revision,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, fan_id, turn_id, attempt_no) DO NOTHING",
            (acct, fan, tid, int(attempt_no), ATTEMPT_DISPATCHING,
             str(owner_token), int(revision), now_s, now_s),
        )
        if not (inserted.rowcount or 0):
            con.execute("ROLLBACK")
            raise AttemptExists(f"attempt {attempt_no} already recorded")
        attempt_id = int(inserted.lastrowid)
        con.execute("COMMIT")
    except (GateRefused, AttemptExists, BudgetExhausted):
        raise
    except sqlite3.Error as exc:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise GateRefused("admission-storage-error") from exc
    finally:
        con.close()

    assert attempt_id is not None
    return Attempt(
        id=attempt_id, account_id=acct, fan_id=fan, turn_id=tid,
        attempt_no=int(attempt_no), owner_token=str(owner_token),
        revision=int(revision),
    )


async def mark_sent(
    *, attempt_id: int, provider_message_id: str, evidence: dict | None, now: datetime,
) -> None:
    """Record the exact provider id BEFORE ack/attribution bookkeeping runs."""
    async with get_session() as session:
        await session.execute(
            update(TwinBridgeSendAttempt)
            .where(TwinBridgeSendAttempt.id == int(attempt_id))
            .values(
                state=ATTEMPT_SENT,
                provider_message_id=str(provider_message_id),
                evidence_json=json.dumps(evidence or {}, sort_keys=True, separators=(",", ":")),
                updated_at=now,
            )
        )


async def mark_unknown(*, attempt_id: int, reason: str, now: datetime) -> None:
    async with get_session() as session:
        await session.execute(
            update(TwinBridgeSendAttempt)
            .where(TwinBridgeSendAttempt.id == int(attempt_id))
            .values(state=ATTEMPT_UNKNOWN, reason=reason, updated_at=now)
        )


async def mark_refused(*, attempt_id: int, reason: str, now: datetime) -> None:
    """A refusal we can prove happened BEFORE the message POST (no evidence kept
    as delivery). The budget reservation still stands - it was spent."""
    async with get_session() as session:
        await session.execute(
            update(TwinBridgeSendAttempt)
            .where(TwinBridgeSendAttempt.id == int(attempt_id))
            .values(state=ATTEMPT_REFUSED, reason=reason, updated_at=now)
        )


async def recover_dispatching(*, now: datetime, account_id: str | None = None,
                              stale_after_s: float = DISPATCH_STALE_S) -> list[tuple[str, int, str]]:
    """Recover the two interrupted-dispatch windows, bounded by an attempt age.

    * **Confirmed send, lost turn update** (attempt `sent`, turn still
      `dispatching`): reconstruct the turn as `sent` so the idempotent
      acknowledgement runs - and NO second provider POST is made. This is safe
      regardless of age: a `sent` attempt already proves the POST landed, and
      touching only the turn state cannot create a duplicate.
    * **Genuinely abandoned dispatch** (attempt still `dispatching`, older than
      `stale_after_s`): treat it as `delivery_unknown` and pause the
      conversation. A row younger than the bound may belong to a LIVE dispatch
      in another process, so it is left completely untouched.
    """
    recovered: list[tuple[str, int, str]] = []
    cutoff = now - timedelta(seconds=max(0.0, float(stale_after_s)))

    # Pass 1: reconstruct a turn from confirmed sent evidence (no provider POST).
    async with get_session() as session:
        sent_stmt = (
            select(TwinBridgeSendAttempt.account_id, TwinBridgeSendAttempt.fan_id,
                   TwinBridgeSendAttempt.turn_id)
            .join(
                TwinBridgeTurn,
                (TwinBridgeTurn.account_id == TwinBridgeSendAttempt.account_id)
                & (TwinBridgeTurn.fan_id == TwinBridgeSendAttempt.fan_id)
                & (TwinBridgeTurn.turn_id == TwinBridgeSendAttempt.turn_id),
            )
            .where(
                TwinBridgeSendAttempt.state == ATTEMPT_SENT,
                TwinBridgeTurn.state == TURN_DISPATCHING,
            )
        )
        if account_id is not None:
            sent_stmt = sent_stmt.where(TwinBridgeSendAttempt.account_id == str(account_id))
        for account, fan, turn in (await session.execute(sent_stmt)).all():
            res = await session.execute(
                update(TwinBridgeTurn)
                .where(
                    TwinBridgeTurn.account_id == account,
                    TwinBridgeTurn.fan_id == int(fan),
                    TwinBridgeTurn.turn_id == turn,
                    TwinBridgeTurn.state == TURN_DISPATCHING,
                )
                .values(state=TURN_SENT, reason="recovered-sent-evidence", updated_at=now)
            )
            if res.rowcount:
                recovered.append((str(account), int(fan), str(turn)))

    async with get_session() as session:
        stmt = select(TwinBridgeSendAttempt).where(
            TwinBridgeSendAttempt.state == ATTEMPT_DISPATCHING,
            TwinBridgeSendAttempt.updated_at <= cutoff,
        )
        if account_id is not None:
            stmt = stmt.where(TwinBridgeSendAttempt.account_id == str(account_id))
        rows = (await session.execute(stmt)).scalars().all()
        for row in rows:
            await session.execute(
                update(TwinBridgeSendAttempt)
                .where(
                    TwinBridgeSendAttempt.id == int(row.id),
                    TwinBridgeSendAttempt.state == ATTEMPT_DISPATCHING,
                )
                .values(state=ATTEMPT_UNKNOWN, reason="interrupted-before-evidence",
                        updated_at=now)
            )
            await session.execute(
                update(TwinBridgeTurn)
                .where(
                    TwinBridgeTurn.account_id == row.account_id,
                    TwinBridgeTurn.fan_id == int(row.fan_id),
                    TwinBridgeTurn.turn_id == row.turn_id,
                    TwinBridgeTurn.state == TURN_DISPATCHING,
                )
                .values(state=TURN_UNKNOWN, reason="interrupted-before-evidence",
                        owner_token=None, owner_expires_at=None, updated_at=now)
            )
            await session.execute(
                update(TwinBridgeConversation)
                .where(
                    TwinBridgeConversation.account_id == row.account_id,
                    TwinBridgeConversation.fan_id == int(row.fan_id),
                )
                .values(state=CONV_PAUSED, reason="delivery-unknown",
                        owner_token=None, owner_expires_at=None,
                        reserved=True,
                        revision=TwinBridgeConversation.revision + 1, updated_at=now)
            )
            recovered.append((row.account_id, int(row.fan_id), row.turn_id))
    for account, fan, turn in recovered:
        log.warning("twin bridge recovered interrupted dispatch: account=%s fan=%s turn=%s",
                    account, fan, turn)
    return recovered


async def known_provider_outbound_ids(account_id: str, fan_id: int, provider_ids: list[int]) -> set[int]:
    """Provider ids the bridge itself sent, so its own message is not a takeover."""
    if not provider_ids:
        return set()
    wanted = [str(int(v)) for v in provider_ids]
    async with get_session() as session:
        rows = (
            await session.execute(
                select(TwinBridgeSendAttempt.provider_message_id).where(
                    TwinBridgeSendAttempt.account_id == str(account_id),
                    TwinBridgeSendAttempt.fan_id == int(fan_id),
                    TwinBridgeSendAttempt.state == ATTEMPT_SENT,
                    TwinBridgeSendAttempt.provider_message_id.in_(wanted),
                )
            )
        ).scalars().all()
    return {int(v) for v in rows if v is not None}


async def count_attempts(account_id: str, fan_id: int, turn_id: str) -> int:
    """How many provider send attempts this turn already has on record."""
    async with get_session() as session:
        rows = (
            await session.execute(
                select(TwinBridgeSendAttempt.id).where(
                    TwinBridgeSendAttempt.account_id == str(account_id),
                    TwinBridgeSendAttempt.fan_id == int(fan_id),
                    TwinBridgeSendAttempt.turn_id == str(turn_id),
                )
            )
        ).all()
    return len(rows)


class HistoryUnreadable(RuntimeError):
    """A history page cannot be interpreted; the caller must refuse the draft."""


def external_outbound_ids(history_items: list[dict], known: set[int],
                          creator_id: object = None, fan_id: object = None,
                          *, since: datetime | None = None) -> list[int]:
    """Outbound provider messages in a history page that Fastt did NOT send.

    Anything outbound and not in `known` is a human/external send (the official
    site, another device, or an outside client), which must park the draft. A
    row whose direction cannot be read is AMBIGUOUS, so this refuses the whole
    page (raising `HistoryUnreadable`) rather than silently treating it as
    inbound - which would let an unread human send slip past the takeover gate.

    Author/direction is resolved against the BOUND creator identity: OF marks
    creator-owned rows with `isFromMe`, but a real payload may instead carry
    `fromUser.id` equal to the creator's own user id (exactly what
    `event_transcoder` uses to classify `out`), and the legacy sentinel `-1`
    also appears. All three are recognised as creator-authored.

    A supplied, readable author that is NEITHER the bound creator NOR the bound
    fan (`fan_id`) is unrelated/ambiguous history - not proof of an inbound fan
    message - so it is refused too, rather than assumed to be the fan.

    With a frozen source boundary, only a PROVABLY older creator message is
    historical context. Equal/newer timestamps still block; unreadable times
    refuse. Identity/direction validation always runs, including on old rows.
    """
    if since is not None and (not isinstance(since, datetime) or since.tzinfo is not None):
        raise HistoryUnreadable("history boundary must be stored UTC")
    if since is not None:
        # The shared parser truncates provider precision to milliseconds. Floor
        # the boundary too: a same-millisecond creator row must still block.
        since = since.replace(microsecond=(since.microsecond // 1000) * 1000)
    out: list[int] = []
    creator = str(creator_id) if creator_id is not None else None
    fan = str(fan_id) if fan_id is not None else None
    for item in history_items or []:
        if not isinstance(item, dict):
            raise HistoryUnreadable("history row is not an object")
        id_raw = item.get("id")
        try:
            mid = int(id_raw)
        except (TypeError, ValueError):
            raise HistoryUnreadable("history row has no readable id") from None
        from_user_raw = item.get("fromUser")
        if from_user_raw is not None and not isinstance(from_user_raw, dict):
            raise HistoryUnreadable("history row author is not an object")
        author_raw = from_user_raw.get("id") if isinstance(from_user_raw, dict) else None
        is_me_raw = item.get("isFromMe")
        if is_me_raw is not None and not isinstance(is_me_raw, bool):
            # A non-boolean direction (0/1, "false", null) is ambiguous.
            raise HistoryUnreadable("history row direction is not a boolean")

        # A supplied author must be a readable positive id, the legacy self
        # sentinel `-1`, or the BOUND creator's own id. Anything else (bool,
        # null, object, float, non-id string) is malformed.
        author_text: str | None = None
        author_is_legacy_self = False
        if author_raw is not None:
            if isinstance(author_raw, bool):
                raise HistoryUnreadable("history row author is malformed")
            if isinstance(author_raw, int) and author_raw == -1:
                author_is_legacy_self = True
                author_text = "-1"
            elif isinstance(author_raw, int):
                if author_raw <= 0:
                    raise HistoryUnreadable("history row author is malformed")
                author_text = str(author_raw)
            elif isinstance(author_raw, str):
                text = author_raw.strip()
                if text.isdigit():
                    value = int(text)
                    if value <= 0:
                        raise HistoryUnreadable("history row author is malformed")
                    author_text = str(value)
                elif creator is not None and text == creator:
                    author_text = creator
                else:
                    raise HistoryUnreadable("history row author is malformed")
            else:
                raise HistoryUnreadable("history row author is malformed")

        author_is_creator = (
            author_text is not None and creator is not None and author_text == creator
        )
        author_is_fan = (
            author_text is not None and fan is not None and author_text == fan
        )
        # Classify the supplied AUTHOR first, INDEPENDENT of direction, so a
        # contradictory author can never be excused by `isFromMe` or by a known
        # provider id: a familiar id does not make unrelated evidence valid.
        if author_text is None:
            author_kind: str | None = None
        elif author_is_legacy_self or author_is_creator:
            author_kind = "creator"
        elif author_is_fan:
            author_kind = "fan"
        else:
            raise HistoryUnreadable("history row author is not this conversation")

        if is_me_raw is True:
            if author_kind == "fan":
                raise HistoryUnreadable("history row direction contradicts its author")
            outbound = True
        elif is_me_raw is False:
            if author_kind == "creator":
                raise HistoryUnreadable("history row direction contradicts its author")
            outbound = False
        elif author_kind == "creator":
            outbound = True
        elif author_kind == "fan":
            outbound = False
        else:
            # Neither a boolean direction nor a readable author: ambiguous.
            raise HistoryUnreadable("history row has no readable direction")
        if outbound and mid not in known:
            if since is not None:
                try:
                    created_at = parse_canonical_timestamp(item.get("createdAt"), "createdAt")
                except ContractError:
                    raise HistoryUnreadable("history row has no usable provider time") from None
                if created_at < since:
                    continue
            out.append(mid)
    return out


async def pause_for_attention(*, account_id: str, fan_id: int, reason: str, now: datetime) -> None:
    async with get_session() as session:
        await session.execute(
            update(TwinBridgeConversation)
            .where(
                TwinBridgeConversation.account_id == str(account_id),
                TwinBridgeConversation.fan_id == int(fan_id),
            )
            .values(state=CONV_ATTENTION, reason=reason,
                    owner_token=None, owner_expires_at=None,
                    reserved=True,
                    revision=TwinBridgeConversation.revision + 1, updated_at=now)
        )
