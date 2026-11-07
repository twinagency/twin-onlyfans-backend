"""Conservative mapping of documented message-send error fields.

Provider docs: https://docs.onlyfansapi.com/introduction/essentials/idempotency
This module does not send requests, infer acceptance from a success status, or
resolve earlier attempts. Durable history is evaluated separately.
"""
from conversation_engine.recovery import Outcome


def classify_send_error(status: int | None, body: object) -> Outcome:
    if type(status) is not int or type(body) is not dict:
        return Outcome.UNKNOWN
    error = body.get("error")
    request_outcome = body.get("request_outcome")
    if status == 422 and error == "IDEMPOTENCY_KEY_MISMATCH":
        return Outcome.KEY_MISMATCH
    if status == 409 and error == "IDEMPOTENCY_CONFLICT":
        return Outcome.CONFLICT
    if status == 503 and error == "IDEMPOTENCY_UNAVAILABLE" and request_outcome in (None, "not_sent"):
        return Outcome.NOT_SENT
    if status == 503 and error == "SERVICE_UNAVAILABLE" and request_outcome == "not_sent":
        return Outcome.NOT_SENT
    return Outcome.UNKNOWN
