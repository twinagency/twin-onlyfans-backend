"""R8: per-turn timing and per-route model accounting. Pure and additive: it only observes.

The user-visible clock starts at the EARLIEST unresolved qualifying fan input (``first_input_at``: the service's
``priority``, the first unanswered message of the burst) and ends at the first SUBSTANTIVE outcome (first sent
bubble, or the no-reply / hold outcome). Filler and later bubbles do not restart it, and a newer message in the
same burst does not move it. One ``turn-timing`` event is emitted per executed turn from data that already exists:
the service's claim time, the model adapter's own ``model-*`` events (calls, tokens, latency, truncation, cost) and
the ledger receipts (``created_at`` = admission/decision, ``updated_at`` = provider acceptance). No schema change,
no second queue, no send authority.
"""
from __future__ import annotations

from datetime import datetime


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return str(value)


def _secs(a, b):
    try:
        x = datetime.fromisoformat(str(a)) if not isinstance(a, datetime) else a
        y = datetime.fromisoformat(str(b)) if not isinstance(b, datetime) else b
        return round((y - x).total_seconds(), 3)
    except (TypeError, ValueError):
        return None


class TurnTiming:
    """Collects the model adapter's metadata events for one turn and builds the final timing record."""

    def __init__(self, forward=None):
        self.forward = forward
        self.calls = 0
        self.failed = 0
        self.truncated = 0
        self.latency_ms = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.purposes = {}
        self.first_model_start = None
        self.last_model_end = None

    def on_event(self, event, now=None):
        name = event.get("event")
        if name == "model-start":
            self.calls += 1
            self.first_model_start = self.first_model_start or now
        elif name == "model-complete":
            self.latency_ms += int(event.get("latency_ms") or 0)
            self.tokens_in += int(event.get("prompt_tokens") or 0)
            self.tokens_out += int(event.get("completion_tokens") or 0)
            self.cost_usd += float(event.get("cost_usd") or 0.0)
            purpose = event.get("purpose") or "unknown"
            self.purposes[purpose] = self.purposes.get(purpose, 0) + 1
            self.last_model_end = now
        elif name == "model-failed":
            self.failed += 1
            self.last_model_end = now
            if event.get("finish_reason") == "length" or event.get("code") == "model-output-incomplete":
                self.truncated += 1
        if self.forward is not None:
            self.forward(event)

    def record(self, *, account, fan, first_input_at, claimed_at, generation_started_at, generation_done_at,
               finished_at, receipts, outcome, requeues=0):
        sent = sorted((r for r in receipts if r.get("state") == "sent"), key=lambda r: r["updated_at"])
        decision_at = min((r["created_at"] for r in receipts), default=None)
        first_send = sent[0]["updated_at"] if sent else None
        last_send = sent[-1]["updated_at"] if sent else None
        end = first_send or finished_at
        return {
            "event": "turn-timing", "account": account, "fan_id": fan, "outcome": outcome,
            "first_input_at": _iso(first_input_at), "claimed_at": _iso(claimed_at),
            "generation_started_at": _iso(generation_started_at), "generation_done_at": _iso(generation_done_at),
            "decision_at": _iso(decision_at), "provider_first_send_at": _iso(first_send),
            "provider_last_send_at": _iso(last_send), "reconciled_at": _iso(finished_at),
            "user_visible_s": _secs(first_input_at, end) if first_input_at else None,
            "queue_before_claim_s": _secs(first_input_at, claimed_at) if first_input_at else None,
            "generation_s": _secs(generation_started_at, generation_done_at),
            "decision_to_send_s": _secs(decision_at, first_send) if decision_at and first_send else None,
            "bubbles_sent": len(sent), "requeues": requeues,
            "model": {"calls": self.calls, "failed": self.failed, "truncated": self.truncated,
                      "latency_ms": self.latency_ms, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                      "cost_usd": round(self.cost_usd, 6), "purposes": dict(sorted(self.purposes.items()))},
        }
