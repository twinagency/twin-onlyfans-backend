"""Typed stops for the FASTT runner.

These derive from BaseException ON PURPOSE. ai_chatter wraps every send and every
per-fan body in `except Exception` (it quarantines the fan, counts an error and
moves on to the next bubble). A runner stop must unwind the whole run instead, so
nothing after it is sent, quarantined or retried by FASTT's own recovery code.
"""


class FasttStop(BaseException):
    """Base for runner stops; `reason` is a short non-secret code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class FasttTurnAbort(FasttStop):
    """This turn must not send (anything more). Nothing uncertain happened.

    Pause, takeover, a newer inbound, changed control/input revisions, a throttled
    or busy account, or an unsupported send form. The runner re-evaluates on new
    input; it never retries the same input revision.
    """


class FasttHalt(FasttStop):
    """The runner must stop and stay stopped until an operator reconciles.

    An unknown or held send, an unresolved delivery, a scope violation or a
    failed durable stop. Never replayed automatically.
    """
