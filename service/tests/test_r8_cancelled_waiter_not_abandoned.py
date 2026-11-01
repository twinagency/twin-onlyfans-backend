"""R8: a reply queued at the send gate when the credential changes is released and answered, not abandoned.

Measured before the fix (auth failure + reconnect, gap mode, 20 fans): 2 fans were marked failed ("native-turn-failed") and never
answered. The cancel left through SendGate.acquire, which does not set the gateway's stopped_reason."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fastt_r8_load as L  # noqa: E402


def test_auth_failure_then_reconnect_answers_every_fan_exactly_once():
    r = L.run_one("auth_failure", "gap")
    assert r["stranded_due_turns"] == 0 and r["answered_turns"] == r["eligible_turns"] == 20, r
    assert r["duplicate_sends"] == r["cross_account_sends"] == 0 and r["stale_credential_posts"] == 0
    assert r["retries"]["turn-abandoned"] == 0, r["retries"]
