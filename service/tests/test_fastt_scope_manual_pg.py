"""Real PostgreSQL half of the sender/API db:* ownership agreement. Synthetic transport only."""
from sqlalchemy.orm import Session
from onlyfans_runtime import fastt_manual as manual, fastt_scope as fs
from test_fastt_controls_pg import database, template  # noqa: F401 -- pytest fixtures


def test_owned_fan_manual_send_goes_through_the_fastt_transport_only(database, monkeypatch):
    from test_fastt_controls_pg import command, live_like, native_policy, utcnow_iso
    from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured, prepare
    from onlyfans_runtime.fastt_manual import FasttManualTransport, dispatch_next_manual
    from onlyfans_runtime.text_delivery import SyntheticTextTransport
    from db.models import OfMessageDelivery
    _, engine = database
    store, authority = configured(database)
    live_like(engine)
    fs.reset_cache()
    monkeypatch.setenv(fs.SCOPE_ENV, "db:*")
    monkeypatch.setattr(fs, "_clock", lambda: 1000.0)
    monkeypatch.setattr(fs, "_discoverer", lambda: {ACCOUNT})
    monkeypatch.setattr(fs, "_reader", lambda ids: {i: None for i in ids})
    try:
        assert manual.fastt_owned(ACCOUNT, FAN) and manual.fastt_serves(ACCOUNT, FAN)
        command(store, "request_takeover", 0)
        native_policy(authority)
        intent = prepare(authority, key="platform-1", revision=1, body="platform human")
        wire = SyntheticTextTransport(ACCOUNT)                            # a second sender must not get it
        assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == "queued" and not wire.calls
        seen = []
        transport = FasttManualTransport(ACCOUNT, lambda g: seen.append((g.fan_id, g.body)) or
                                         {"id": 31337, "createdAt": utcnow_iso()}, policy_revision=2)
        assert dispatch_next_manual(authority, TENANT, ACCOUNT, transport) == "accepted"   # not queued forever
        assert seen == [(FAN, "platform human")]
        with Session(engine) as s:
            assert s.get(OfMessageDelivery, (ACCOUNT, FAN, 31337)).outcome == "sent"
        # An undiscovered account (revoked binding) is held, not handed to a second sender.
        fs.reset_cache()
        monkeypatch.setattr(fs, "_clock", lambda: 1000.0)
        monkeypatch.setattr(fs, "_discoverer", lambda: set())
        monkeypatch.setattr(fs, "_reader", lambda ids: {i: None for i in ids})
        assert manual.fastt_owned(ACCOUNT, FAN) and not manual.fastt_serves(ACCOUNT, FAN)
        late = prepare(authority, key="platform-2", revision=1, body="after revoke")
        assert authority.dispatch(TENANT, ACCOUNT, late, wire) == "queued" and not wire.calls
        assert dispatch_next_manual(authority, TENANT, ACCOUNT, transport) is None and len(seen) == 1
    finally:
        fs.reset_cache()
        store.close()

