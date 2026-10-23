"""Event-fed cancellation of not-yet-sent proactive actions.

Called by the real shared writers INSIDE their existing transaction, after their
own state change, while they already hold the common runtime lock (runtime ->
binding -> conversation). No lock is taken here: only Action rows (and a read of
the linked intent) are touched, after every shared lock.

Best effort by design: INTEGRATION.md item 5 says dispatch-time validation
(`EngagementService.current` / `_validate`) stays authoritative, so a missing or
failed notification can only delay cancellation, never permit a stale send.
Hence the hook never raises into a writer. It never sends, never touches
ownership/revenue, and never marks work that may already be on the wire:
only `scheduled` actions and `queued` actions whose intent is still `queued`
are cancelled, using EngagementService's own cancel semantics.
"""
import logging

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_models import OfRuntime
from .contracts import load_object
from .models import Action

log = logging.getLogger(__name__)
ACTOR = "runtime-event"
CANCELLABLE = ("scheduled", "queued")  # dispatching/unknown may be past the wire marker


def _service_cancel(s, row, now, reason):
    # Lazy: service imports store/text_delivery, which import the writers that call us.
    from .service import EngagementService
    intent = s.get(OfTextIntent, (row.account_id, row.intent_id)) if row.intent_id else None
    if row.cancel_requested_at is not None or (intent is not None and intent.status != "queued"):
        return False
    EngagementService._cancel(s, row, now, ACTOR, reason)
    return True


def _cancel_rows(s, rows, now, reason):
    count = 0
    for row in rows:
        try:
            with s.begin_nested():
                count += _service_cancel(s, row, now, reason)
        except (SQLAlchemyError, KeyError, TypeError, ValueError):
            log.warning("engagement event cancel skipped; dispatch validation still applies", exc_info=True)
    return count


def _now(s, now):
    if now is not None:
        return now
    from sqlalchemy import text
    return s.execute(text("SELECT timezone('utc', now())")).scalar()


def cancel_for_fan(s, account_id, fan_id, reason, *, now=None):
    """Cancel this fan's not-yet-sent engagement action. One indexed read when idle."""
    if fan_id is None:
        return 0
    try:
        rows = s.scalars(select(Action).where(Action.account_id == account_id, Action.fan_id == fan_id,
                                              Action.binding_id == select(OfRuntime.binding_id).where(
                                                  OfRuntime.account_id == account_id).scalar_subquery(),
                                              Action.status.in_(CANCELLABLE))).all()
    except SQLAlchemyError:
        log.warning("engagement event lookup failed; dispatch validation still applies", exc_info=True)
        return 0
    return _cancel_rows(s, rows, _now(s, now), reason) if rows else 0


def _item_ids(row):
    data = load_object(row.snapshot_json)
    ids = set()
    for key in ("terms", "source"):
        part = data.get(key)
        if isinstance(part, dict):
            item = part.get("item")
            ids.add(item["id"] if isinstance(item, dict) else item)
    return ids


def cancel_for_item(s, account_id, item_id, reason="catalogue_changed", *, now=None):
    """Cancel active offer/follow-up actions whose frozen terms or source use this item."""
    try:
        rows = s.scalars(select(Action).where(Action.account_id == account_id, Action.status.in_(CANCELLABLE),
                                              Action.binding_id == select(OfRuntime.binding_id).where(
                                                  OfRuntime.account_id == account_id).scalar_subquery(),
                                              Action.kind.in_(("offer", "follow_up")))).all()
    except SQLAlchemyError:
        log.warning("engagement event lookup failed; dispatch validation still applies", exc_info=True)
        return 0
    if not rows:
        return 0
    hit = []
    for row in rows:
        try:
            if item_id in _item_ids(row):
                hit.append(row)
        except (KeyError, TypeError, ValueError):
            continue
    return _cancel_rows(s, hit, _now(s, now), reason) if hit else 0
