"""R8: a fan who keeps typing is still served in order of their EARLIEST unresolved input (virtual-time engine harness).

Before the fix the ready list followed each fan's newest message, so the chatty fan below (oldest need in the account)
waited ~1148 s behind 59 quieter fans; it must now start among the first. Also asserts the gap-mode physics the
capacity statement relies on (one send per 20 s) and that nothing was duplicated or stranded.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fastt_r8_load as L  # noqa: E402


def test_chatty_fan_with_the_oldest_need_is_not_served_last():
    r = L.run_one("chatty_oldest", "gap")
    first = r["chatty_fan_first_reply_s"][0]
    assert first < 180, first                                       # was 1147.7 s
    assert r["stranded_due_turns"] == 0 and r["duplicate_sends"] == 0 and r["cross_account_sends"] == 0
    assert r["rate_violations"] == 0                                # the 20 s account gap is untouched
    assert r["answered_turns"] == r["eligible_turns"]
