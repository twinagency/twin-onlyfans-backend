"""Set-based list metadata, old holds and account/fan isolation on PostgreSQL."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from sqlalchemy import event as sql_event, text
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database, seed, event
from db.models import OfConversationState, OfMessageDelivery
from onlyfans_runtime.store import utcnow
from onlyfans_read import repo

def test_list_metadata_uses_constant_queries_and_keeps_old_holds(database, monkeypatch):
    store=seed(*database)
    account="acc-a"
    for fan in range(101,126):
        store.record("tenant-a",account,event(mid=fan,fan=fan))
    store.drain("tenant-a",account)
    with Session(database[1]) as session, session.begin():
        state=session.get(OfConversationState,(account,101))
        state.unresolved_attempt_id="explicit-pointer"
        session.add(OfMessageDelivery(account_id=account,fan_id=101,message_id=101,
            outcome="unknown",attempt_id="other-pointer",recorded_at=utcnow()))
        session.add(OfMessageDelivery(account_id=account,fan_id=102,message_id=102,
            outcome="unknown",attempt_id="old-attempt",recorded_at=utcnow()))
    class Adapter:
        def __init__(self, session):self.session=session
        async def execute(self, query):return self.session.execute(query)
        async def scalars(self, query):return self.session.scalars(query)
        async def run_sync(self, fn):return fn(self.session)
    @asynccontextmanager
    async def session_factory():
        with Session(database[1]) as session:yield Adapter(session)
    async def scope(session, requested):
        assert requested==account
        await session.execute(text("SELECT 1"))
    monkeypatch.setattr(repo,"get_session",session_factory)
    monkeypatch.setattr(repo,"lock_read_scope",scope)
    queries=[]
    def observe(*args):queries.append(args[2])
    sql_event.listen(database[1],"before_cursor_execute",observe)
    try:
        result,outcomes=asyncio.run(repo.SqlOnlyFansReadRepo().conversation_metadata(
            account_id=account,rows=[SimpleNamespace(fan_id=fan,last_message_id=fan) for fan in range(101,126)]))
        page_query_count=len(queries)
        assert page_query_count<=7, "page metadata must have O(1) query count"
        assert result[101].unresolved_attempt_id=="explicit-pointer"
        assert result[102].unresolved_attempt_id=="old-attempt"
        assert result[102].requires_attention
        assert not result[103].unresolved_attempt_id
        assert outcomes[101]==outcomes[102]=="unknown"
        queries.clear()
        asyncio.run(repo.SqlOnlyFansReadRepo().conversation_metadata(
            account_id=account,rows=[SimpleNamespace(fan_id=101,last_message_id=101)]))
        assert len(queries)==page_query_count, "query count must not grow with page size"
        queries.clear()
        empty=asyncio.run(repo.SqlOnlyFansReadRepo().conversation_metadata(account_id=account,rows=[]))
        assert empty==({}, {}) and queries==[]
    finally:
        sql_event.remove(database[1],"before_cursor_execute",observe)
        store.close()
