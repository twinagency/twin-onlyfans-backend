"""Single-sender ownership: which runtime may send automated replies to a fan.

An active row in of_runtime_owner means the dedicated FASTT runner process is the
ONLY automated sender for that conversation. The native reactive planner
(Store.claim), proactive engagement, campaigns and TextAuthority's automated
origins all refuse an owned fan, and the 'fastt' origin is refused for any fan
that is NOT owned. Control state (pause / takeover / no_contact) stays in
of_conversation_state; ownership only says WHO sends while that state is
'automated'. Pausing never releases ownership and releasing never resumes AI.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Fan
from db.onlyfans_fastt_models import OfRuntimeOwner
from .store import Refused, _id, utcnow

OWNER = "fastt"


def active_owner(s, account_id, fan_id):
    """The owning runtime name, or None. Cheap indexed primary-key read."""
    row = s.get(OfRuntimeOwner, (account_id, fan_id))
    return row.owner if row is not None and row.released_at is None else None


def owned_by_fastt(s, account_id, fan_id):
    return active_owner(s, account_id, fan_id) == OWNER


def claim(store, tenant_id, account_id, fan_id, *, actor):
    """Mark the fan as FASTT-owned (idempotent) and cancel not-yet-sent engagement work."""
    _id(fan_id)
    if not isinstance(actor, str) or not 1 <= len(actor) <= 128:
        raise Refused("invalid owner actor")
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant_id, account_id)
        if s.get(Fan, (account_id, fan_id)) is None:
            raise Refused("conversation unavailable")
        now = utcnow()
        row = s.get(OfRuntimeOwner, (account_id, fan_id), with_for_update=True)
        if row is None:
            s.add(OfRuntimeOwner(account_id=account_id, fan_id=fan_id, owner=OWNER, actor=actor, claimed_at=now))
        elif row.released_at is not None:
            row.owner, row.actor, row.claimed_at, row.released_at, row.released_by = OWNER, actor, now, None, None
        s.flush()
        from .launch_engagement.events import cancel_for_fan
        cancel_for_fan(s, account_id, fan_id, "operator_cancelled", now=now)


def release(store, tenant_id, account_id, fan_id, *, actor):
    """Hand the conversation back to the native senders. Never changes control state."""
    _id(fan_id)
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant_id, account_id)
        row = s.get(OfRuntimeOwner, (account_id, fan_id), with_for_update=True)
        if row is not None and row.released_at is None:
            row.released_at, row.released_by = utcnow(), actor
