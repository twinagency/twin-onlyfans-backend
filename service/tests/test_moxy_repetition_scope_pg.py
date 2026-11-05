"""Repeated creator openings receive one targeted repair in the real path."""
from datetime import timedelta

from sqlalchemy.orm import Session

from db.models import Message
from onlyfans_runtime.store import utcnow
from test_moxy_integrated_pg import setup, decision, model
from test_moxy_reply_recovery_pg import add, correction, row, send
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT


def test_repeated_opening_is_repaired_without_changing_the_paid_card(database, monkeypatch):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        now = utcnow()
        with Session(engine) as session, session.begin():
            for index, text in enumerate(('ha, that sounds fun.', 'ha, tell me more.', 'ha, you are cheeky.')):
                session.add(Message(account_id=ACCOUNT, fan_id=101, message_id=300+index,
                    direction='out', body=text, created_at=now-timedelta(minutes=3-index)))
        identity = add(service, key='repeated-opening')
        service.prepare(TENANT, ACCOUNT, identity, model(decision(
            'offer', 'ha, here is the outfit photo.', 'first', sequence='fixture_level')))
        assert row(engine, identity).reason == 'repeated_recent_phrase'
        retry = correction(engine)
        service.prepare(TENANT, ACCOUNT, retry, model(decision(
            'offer', 'Here is the outfit photo.', 'first', sequence='fixture_level')))
        assert row(engine, retry).status == 'ready', row(engine, retry).reason
        result, wire, _ = send(service, authority, engine, retry)
        assert result == 'accepted' and len(wire.calls) == 1
        assert wire.calls[0].media_ids == (7101,) and wire.calls[0].price_cents == 7500
    finally:
        store.close()
