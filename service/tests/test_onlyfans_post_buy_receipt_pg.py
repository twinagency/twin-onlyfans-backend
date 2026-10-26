"""The existing offer receipt serializes purchase replies before any wire call."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import select,text
from sqlalchemy.ext.asyncio import async_sessionmaker,create_async_engine
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database,seed
from db.models import AutomationRun,CatalogItem,ContentOffer
from onlyfans_runtime.store import utcnow

ACCOUNT="of-900001"


def purchase(database):
    store=seed(*database,account=ACCOUNT)
    now=utcnow()
    with Session(store.engine) as s,s.begin():
        s.add(CatalogItem(id=1,account_id=ACCOUNT,label="Synthetic",kind="photo",price_cents=1000))
        s.flush()
        for identity in (1,2):
            s.add(ContentOffer(id=identity,account_id=ACCOUNT,fan_id=101,item_id=1,mode="ppv",
                price_cents=1000,offer_message_id=5000+identity,status="delivered",
                resolved_by="ppv_ledger",resolved_at=now))
    return store


async def claims(store,monkeypatch,*,foreign=False,legacy=False):
    from automations import ai_chatter
    engine=create_async_engine(store.engine.url.set(drivername="postgresql+asyncpg"))
    factory=async_sessionmaker(engine,expire_on_commit=False)
    @asynccontextmanager
    async def session():
        async with factory() as s:
            yield s
            await s.commit()
    monkeypatch.setattr(ai_chatter,"get_session",session)
    try:
        async with factory() as s:
            first=await s.get(ContentOffer,1)
            second=await s.get(ContentOffer,2)
        if legacy:
            assert not await ai_chatter._claim_post_buy(ACCOUNT,first,utcnow())
            assert await ai_chatter._claim_post_buy(ACCOUNT,second,utcnow())
            assert not await ai_chatter._claim_post_buy(ACCOUNT,second,utcnow()+timedelta(days=2))
        elif foreign:
            assert not await ai_chatter._claim_post_buy("of-900009",first,utcnow())
            async with factory() as s,s.begin():
                row=await s.get(ContentOffer,1);row.status="cancelled"
            assert not await ai_chatter._claim_post_buy(ACCOUNT,first,utcnow())
        else:
            attempts=await asyncio.gather(*(ai_chatter._claim_post_buy(ACCOUNT,first,utcnow()) for _ in range(2)))
            assert sorted(attempts)==[False,True]
            assert not await ai_chatter._claim_post_buy(ACCOUNT,first,utcnow()+timedelta(days=2))
            assert await ai_chatter._claim_post_buy(ACCOUNT,second,utcnow())
    finally:
        await engine.dispose()


@pytest.mark.parametrize("foreign",[False,True])
def test_purchase_claim_is_durable_scoped_and_independent_for_distinct_purchases(database,monkeypatch,foreign):
    store=purchase(database)
    try:
        asyncio.run(claims(store,monkeypatch,foreign=foreign))
        with Session(store.engine) as s:
            values=s.scalars(select(ContentOffer.post_buy_started_at).order_by(ContentOffer.id)).all()
            assert all(value is None for value in values) if foreign else all(value is not None for value in values)
    finally:
        store.close()


def test_purchase_claim_upgrade_preserves_existing_offer_identity(database):
    from db.schema import upgrade,check_schema,LEDGER_MARKERS,SchemaMismatch
    store=purchase(database)
    with store.engine.begin() as c:
        c.exec_driver_sql("ALTER TABLE content_offers DROP COLUMN post_buy_started_at")
        c.execute(text("UPDATE alembic_version SET version_num=:v"),{"v":LEDGER_MARKERS})
        check_schema(c,expected=LEDGER_MARKERS)
        upgrade(c)
        check_schema(c)
        with pytest.raises(SchemaMismatch):check_schema(c,expected=LEDGER_MARKERS)
    with Session(store.engine) as s:
        offers=s.scalars(select(ContentOffer).order_by(ContentOffer.id)).all()
        assert [offer.offer_message_id for offer in offers]==[5001,5002]
        assert all(offer.status=="delivered" and offer.post_buy_started_at is None for offer in offers)
    store.close()


@pytest.mark.parametrize("status",["attempted","failed","completed"])
def test_deployed_attempt_receipts_survive_upgrade_without_replaying_a_purchase(database,monkeypatch,status):
    from db.schema import upgrade,LEDGER_MARKERS
    store=purchase(database)
    try:
        with Session(store.engine) as s,s.begin():
            s.add(AutomationRun(account_id=ACCOUNT,kind="post_buy:1",status=status,
                                started_at=utcnow(),stats_json='{"offer_id":1,"fan_id":101}'))
        with store.engine.begin() as c:
            c.exec_driver_sql("ALTER TABLE content_offers DROP COLUMN post_buy_started_at")
            c.execute(text("UPDATE alembic_version SET version_num=:v"),{"v":LEDGER_MARKERS})
            upgrade(c)
        asyncio.run(claims(store,monkeypatch,legacy=True))
        with Session(store.engine) as s:
            old=s.get(ContentOffer,1);new=s.get(ContentOffer,2)
            assert old.post_buy_started_at is None and new.post_buy_started_at is not None
            rows=s.scalars(select(AutomationRun).where(AutomationRun.account_id==ACCOUNT).order_by(AutomationRun.id)).all()
            assert [(r.kind,r.status) for r in rows]==[("post_buy:1",status),("post_buy:2","attempted")]
    finally:
        store.close()


def test_other_account_attempt_receipt_cannot_consume_our_purchase(database,monkeypatch):
    store=purchase(database)
    try:
        with Session(store.engine) as s,s.begin():
            s.add(AutomationRun(account_id="of-900009",kind="post_buy:1",status="attempted"))
        asyncio.run(claims(store,monkeypatch))
        with Session(store.engine) as s:
            assert len(s.scalars(select(AutomationRun).where(AutomationRun.account_id==ACCOUNT)).all())==2
    finally:
        store.close()
