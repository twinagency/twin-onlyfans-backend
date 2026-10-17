"""The refusal vocabulary.

Four kinds of "no", kept apart because the dashboard renders them differently
and because collapsing them is how an outage starts reading as "this creator
has no accounts":

  • `Unauthorized`  — no credential, or a credential that is not this
                      service's. Fail closed; never a fallback.
  • `BadRequest`    — the request itself is malformed (missing version, bad
                      cursor, bad id shape). The caller can fix it.
  • `Refused`       — the request is well-formed and this service will not
                      answer it: an account that is not bound to this tenant,
                      an unknown account, a conversation from another account.
                      404/403, and never an empty success.
  • `Unavailable`   — there is nothing to read from: no service token
                      configured, or storage that cannot be reached. 503.

`Refused` carries an `expose_code` flag rather than a message, because the
detail of *why* a record was refused is itself information. An unknown account
and another tenant's account are the same answer to the caller: the service
does not say which one it was.
"""

from __future__ import annotations


class OnlyFansReadError(Exception):
    """Base for every refusal this package can produce."""

    status: int = 500
    state: str = "error"
    code: str = "error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def as_body(self, *, expose: bool) -> dict:
        return {
            "ok": False,
            "state": self.state,
            "code": self.code,
            "error": self.message if expose else "That request could not be answered.",
        }


class Unauthorized(OnlyFansReadError):
    status = 401
    state = "denied"
    code = "unauthorized"

    def __init__(self) -> None:
        super().__init__("This service requires its own credential.")


class BadRequest(OnlyFansReadError):
    status = 400
    state = "error"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Refused(OnlyFansReadError):
    status = 404
    state = "denied"

    def __init__(self, code: str = "not_available") -> None:
        # Deliberately one sentence for every cause. See the module docstring.
        super().__init__("That record is not available to this service credential.")
        self.code = code


class Unavailable(OnlyFansReadError):
    status = 503
    state = "unavailable"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
