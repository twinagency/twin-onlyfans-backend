"""Durable admission of one eligible inbound DM into the bridge intake table.

The ingestion hook and the reconciliation both call
`enqueue_inbound_if_enabled`, so there is exactly one place that decides
eligibility, builds the frozen event and inserts the row. The only difference
between them is where the ORIGINAL provider frame comes from: the hook still has
the live frame; the reconciliation re-parses `messages.raw_json`.

Contract with the caller (the transcoder's auto-committing `get_session()`):

* Nothing here commits, opens a second session, launches a task or makes an
  HTTP call. The insert joins the caller's transaction, so a rollback between
  the canonical message writes and this insert leaves NEITHER record.
* A database failure is never swallowed: it propagates, the caller's session
  rolls back, and no caller can mistake a half-written intake for success.
* Disabled scope returns immediately without touching the database, so ordinary
  ingestion is unchanged when the bridge is off.
* Timestamps are read as TEXT. Materialising `messages.created_at` through the
  ORM's DateTime processor is what aborts a whole page on one dirty cell, and it
  is never needed here - the provider instant comes from the raw frame.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, type_coerce
from sqlalchemy import String as SAString
from .store import dialect_insert

from db.models import Message
from db.onlyfans_pilot_models import TwinBridgeInput

from . import contract
from .errors import IntakeConflictError
from .normalize import (
    build_candidate,
    build_event,
    eligibility_problem,
    frame_evidence,
    frame_evidence_from_raw_json,
)
from .scope import BridgeScope, SCOPE_VERSION, load_scope

log = logging.getLogger("of-relay.twin_bridge.intake")

# Outcomes of one admission attempt (fixed strings; no fan data).
DISABLED = "disabled"
INELIGIBLE = "ineligible"
MISSING = "missing-message"
ADMITTED = "admitted"
DUPLICATE = "duplicate"

_PK = ["account_id", "fan_id", "provider_message_id"]


@dataclass(frozen=True)
class RowView:
    """The canonical columns the bridge decides on, with the receipt as TEXT.

    `ingested_at` stays a raw string so one malformed cell cannot abort a page,
    and so a cursor can always be built from it.
    """

    account_id: str
    fan_id: int
    message_id: int
    direction: str
    body: str
    media_ids: object
    media_count: object
    price_cents: object
    is_paid: object
    is_tip: object
    is_unsent: object
    ingested_at: object
    raw_json: object


# One definition of the columns, shared by the hook's lookup and the
# reconciliation's page so the two can never read different data.
ROW_COLUMNS = (
    Message.account_id,
    Message.fan_id,
    Message.message_id,
    Message.direction,
    Message.body,
    Message.media_ids,
    Message.media_count,
    Message.price_cents,
    Message.is_paid,
    Message.is_tip,
    Message.is_unsent,
    type_coerce(Message.ingested_at, SAString).label("ingested_at"),
    Message.raw_json,
)


def row_view_from(row: Any) -> RowView:
    """Wrap a selected row (or any row-like object) as a RowView."""
    return RowView(
        account_id=str(row.account_id),
        fan_id=int(row.fan_id),
        message_id=int(row.message_id),
        direction=row.direction,
        body=row.body,
        media_ids=row.media_ids,
        media_count=row.media_count,
        price_cents=row.price_cents,
        is_paid=row.is_paid,
        is_tip=row.is_tip,
        is_unsent=row.is_unsent,
        ingested_at=row.ingested_at,
        raw_json=row.raw_json,
    )


async def fetch_row_view(
    session: Any, account_id: object, fan_id: object, message_id: object
) -> RowView | None:
    """Read one message's decision columns without the ORM DateTime processor."""
    stmt = (
        select(*ROW_COLUMNS)
        .where(
            Message.account_id == str(account_id),
            Message.fan_id == int(fan_id),
            Message.message_id == int(message_id),
        )
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    return row_view_from(row) if row is not None else None


def serialize_event(event: dict) -> str:
    """Deterministic, stable serialization so a duplicate can be compared."""
    return json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def enqueue_inbound_if_enabled(
    session: Any,
    *,
    account_id: object,
    fan_id: object,
    message_id: object,
    frame: object | None = None,
    row: RowView | None = None,
    scope: BridgeScope | None = None,
) -> tuple[str, str | None]:
    """Record one inbound DM as pending bridge work, inside the caller's session.

    `frame` is the ORIGINAL provider frame when the caller still has it (the
    ingestion hook). When it does not (reconciliation), the frame is re-read
    from `messages.raw_json`. A row with no readable evidence is refused rather
    than guessed at.

    Returns `(outcome, reason)`:

    * `("disabled", None)`    - no trusted scope; nothing was read or written.
    * `("ineligible", why)`   - a fixed reason; nothing was written.
    * `("missing-message", None)` - the canonical row is not there.
    * `("admitted", None)`    - a new pending row joined the transaction.
    * `("duplicate", None)`   - an identical snapshot already existed.

    Raises `IntakeConflictError` when the same identity already holds a
    DIFFERENT snapshot: the existing row is never overwritten or reinterpreted
    as new work.
    """
    active = scope if scope is not None else load_scope()
    if not active.enabled:
        return DISABLED, None

    view = row if row is not None else await fetch_row_view(session, account_id, fan_id, message_id)
    if view is None:
        return MISSING, None

    evidence = (
        frame_evidence(frame)
        if frame is not None
        else frame_evidence_from_raw_json(view.raw_json)
    )
    candidate = build_candidate(view, evidence=evidence)
    if candidate is None:
        return INELIGIBLE, "unusable-identity"

    problem = eligibility_problem(candidate, active)
    if problem is not None:
        return INELIGIBLE, problem

    event = build_event(candidate, active)
    event_json = serialize_event(event)
    if len(event_json.encode("utf-8")) > contract.MAX_TOTAL_BYTES:
        return INELIGIBLE, "oversized-event"

    insert_stmt = (
        dialect_insert(TwinBridgeInput)
        .values(
            account_id=candidate.account_id,
            fan_id=candidate.fan_id,
            provider_message_id=candidate.message_id,
            creator_slug=active.creator_slug,
            activation_cutoff=active.activation_cutoff,
            bridge_scope_version=SCOPE_VERSION,
            event_schema_version=contract.SCHEMA_VERSION,
            conversation_id=event["conversationId"],
            normalized_text=event["text"],
            event_json=event_json,
            occurred_at=contract.parse_canonical_timestamp(event["occurredAt"], "occurredAt"),
            received_at=contract.parse_canonical_timestamp(event["receivedAt"], "receivedAt"),
            state="pending",
        )
        # The database constraint - not a preliminary SELECT - is what makes a
        # duplicate or a racing admission safe.
        .on_conflict_do_nothing(index_elements=_PK)
    )
    result = await session.execute(insert_stmt)
    if result.rowcount:
        return ADMITTED, None

    # The insert did nothing: the identity already exists. Compare the frozen
    # snapshot; identical is a plain duplicate, different is a conflict.
    existing = await session.get(
        TwinBridgeInput, (candidate.account_id, candidate.fan_id, candidate.message_id)
    )
    if existing is not None and existing.event_json == event_json:
        return DUPLICATE, None
    raise IntakeConflictError(
        f"bridge input {candidate.account_id}/{candidate.fan_id}/{candidate.message_id} "
        "already exists with a different normalized event"
    )
