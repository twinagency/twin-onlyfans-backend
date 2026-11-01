"""R8 item 4: gap vs concurrent delivery on the same synthetic scenario (no production mode change).

Same seed, same fans, same model latency; only the account delivery mode differs. Asserts the properties that must hold in
BOTH modes (no duplicate, no cross-account send, per-conversation bubble order, nothing stranded, the 20 s gap untouched in
gap mode) and records that concurrent mode is faster. The synthetic provider allowance is NOT live provider capacity."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fastt_r8_load as L  # noqa: E402


def test_gap_and_concurrent_both_safe_and_concurrent_is_faster_on_multi_bubble():
    gap = L.run_one("multi_bubble", "gap")
    conc = L.run_one("multi_bubble", "concurrent")
    for r in (gap, conc):
        assert r["duplicate_sends"] == r["cross_account_sends"] == r["per_fan_order_violations"] == 0, r
        assert r["stranded_due_turns"] == 0 and r["answered_turns"] == r["eligible_turns"] == 40, r
        assert r["replies_sent"] == 80                                   # two bubbles per fan, none lost
    assert gap["rate_violations"] == 0                                    # one send per 20 s per account, intact
    assert conc["user_visible_s"]["p95"] < gap["user_visible_s"]["p95"] / 5
    assert conc["share_within_180s"] == 1.0 and gap["share_within_180s"] < 0.25
