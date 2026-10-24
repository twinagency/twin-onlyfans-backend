"""FASTT's own delayed follow-ups (the stock re-engage nudge) for the native runner.

ai_chatter schedules ONE ``nudge_fan_ids`` job after an unbought paid offer
(``_runtime_enqueue(account, "ai_chatter", payload={"nudge_fan_ids": [fan]}, run_at=...)``) and
runs it later through ``run()`` -> ``_run_nudge``. In the native runner those jobs are never handed to
the legacy ``scheduled_jobs`` worker. With ``--followups`` the engine's ``enqueue_job`` persists that
one job kind to the runner-owned ``native_pilot_followups`` table (see ``ledger.py``) and the
service executes it when due through the same admission, cap and hold checks as a normal turn.
Every other job kind (post-buy, aftercare, rhythm resume, gen_info, make_right, ...) stays suppressed.
"""
from __future__ import annotations

from datetime import datetime

from .ledger import Ledger, Refused

NUDGE_KIND = "nudge"


def nudge_job(kind, payload, fan_id):
    """``"nudge"`` when this is exactly ai_chatter's re-engage job for ``fan_id``, else None."""
    if (kind == "ai_chatter" and isinstance(payload, dict) and set(payload) == {"nudge_fan_ids"}
            and isinstance(payload["nudge_fan_ids"], list) and len(payload["nudge_fan_ids"]) == 1
            and type(payload["nudge_fan_ids"][0]) is int and payload["nudge_fan_ids"][0] == fan_id):
        return NUDGE_KIND
    return None


def nudge_payload(fan_id):
    return {"nudge_fan_ids": [int(fan_id)]}


class FollowupQueue:
    """The engine's write handle: account-scoped, accepts only the nudge job."""

    def __init__(self, ledger: Ledger, account_id: str, clock):
        self.ledger, self.account_id, self.clock = ledger, account_id, clock

    def add(self, fan_id, kind, payload, run_at):
        """Job id when queued, None when the job kind is not one this runner executes."""
        job = nudge_job(kind, payload, fan_id)
        if job is None or not isinstance(run_at, datetime):
            return None
        try:
            return self.ledger.followup_add(account_id=self.account_id, fan_id=fan_id, kind=job,
                                            payload=nudge_payload(fan_id), run_at=run_at, now=self.clock())
        except Refused:
            return None
