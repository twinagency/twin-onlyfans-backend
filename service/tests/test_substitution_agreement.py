"""F04: no materially different alternative is dispatched without stored agreement to THAT alternative."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from db.models import Base, Account, Fan, Message
from db.onlyfans_substitution_models import OfSubstitutionAgreement
from automations import pack_sender as packs, substitution_agreement as sa
from automations.content_contract import Contract, NO_MATCH, _empty
from automations.pack_claim import Claim

ACC, FAN = "of-test", 42


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "sub.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session():
        async with factory() as s:
            yield s
    for module in (packs, sa):
        monkeypatch.setattr(module, "get_session", session)

    async def setup():
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        async with factory() as s:
            s.add(Account(id=ACC, nickname="Synthetic"))
            await s.flush()
            s.add(Fan(account_id=ACC, fan_id=FAN))
            await s.commit()
    asyncio.run(setup())
    yield factory
    asyncio.run(engine.dispose())


def say(factory, mid, direction, body):
    async def go():
        async with factory() as s:
            s.add(Message(account_id=ACC, fan_id=FAN, message_id=mid, direction=direction, body=body,
                          created_at=datetime.utcnow()))
            await s.commit()
    asyncio.run(go())


def agreed(media):
    return asyncio.run(sa.is_agreed(ACC, FAN, media))


def test_no_stored_agreement_means_not_agreed(db):
    say(db, 5, "in", "do you have lace feet pics")
    assert not agreed([102, 103])


def test_stale_generic_yes_is_not_agreement(db):
    say(db, 1, "in", "yes")                       # an old yes to something else
    say(db, 5, "in", "do you have lace feet pics")
    asyncio.run(sa.propose(ACC, FAN, [102, 103], subject="lace feet"))
    say(db, 6, "out", "i dont have that exactly, want the red set instead?")
    assert not agreed([102, 103])                 # the yes predates the proposal; no yes after it


def test_yes_after_a_proposal_that_never_reached_him_is_not_agreement(db):
    say(db, 5, "in", "lace feet?")
    asyncio.run(sa.propose(ACC, FAN, [102, 103]))
    say(db, 7, "in", "yes")                       # no outbound message between proposal and yes
    assert not agreed([102, 103])


def test_agreed_alternative_is_allowed_once_for_that_turn_only(db):
    say(db, 5, "in", "lace feet?")
    asyncio.run(sa.propose(ACC, FAN, [102, 103], subject="lace feet"))
    say(db, 6, "out", "i dont have that, but i have the red set. want it?")
    say(db, 7, "in", "<p>Yes!</p>")
    assert agreed([102, 103]) and agreed([103])
    assert not agreed([102, 999])                 # a different item is not covered
    say(db, 9, "in", "what else you got")
    assert not agreed([102, 103])                 # a later turn needs fresh agreement


def test_plan_ask_refuses_substitute_and_records_proposal_then_sends_after_agreement(db, monkeypatch):
    say(db, 5, "in", "lace feet please")
    contract = Contract(is_ask=True, subject="lace feet", quote="lace feet please")
    monkeypatch.setattr(packs, "_guard", lambda *_: None)
    monkeypatch.setattr(packs.content_resolver, "resolve", AsyncMock(
        return_value=_empty(contract, NO_MATCH, alternatives=[102, 103])))
    ok = packs.PackPlan(ACC, FAN, "ask", "", 1, [102, 103], [], 900, Claim("not exactly lace feet but", 2),
                        substitute=True)
    monkeypatch.setattr(packs, "_plan_ask_from", AsyncMock(return_value=ok))
    first = asyncio.run(packs.plan_ask(ACC, FAN, contract))
    assert first.refusal == packs.REFUSE_SUBSTITUTE_UNAGREED and not first.media
    async def rows():
        async with db() as s:
            return (await s.execute(select(OfSubstitutionAgreement))).scalars().all()
    stored = asyncio.run(rows())
    assert len(stored) == 1 and stored[0].state == "proposed" and stored[0].proposed_turn_id == 5
    # the caption alone ("not exactly ...") never counts; only a later affirmative does
    say(db, 6, "out", "i dont have lace feet, want this set instead?")
    say(db, 7, "in", "yes please")
    second = asyncio.run(packs.plan_ask(ACC, FAN, contract))
    assert second.ok and second.substitute and second.media == [102, 103]


def test_wire_refuses_substitute_without_agreement(db, monkeypatch):
    say(db, 5, "in", "lace feet please")
    plan = packs.PackPlan(ACC, FAN, "ask", "", 1, [102], [], 900, Claim("not exactly lace feet but", 1), substitute=True)
    reaudit = AsyncMock(return_value=[])
    out = asyncio.run(packs._deliver(None, plan, voice_line=None, dry_run=False, reaudit=reaudit))
    assert out["status"] == "refused" and out["reason"] == packs.REFUSE_SUBSTITUTE_UNAGREED
    assert not reaudit.called
