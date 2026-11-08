"""Safe SQL expression for bounded recovery of authenticated webhook envelopes."""
from sqlalchemy import func

ACCOUNT_EVENTS = ('accounts.connected','accounts.reconnected','accounts.disconnected',
                  'accounts.session_expired','accounts.authentication_failed')


def event_fan(event_type, raw_body):
    # The schema-owned function catches malformed JSON and applies the same int64 bounds as intake.
    return func.of_engine_pilot_event_fan(raw_body, event_type)
