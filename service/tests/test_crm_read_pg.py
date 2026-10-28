"""Real PostgreSQL queries for copied handles, unloaded rows and health evidence."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine,AsyncSession
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database,seed
from db.models import Chat,Fan
from db.onlyfans_models import OfIngress,OfRuntime
from onlyfans_runtime.store import utcnow

def test_server_search_precedes_pagination_and_never_crosses_accounts(database,monkeypatch):
    url,engine=database;store=seed(url,engine);other=seed(url,engine,account='acc-b')
    now=utcnow()
    with Session(engine) as s,s.begin():
        for account in ('acc-a','acc-b'):
            s.add(Fan(account_id=account,fan_id=318022380,of_username='u318022380',of_display_name='Gervonta'))
            s.add(Chat(account_id=account,fan_id=318022380,last_message_at=now-timedelta(days=7)))
        s.add(Chat(account_id='acc-a',fan_id=101,last_message_at=now))
    async def check():
        from onlyfans_read import repo
        from onlyfans_read.search import normalize_search as normalize
        from onlyfans_read.health import account_health
        ae=create_async_engine(make_url(url).set(drivername='postgresql+asyncpg'))
        @asynccontextmanager
        async def session():
            async with AsyncSession(ae) as s:yield s
        monkeypatch.setattr(repo,'get_session',session)
        try:
            r=repo.SqlOnlyFansReadRepo()
            assert (await r.conversation_page(account_id='acc-a',before=None,limit=1))[0].fan_id==101
            for query in ('@u318022380','u318022380','318022380','Gervonta'):
                found=await r.conversation_page(account_id='acc-a',before=None,limit=1,search=normalize(query))
                assert [(x.account_id,x.fan_id) for x in found]==[('acc-a',318022380)]
            assert not await r.conversation_page(account_id='acc-a',before=None,limit=1,search='%')
            async with session() as s:
                health=await account_health(s,'acc-a')
                assert health['syncHealth']['incoming']['lastSuccessAt'] is None
                assert health['syncHealth']['incoming']['coverage']=='Latest 256 applied intake events.'
                assert health['syncHealth']['history']['quarantined']==0
        finally:await ae.dispose()
    asyncio.run(check());store.close();other.close()
