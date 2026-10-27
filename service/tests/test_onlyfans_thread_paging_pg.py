"""Keyset thread history and delivery/media scope on real PostgreSQL."""
import asyncio
from contextlib import asynccontextmanager
from sqlalchemy.orm import Session
import pytest
from test_onlyfans_foundation_pg import database, seed, event
from onlyfans_read import repo
from onlyfans_read.contract import encode_thread_cursor, decode_thread_cursor


def test_history_keyset_keeps_pages_and_receipts_in_thread(database, monkeypatch):
    store=seed(*database)
    for mid in range(1,251):
        store.record('tenant-a','acc-a',event(mid=mid,fan=101))
    store.record('tenant-a','acc-a',event(mid=9000,fan=202))
    while store.drain('tenant-a','acc-a'):
        pass
    class Adapter:
        def __init__(self,s):self.session=s
        async def execute(self,q):return self.session.execute(q)
    @asynccontextmanager
    async def sessions():
        with Session(database[1]) as s:yield Adapter(s)
    async def scope(s,a):assert a=='acc-a'
    monkeypatch.setattr(repo,'get_session',sessions)
    monkeypatch.setattr(repo,'lock_read_scope',scope)
    async def journey():
        r=repo.SqlOnlyFansReadRepo()
        first=await r.messages(account_id='acc-a',fan_id=101,limit=100)
        assert [m.message_id for m in first]==list(range(151,251))
        token=encode_thread_cursor(account_id='acc-a',fan_id=101,before_message_id=first[0].message_id)
        before=decode_thread_cursor(token,account_id='acc-a',fan_id=101)
        second=await r.messages(account_id='acc-a',fan_id=101,limit=100,before_message_id=before)
        third=await r.messages(account_id='acc-a',fan_id=101,limit=100,before_message_id=second[0].message_id)
        assert [m.message_id for m in second]==list(range(51,151))
        assert [m.message_id for m in third]==list(range(1,51))
        assert not set(m.message_id for m in first)&set(m.message_id for m in second)
        for account,fan in [('acc-b',101),('acc-a',202)]:
            with pytest.raises(ValueError):decode_thread_cursor(token,account_id=account,fan_id=fan)
        for invalid in ['', 'x'*513,'e30','eyJ2IjoxLCJhIjoiYWNjLWEiLCJmIjoxMDEsIm0iOnRydWV9']:
            with pytest.raises(ValueError):decode_thread_cursor(invalid,account_id='acc-a',fan_id=101)
    try:asyncio.run(journey())
    finally:store.close()
