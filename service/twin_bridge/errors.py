"""Error types for the Twin bridge. Fixed, non-sensitive messages only."""

from __future__ import annotations


class TwinBridgeError(Exception):
    """Base class for every bridge error."""


class BridgeConfigError(TwinBridgeError):
    """Trusted scope or client configuration is present but unusable."""


class ContractError(TwinBridgeError):
    """A value does not satisfy Twin's accepted wire contract.

    Raised while building a candidate event and while validating a response, so
    a malformed value can never be admitted or mistaken for a ready draft.
    """


class IntakeConflictError(TwinBridgeError):
    """A duplicate identity arrived with a contradictory payload.

    The existing snapshot is never overwritten and the row is never reset or
    reinterpreted as new work; the caller gets this instead.
    """
