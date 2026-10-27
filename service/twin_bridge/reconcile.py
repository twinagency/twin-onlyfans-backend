"""Explicitly invoked, bounded reconciliation of missed intake rows.

Fastt's ingestion hook enqueues an input in the same transaction as the message
it came from. This module is the safety net for a committed `messages` row whose
intake entry is missing - for example a message persisted while the bridge was
disabled.

Properties the assignment requires, and where each one lives:

* Only the enabled scope, designated fans and the activation cutoffs are
  scanned. The SQL prefilter uses Fastt's OWN receipt column, which is generated
  by Fastt and therefore has no provider offset to lose. The provider boundary
  is applied in Python, from the verified `createdAt`, so a correctly converted
  provider time can never be discarded by SQL.
* A deterministic TOTAL order - `(ingested_at, fan_id, message_id)` - with a
  cursor made of the same three parts. `fan_id` is not unique on its own, so a
  partial sort key would not be a stable page boundary.
* Both times are compared as TEXT. That keeps pagination working when a cell
  holds something unparseable, and it never re-parses a dirty timestamp through
  the ORM's DateTime processor after selecting it safely.
* A finite `batch_size`; no unlimited sweep and nothing runs on a timer.
* Rows already carrying an intake entry are excluded from the page, so the scan
  makes progress past them and a repeated run admits nothing new.
* Admission goes through `enqueue_inbound_if_enabled`, so it shares the
  eligibility/normalization code, the immutable snapshot and the database unique
  constraint with the hook and is safe to race against it.

It starts from committed Fastt messages only. Fastt's upstream event queue can
drop an event before canonical persistence, and nothing here can recover a
message Fastt never stored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import and_, exists, or_, select, type_coerce
from sqlalchemy import String as SAString

from db.engine import get_session
from db.models import Message
from db.onlyfans_pilot_models import TwinBridgeInput

from .errors import IntakeConflictError
from .intake import (
    ADMITTED,
    DISABLED,
    DUPLICATE,
    INELIGIBLE,
    ROW_COLUMNS,
    enqueue_inbound_if_enabled,
    row_view_from,
)
from .scope import BridgeScope, load_scope

log = logging.getLogger("of-relay.twin_bridge.reconcile")

DEFAULT_BATCH_SIZE = 200
MAX_BATCH_SIZE = 500

# SQLAlchemy's SQLite DATETIME storage format. Comparing text in exactly this
# shape orders chronologically, which is what lets the cursor keep advancing
# even when one stored cell is unreadable.
_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


@dataclass
class ReconcileReport:
    """What one bounded page did. Counts only - never fan text."""

    scanned: int = 0
    admitted: int = 0
    duplicates: int = 0
    ineligible: int = 0
    conflicts: int = 0
    done: bool = False
    next_cursor: tuple[str, int, int] | None = None


async def reconcile_pending(
    *,
    scope: BridgeScope | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    after: tuple[str, int, int] | None = None,
) -> ReconcileReport:
    """Admit at most `batch_size` committed eligible inputs that are missing.

    Pass the returned `next_cursor` back in as `after` to continue. A `done`
    report means the page was short: there is nothing left at or after the
    cursor.
    """
    active = scope if scope is not None else load_scope()
    report = ReconcileReport()
    if not active.enabled or active.activation_cutoff is None:
        report.done = True
        return report

    size = max(1, min(int(batch_size), MAX_BATCH_SIZE))
    receipt_text = type_coerce(Message.ingested_at, SAString)
    cutoff_text = active.activation_cutoff.strftime(_STAMP_FORMAT)

    already = exists().where(
        and_(
            TwinBridgeInput.account_id == Message.account_id,
            TwinBridgeInput.fan_id == Message.fan_id,
            TwinBridgeInput.provider_message_id == Message.message_id,
        )
    )

    stmt = (
        select(*ROW_COLUMNS)
        .where(Message.account_id == active.account_id)
        .where(Message.direction == "in")
        .where(Message.fan_id.in_(list(active.fan_ids_for_sql())))
        # The receipt boundary, on Fastt's own column (see the module docstring).
        .where(receipt_text >= cutoff_text)
        .where(~already)
        .order_by(receipt_text, Message.fan_id, Message.message_id)
        .limit(size)
    )
    if after is not None:
        at, fan, message = after
        stmt = stmt.where(
            or_(
                receipt_text > at,
                and_(receipt_text == at, Message.fan_id > fan),
                and_(receipt_text == at, Message.fan_id == fan, Message.message_id > message),
            )
        )

    async with get_session() as session:
        rows = (await session.execute(stmt)).all()
        report.scanned = len(rows)
        for row in rows:
            # The cursor is always available as text, so a dirty timestamp
            # cannot strand the page or look like "no work left".
            report.next_cursor = (str(row.ingested_at), int(row.fan_id), int(row.message_id))
            try:
                outcome, _reason = await enqueue_inbound_if_enabled(
                    session,
                    account_id=row.account_id,
                    fan_id=row.fan_id,
                    message_id=row.message_id,
                    row=row_view_from(row),
                    scope=active,
                )
            except IntakeConflictError:
                # Contradictory same-identity payload: left exactly as it was.
                report.conflicts += 1
            else:
                if outcome == ADMITTED:
                    report.admitted += 1
                elif outcome == DUPLICATE:
                    report.duplicates += 1
                elif outcome in (INELIGIBLE, DISABLED):
                    report.ineligible += 1

    if report.scanned < size:
        report.done = True
    return report
