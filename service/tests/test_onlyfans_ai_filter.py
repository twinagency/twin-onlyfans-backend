"""AI inbox filtering runs before pagination and preserves creator isolation."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import os
import sys
from pathlib import Path

os.environ.setdefault('TWIN_FASTT_CONFIG_ISOLATED', '1')
os.environ.setdefault('DATABASE_URL', 'sqlite+aiosqlite:///:memory:')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from db.models import Base, Chat, Fan, OfConversationState
import onlyfans_read.repo as module


def test_ai_filter_before_limit_account_pair_hidden_and_takeover(monkeypatch):
    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with engine.begin() as connection:
            await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=[Chat.__table__, Fan.__table__, OfConversationState.__table__]))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        @asynccontextmanager
        async def local_session():
            async with sessions() as session:
                yield session
        monkeypatch.setattr(module, 'get_session', local_session)
        stamp = datetime(2026, 10, 26, 10)
        async with sessions() as session, session.begin():
            for i in range(1, 75):
                session.add(Chat(account_id='account-a', fan_id=i, last_message_at=stamp-timedelta(minutes=i), hidden_locally=i == 73))
                session.add(Fan(account_id='account-a', fan_id=i, of_display_name='Synthetic fan'))
                session.add(OfConversationState(account_id='account-a', fan_id=i, mode='automated' if i >= 70 else 'paused', takeover_state='requested' if i == 72 else 'effective' if i == 74 else 'none'))
            # Identical fan id under another creator cannot supply control for account A.
            session.add(Chat(account_id='account-a', fan_id=80, last_message_at=None))
            session.add(OfConversationState(account_id='account-b', fan_id=80, mode='automated', takeover_state='none'))
            session.add(Chat(account_id='account-b', fan_id=90, last_message_at=stamp))
            session.add(OfConversationState(account_id='account-b', fan_id=90, mode='automated', takeover_state='none'))
            session.add(Chat(account_id='account-a', fan_id=81, last_message_at=None))
            session.add(OfConversationState(account_id='account-a', fan_id=81, mode='automated', takeover_state='none'))
        repo = module.SqlOnlyFansReadRepo()
        all_page = await repo.conversation_page(account_id='account-a', before=None, limit=25)
        assert all(row.fan_id < 70 for row in all_page)
        first = await repo.conversation_page(account_id='account-a', before=None, limit=1, ai_only=True)
        assert [r.fan_id for r in first] == [70]
        second = await repo.conversation_page(account_id='account-a', before=(first[-1].last_message_at, 70), limit=25, ai_only=True)
        assert [r.fan_id for r in second] == [71, 81]
        assert await repo.conversation_page(account_id='account-a', before=(None, 81), limit=25, ai_only=True) == []
        assert [r.fan_id for r in await repo.conversation_page(account_id='account-b', before=None, limit=25, ai_only=True)] == [90]
        # A real takeover immediately removes the conversation from AI results.
        async with sessions() as session, session.begin():
            state = await session.get(OfConversationState, ('account-a', 70))
            state.mode = 'human_controlled'
            state.takeover_state = 'effective'
        assert [r.fan_id for r in await repo.conversation_page(account_id='account-a', before=None, limit=25, ai_only=True)] == [71, 81]
        await engine.dispose()
    asyncio.run(run())
