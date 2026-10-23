"""Compatibility projection through the existing connection authority.

The authority owns scope checks and decryption. This callback returns metadata
only, constructs no provider or model client, and never falls back to a key.
"""
from . import connections
from .store import Refused


def personality_credential_status(session, account_id, binding_id):
    """Return the scoped four-field metadata, or None when unverified."""
    try:
        return connections.personality_credential_status(
            session, account_id, binding_id, _unavailable_as_none=True)
    except Refused:
        return None
