"""Test-only helpers: an EXPLICIT operator approval step for 'fansapi' accounts.

These are used by tests that exercise delivery below the worker (engine.queue, TextAuthority). They are not part of
the application; nothing under service/ imports this module.
"""
from sqlalchemy.orm import Session

from conversation_engine import approval
from db.onlyfans_delivery_models import OfDeliveryPolicy

OPERATOR = 'test-operator'


def approve_draft(store, tenant, account, decision_id, actor=OPERATOR):
    """Stage and approve the exact planned draft, as a named operator would."""
    info = approval.create_draft_approval(store, tenant, account, decision_id)
    return approval.approve(store, tenant, account, info['requestId'], info['generation'],
                            expected_version=info['version'], actor=actor)


def queue_decision(engine, tenant, account, decision_id, actor=OPERATOR):
    """engine.queue; for a fansapi account the draft is first approved by `actor` (an explicit test step)."""
    with Session(engine.engine) as s:
        policy = s.get(OfDeliveryPolicy, account)
        gated = policy is not None and policy.transport_kind == approval.GATED_TRANSPORT
    if gated:
        approve_draft(engine.store, tenant, account, decision_id, actor)
    return engine.queue(tenant, account, decision_id)


def finished_backfill(db_engine, account, *fans):
    """A finished history/transaction backfill for these fans (offers to a real fan wait for one)."""
    import json
    import uuid
    from datetime import timedelta
    from db.onlyfans_fans_intake_models import OfFansBackfill
    from onlyfans_runtime.store import utcnow
    with Session(db_engine) as s, s.begin():
        now = utcnow()
        s.add(OfFansBackfill(account_id=account, run_id=str(uuid.uuid4()), reason='operator', start_date=now - timedelta(days=3650),
            status='complete', pages_used=1, pages_budget=1,
            marker=json.dumps({'phase': 'done', 'chats': [], 'done_chats': [str(f) for f in fans]}),
            rows_seen=0, rows_new=0, started_at=now, updated_at=now))
