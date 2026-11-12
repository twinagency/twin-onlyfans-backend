"""Readiness must not call a fan eligible while that fan has an unresolved send attempt.

Live defect: /readyz reported eligibleFans=2, heldFans=0 and unknownSends=1 at the same time. Real scheduler, real
worker, real FansApiRealTransport over a scripted httpx.MockTransport. Synthetic data only.
"""
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import OfConversationState
from db.onlyfans_delivery_models import OfTextIntent
from plan_c_worker import _work_counts
from test_engine_real_worker_pg import MockFans
from test_plan_c_model_retry_due_pg import (make_world, database, world, configured_draft, stopped_text,  # noqa: F401
                                           post, ACCOUNT, FAN, FAN2)


def refuse_fan(w, fan, status, body):
    """Provider answers one fan's chat POSTs with an unproven refusal; everything else is the normal fixture."""
    fans = w.w.fans
    def handler(request):
        if request.method == 'POST' and request.url.path.endswith(f'/chats/{fan}/messages'):
            fans.posts.append({'path': request.url.path, 'key': request.headers['Idempotency-Key']})
            return httpx.Response(status, json=body)
        return MockFans.handle(fans, request)
    fans.transport.handler = handler


def unknown_rows(w):
    with Session(w.w.database[1]) as s:
        return list(s.scalars(select(OfTextIntent).where(OfTextIntent.account_id == ACCOUNT, OfTextIntent.status == 'unknown')))


def test_fan_with_an_unresolved_attempt_is_held_not_eligible(make_world):
    w = make_world([])
    refuse_fan(w, FAN, 422, {'error': 'VALIDATION_FAILED', 'message': 'rejected'})
    post(w, 95001, fan=FAN)
    post(w, 95002, fan=FAN2)
    w.ticks(4)
    [row] = unknown_rows(w)
    assert row.fan_id == FAN
    with Session(w.w.database[1]) as s:
        assert s.get(OfConversationState, (ACCOUNT, FAN)).unresolved_attempt_id == row.attempt_id
        assert s.get(OfConversationState, (ACCOUNT, FAN2)).unresolved_attempt_id is None
    counts = _work_counts(w.pilot)
    assert counts['unknownSends'] == 1
    assert counts['eligibleFans'] == 1 and counts['heldFans'] == 1, counts
    assert counts['accountHold'] is False          # per-fan isolation is unchanged: the other fan keeps sending
    assert [p for p in w.w.fans.posts if f'/chats/{FAN2}/' in p['path']]


def test_both_fans_unresolved_means_none_eligible(make_world):
    w = make_world([])
    fans = w.w.fans
    def handler(request):
        if request.method == 'POST' and request.url.path.endswith('/messages'):
            return httpx.Response(503, json={'error': 'SERVICE_UNAVAILABLE', 'request_outcome': 'unknown'})
        return MockFans.handle(fans, request)
    fans.transport.handler = handler
    post(w, 95011, fan=FAN)
    post(w, 95012, fan=FAN2)
    w.ticks(4)
    counts = _work_counts(w.pilot)
    assert counts['unknownSends'] == 2
    assert counts['eligibleFans'] == 0 and counts['heldFans'] == 2, counts


def test_no_unresolved_attempt_still_counts_both_fans_eligible(make_world):
    w = make_world([])
    post(w, 95021, fan=FAN)
    post(w, 95022, fan=FAN2)
    w.ticks(4)
    counts = _work_counts(w.pilot)
    assert counts['unknownSends'] == 0 and counts['eligibleFans'] == 2 and counts['heldFans'] == 0, counts
