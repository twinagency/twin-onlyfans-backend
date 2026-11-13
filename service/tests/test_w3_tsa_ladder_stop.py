"""W3 trace-send-authority W3TSA-02 (red at a2f1dda6, review-only; no engine change).

REVIEW inv.4 hold 3: runtime_stop.STRONGER ({'of_restricted','ladder_stop'}) is a stop stronger than a Twin pause.
Every send-time reader must honour it. conversation_policy.SKIP_STOP and conversation_engine.controls.reason omit
'ladder_stop', so an automated fan with a ladder_stop skip_list row is allowed to receive automated/campaign sends.
Pure checks only: no DB, no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from onlyfans_runtime import conversation_policy as policy
from onlyfans_runtime import runtime_stop


def _facts(skip_reason):
    return policy.Facts(mode="automated", takeover_state="none", muted=False, skip_reason=skip_reason,
                        unresolved=False, critical_flag=False, cases=frozenset())


def test_every_stronger_stop_is_a_send_stop():
    missing = runtime_stop.STRONGER - policy.SKIP_STOP
    assert not missing, f"stronger runtime stops not honoured at send time: {sorted(missing)}"


def test_ladder_stop_denies_automated_and_campaign_sends():
    for origin in ("automated", "campaign", "engagement"):
        decision = policy.decide(_facts("ladder_stop"), origin)
        assert not decision.allowed, f"{origin} send allowed for a ladder_stop fan"


def test_control_reason_set_matches_runtime_stop():
    # controls.reason (engine sends) keeps its own literal set; it must cover every stronger stop.
    src = (Path(__file__).resolve().parents[1] / "conversation_engine" / "controls.py").read_text()
    assert "ladder_stop" in src or "runtime_stop" in src, "controls.reason ignores ladder_stop / runtime_stop.STRONGER"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
