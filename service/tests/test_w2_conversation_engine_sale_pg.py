"""W2S-02 (base DOC-10): unreadable ownership evidence must read as UNKNOWN, never as unowned.

Adapted from sweep 22fbbe79 to the head API (no history_ownership/ownership_gate at head): the paid-media
evidence for a fan exists but cannot be parsed. `ownership.owned_media_in_session` used to swallow it
(`except (ValueError, TypeError): pass`), so the boundary `media_delivery.assert_unowned` cleared the sale.
Real PostgreSQL (throwaway), synthetic transport; no network. Declared bypass: one paid `messages` row with a
malformed `media_ids` payload inserted by ORM (stand-in for corrupt ingested PPV history). Synthetic ids only.
"""
import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from db.models import Message
from onlyfans_runtime.media_delivery import assert_unowned, catalog_terms
from onlyfans_runtime.store import Refused, utcnow
from test_engine_catalogue_pg import setup as engine_setup
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN


@pytest.mark.parametrize('raw', ['[21,', '{"21": true}', '"21"', '["x"]', ''])
def test_w2s02_unreadable_paid_media_evidence_is_unknown_ownership_not_clear(database, raw):
    store, authority, engine, model, red, blue = engine_setup(database)
    with Session(database[1]) as s, s.begin():
        s.add(Message(account_id=ACCOUNT, fan_id=FAN, message_id=990001, direction='out', media_ids=raw,
                      price_cents=1200, is_paid=True, created_at=utcnow()))
    with Session(database[1]) as s:
        snapshot = catalog_terms(s, ACCOUNT, blue, lock=False)
        assert snapshot['media_ids'] == [21, 22]                                   # fixture precondition
        with pytest.raises(Refused, match='ownership evidence unavailable'):
            assert_unowned(s, ACCOUNT, FAN, snapshot)
    store.close()


def test_w2s02_relay_resale_guard_keeps_blocking_valid_evidence_beside_a_corrupt_row(database, monkeypatch):
    """Relay mode is explicitly lenient (= base, gate before relay re-enable): one corrupt paid row drops only its
    own ids; a valid paid row for the same fan still makes resale_verdict block that media."""
    import ownership
    store, authority, engine, model, red, blue = engine_setup(database)
    with Session(database[1]) as s, s.begin():
        s.add_all([Message(account_id=ACCOUNT, fan_id=FAN, message_id=990002, direction='out', media_ids='[21,',
                           price_cents=1200, is_paid=True, created_at=utcnow()),
                   Message(account_id=ACCOUNT, fan_id=FAN, message_id=990003, direction='out', media_ids='[22]',
                           price_cents=1200, is_paid=True, created_at=utcnow())])
    relay = create_async_engine(make_url(database[0]).set(drivername='postgresql+asyncpg'))

    @asynccontextmanager
    async def session():
        async with AsyncSession(relay) as s:
            yield s
    monkeypatch.setattr(ownership, 'get_session', session)

    async def verdict():
        try:
            return await ownership.resale_verdict(ACCOUNT, FAN, [22])
        finally:
            await relay.dispose()
    result = asyncio.run(verdict())
    assert result.reason == ownership.RESALE_OWNED_VIDEO and result.owned_videos == [22], result
    store.close()
