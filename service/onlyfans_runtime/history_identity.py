"""Compare legacy provider HTML with native history without rewriting evidence."""
from datetime import datetime
import json


def same_body(existing, incoming):
    if existing.body == incoming:
        return True
    body = existing.body
    if not isinstance(body, str) or not body.startswith("<p>") or not body.endswith("</p>"):
        return False
    try:
        raw = json.loads(existing.raw_json or "{}")
        # Native canonical payloads already contain plain text. Literal markup
        # in such a message must never be reinterpreted as provider formatting.
        if not isinstance(raw, dict) or "body" in raw:
            return False
        if raw and (raw.get("text") != body or raw.get("id") != existing.message_id):
            return False
        from .native_protocol import history_text
        return history_text(body) == incoming
    except (ValueError, TypeError):
        return False


def same_message(existing, event, *, timestamp=True):
    try:
        return (existing.direction == event.direction
            and same_body(existing, event.body)
            and existing.price_cents == event.price_cents
            and json.loads(existing.media_ids or "[]") == list(event.media_ids)
            and (not timestamp or existing.created_at == datetime.fromisoformat(
                event.created_at.replace("Z", "+00:00")).replace(tzinfo=None)))
    except (ValueError, TypeError):
        return False


def reconcile_formatting_conflicts(store, tenant, account, *, after=None, limit=100):
    """A bounded, scoped repair of equivalent historical ingress receipts.

    Canonical messages, controls, claims, money and import cursors are retained.
    The original ingress payload remains the evidence; reason records the repair.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from db.models import Message
    from db.onlyfans_models import OfIngress
    from .store import Event, Refused, utcnow
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Refused("invalid formatting repair bound")
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        store._read_connection(s, runtime, None)
        query = select(OfIngress).where(OfIngress.account_id == account,
            OfIngress.binding_id == runtime.binding_id, OfIngress.status == "quarantined",
            OfIngress.reason == "conflicting_message_identity",
            OfIngress.source.in_(("catchup", "import", "rest")))
        if after is not None:
            query = query.where(OfIngress.event_key > after)
        rows = s.scalars(query.order_by(OfIngress.event_key).limit(limit).with_for_update()).all()
        repaired = 0
        for row in rows:
            event = Event(**json.loads(row.payload)).normalized()
            existing = s.get(Message, (account, event.fan_id, event.message_id))
            if existing is not None and existing.body != event.body and same_message(existing, event):
                row.status, row.reason, row.applied_at = "applied", "legacy_html_equivalent", utcnow()
                repaired += 1
        return {"examined": len(rows), "repaired": repaired,
            "after": rows[-1].event_key if len(rows) == limit else None}
