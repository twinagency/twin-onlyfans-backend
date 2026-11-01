"""R8: the turn-timing record is derived only from existing evidence and keeps the user-visible clock at the earliest input."""
from datetime import datetime, timedelta

from native_pilot.turn_timing import TurnTiming

T0 = datetime(2026, 11, 1, 10, 0, 0)
at = lambda s: T0 + timedelta(seconds=s)  # noqa: E731


def test_clock_runs_from_earliest_input_to_first_substantive_send_not_later_bubbles():
    seen = []
    t = TurnTiming(forward=seen.append)
    for ev, when in (({"event": "model-start", "purpose": "chat"}, 12),
                     ({"event": "model-complete", "purpose": "chat", "latency_ms": 900, "prompt_tokens": 100,
                       "completion_tokens": 20, "cost_usd": 0.001}, 13),
                     ({"event": "model-failed", "code": "model-output-incomplete", "finish_reason": "length"}, 14)):
        t.on_event(ev, at(when))
    assert len(seen) == 3                                     # events are forwarded untouched
    receipts = [{"state": "sent", "created_at": at(14), "updated_at": at(40)},
                {"state": "sent", "created_at": at(41), "updated_at": at(62)},
                {"state": "not_sent", "created_at": at(13), "updated_at": at(13)}]
    rec = t.record(account="a", fan=1, first_input_at=at(0), claimed_at=at(10), generation_started_at=at(12),
                   generation_done_at=at(14), finished_at=at(63), receipts=receipts, outcome="sent", requeues=1)
    assert rec["user_visible_s"] == 40.0                      # first sent bubble, not the last one
    assert rec["queue_before_claim_s"] == 10.0 and rec["generation_s"] == 2.0
    assert rec["decision_to_send_s"] == 27.0 and rec["bubbles_sent"] == 2 and rec["requeues"] == 1
    assert rec["model"] == {"calls": 1, "failed": 1, "truncated": 1, "latency_ms": 900, "tokens_in": 100,
                            "tokens_out": 20, "cost_usd": 0.001, "purposes": {"chat": 1}}


def test_no_send_turn_ends_at_the_outcome_and_missing_input_is_not_invented():
    rec = TurnTiming().record(account="a", fan=1, first_input_at=None, claimed_at=at(1), generation_started_at=at(1),
                              generation_done_at=at(2), finished_at=at(3), receipts=[], outcome="no-reply")
    assert rec["user_visible_s"] is None and rec["provider_first_send_at"] is None and rec["bubbles_sent"] == 0
