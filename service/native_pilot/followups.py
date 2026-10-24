"""FASTT's own delayed follow-ups for the native runner: the stock re-engage nudge, the discounted resend and the
post-purchase upsell.

ai_chatter schedules ONE ``nudge_fan_ids`` job after an unbought paid offer
(``_runtime_enqueue(account, "ai_chatter", payload={"nudge_fan_ids": [fan]}, run_at=...)``) and
runs it later through ``run()`` -> ``_run_nudge``. In the native runner those jobs are never handed to
the legacy ``scheduled_jobs`` worker. With ``--followups`` the engine's ``enqueue_job`` persists that
one job kind to the runner-owned ``native_pilot_followups`` table (see ``ledger.py``) and the
service executes it when due through the same admission, cap and hold checks as a normal turn.

Two more job kinds ride the same table, both keyed to ONE content offer and both queued only by the runner's own
post-sale scan (``postsale.py``), never by ai_chatter: ``discount`` (``{"discount_offer": id}``, FASTT's
``_maybe_discount_resend`` on a still-unpaid vault card) and ``post_buy`` (``{"post_buy": id}``, FASTT's
``_run_post_buy`` after a ledger-confirmed unlock). Every other job kind (aftercare, rhythm resume, gen_info,
make_right, ...) stays suppressed.
"""
from __future__ import annotations

from datetime import datetime

from .ledger import Ledger, Refused, OFFER_PAYLOAD_KEY

NUDGE_KIND = "nudge"
DISCOUNT_KIND = "discount"
POST_BUY_KIND = "post_buy"


def _offer_id(payload, key):
    """The one positive int offer id of ``{key: id}``, else None."""
    if isinstance(payload, dict) and set(payload) == {key} and type(payload[key]) is int and payload[key] > 0:
        return payload[key]
    return None


def nudge_job(kind, payload, fan_id):
    """``"nudge"`` when this is exactly ai_chatter's re-engage job for ``fan_id``, else None."""
    if (kind == "ai_chatter" and isinstance(payload, dict) and set(payload) == {"nudge_fan_ids"}
            and isinstance(payload["nudge_fan_ids"], list) and len(payload["nudge_fan_ids"]) == 1
            and type(payload["nudge_fan_ids"][0]) is int and payload["nudge_fan_ids"][0] == fan_id):
        return NUDGE_KIND
    return None


def job_kind(kind, payload, fan_id):
    """The follow-up kind this ``(kind, payload)`` is exactly, else None. Offer ownership is proven later (queue + run)."""
    if kind != "ai_chatter":
        return None
    nudge = nudge_job(kind, payload, fan_id)
    if nudge:
        return nudge
    for job, key in OFFER_PAYLOAD_KEY.items():
        if _offer_id(payload, key) is not None:
            return job
    return None


def offer_of(job_kind_, payload):
    """The offer id a discount / post_buy payload names, else None."""
    key = OFFER_PAYLOAD_KEY.get(job_kind_)
    return _offer_id(payload, key) if key else None


def nudge_payload(fan_id):
    return {"nudge_fan_ids": [int(fan_id)]}


def job_payload(job):
    """The ai_chatter payload a stored follow-up row stands for (the row's own JSON for the offer-keyed kinds)."""
    import json
    if job["kind"] == NUDGE_KIND:
        return nudge_payload(job["fan_id"])
    return json.loads(job["payload_json"])


class FollowupQueue:
    """The engine's write handle: account-scoped, accepts only the nudge job (and, from the post-sale scan, the
    two offer-keyed jobs)."""

    def __init__(self, ledger: Ledger, account_id: str, clock):
        self.ledger, self.account_id, self.clock = ledger, account_id, clock

    def add(self, fan_id, kind, payload, run_at):
        """Job id when queued, None when the job kind is not one this runner executes."""
        job = job_kind(kind, payload, fan_id)
        if job is None or not isinstance(run_at, datetime):
            return None
        try:
            return self.ledger.followup_add(account_id=self.account_id, fan_id=fan_id, kind=job,
                                            payload=nudge_payload(fan_id) if job == NUDGE_KIND else payload,
                                            run_at=run_at, now=self.clock())
        except Refused:
            return None
