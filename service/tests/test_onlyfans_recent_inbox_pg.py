"""Inbox head intake continues during archive recovery, using synthetic PG only."""
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_launch_provider_budget_pg import native
from test_onlyfans_connections_pg import TENANT, save
from test_onlyfans_readonly_inbox_pg import forbid_outgoing
from db.models import Chat, Message
from db.onlyfans_models import OfScan, OfRecovery, OfRuntime, OfWork
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_content_sync_progress_models import OfProviderReadBudget
from onlyfans_runtime.store import Event, Refused, utcnow
from onlyfans_runtime.worker import ChatPage, MessagePage, read_step
from onlyfans_runtime.recent_inbox import poll_recent
from onlyfans_runtime.provider_connector import NativeRateLimitError


def incoming(mid=200, fan=901):
    return Event(fan, mid, datetime.now(timezone.utc).isoformat(), "in", "Synthetic newest message")


def test_new_head_imports_while_archive_cursors_and_send_settings_stay_unchanged(native, monkeypatch):
    store, account, reader = native
    forbid_outgoing(monkeypatch)
    with Session(store.engine) as s, s.begin():
        scan = s.get(OfScan, account)
        scan.cursor, scan.status = "12000", "needed"
        s.add(OfRecovery(account_id=account, fan_id=901, status="needed", before_id=100,
                         upper_bound=150, high_water=20, generation=7))
    calls = []
    def chats(**kw):
        assert store.engine.pool.checkedout() == 0
        calls.append(kw["cursor"])
        return ChatPage([901], "50", {901: 200})
    monkeypatch.setattr(reader, "chats", chats)
    monkeypatch.setattr(reader, "messages", lambda **kw: MessagePage([incoming()], True))
    assert poll_recent(store, TENANT, account, reader, connection_revision=1, clock=lambda:100) == 1
    with Session(store.engine) as s:
        assert s.get(Chat, (account, 901)).last_message_id == 200
        assert s.get(Message, (account, 901, 200)).body == "Synthetic newest message"
        assert s.get(OfScan, account).cursor == "12000"
        r = s.get(OfRecovery, (account, 901))
        assert (r.before_id, r.upper_bound, r.high_water, r.generation) == (100,150,20,7)
        assert not s.get(OfRuntime, account).enabled
        assert not s.get(OfProviderConnection, account).automate_new_conversations
        assert s.scalar(select(func.count()).select_from(OfWork)) == 0
        assert s.get(OfProviderReadBudget, account).lease_token is None
    assert calls == [None]


def test_known_head_does_not_reread_messages_and_new_head_is_detected_on_next_interval(native, monkeypatch):
    store, account, reader = native
    mid = [200]
    calls = []
    monkeypatch.setattr(reader, "chats", lambda **kw: ChatPage([901], None, {901:mid[0]}))
    def messages(**kw):
        calls.append(kw)
        return MessagePage([incoming(mid[0])],False)
    monkeypatch.setattr(reader, "messages", messages)
    for now in (100,101,116):
        poll_recent(store,TENANT,account,reader,connection_revision=1,clock=lambda:now)
    assert len(calls)==1
    mid[0]=201
    assert poll_recent(store,TENANT,account,reader,connection_revision=1,clock=lambda:132)==1
    assert len(calls)==2 and all(c['before_id'] is None for c in calls)


def test_pending_fans_survive_occupied_budget_then_resume(native,monkeypatch):
    store,account,reader=native
    from onlyfans_runtime.provider_read_budget import acquire, release
    claim=acquire(store,TENANT,account,connection_revision=1,kind='history')
    calls=[]
    monkeypatch.setattr(reader,'chats',lambda **kw:(calls.append('head') or ChatPage([901],None)))
    monkeypatch.setattr(reader,'messages',lambda **kw:MessagePage([incoming()],False))
    assert poll_recent(store,TENANT,account,reader,connection_revision=1,clock=lambda:100)==0
    assert not calls
    release(store,TENANT,claim)
    assert poll_recent(store,TENANT,account,reader,connection_revision=1,clock=lambda:101)==1


def test_reconnect_during_message_get_fences_result(native,monkeypatch):
    store,account,reader=native
    monkeypatch.setattr(reader,'chats',lambda **kw:ChatPage([901],None))
    def messages(**kw):
        with Session(store.engine) as s,s.begin():
            s.get(OfProviderConnection,account).revision+=1
        return MessagePage([incoming()],False)
    monkeypatch.setattr(reader,'messages',messages)
    with pytest.raises(Refused):poll_recent(store,TENANT,account,reader,connection_revision=1)
    with Session(store.engine) as s:
        assert s.scalar(select(func.count()).select_from(Message))==0


def test_restart_rereads_head_without_duplicate_messages(native,monkeypatch):
    store,account,reader=native
    monkeypatch.setattr(reader,'chats',lambda **kw:ChatPage([901],None))
    monkeypatch.setattr(reader,'messages',lambda **kw:MessagePage([incoming()],False))
    assert poll_recent(store,TENANT,account,reader,connection_revision=1)==1
    store._recent_inbox_states={}
    # Use the same timestamp and payload for a duplicate provider row.
    with Session(store.engine) as s:
        row=s.get(Message,(account,901,200))
        stamp=row.created_at.replace(tzinfo=timezone.utc).isoformat()
    monkeypatch.setattr(reader,'messages',lambda **kw:MessagePage([Event(901,200,stamp,'in','Synthetic newest message')],False))
    assert poll_recent(store,TENANT,account,reader,connection_revision=1)==0
    with Session(store.engine) as s:assert s.scalar(select(func.count()).select_from(Message))==1


def test_rate_limit_preserves_pending_and_shared_backoff(native,monkeypatch):
    store,account,reader=native
    monkeypatch.setattr(reader,'chats',lambda **kw:ChatPage([901],None))
    def limited(**kw):raise NativeRateLimitError(30)
    monkeypatch.setattr(reader,'messages',limited)
    with pytest.raises(NativeRateLimitError):poll_recent(store,TENANT,account,reader,connection_revision=1)
    with Session(store.engine) as s:
        row=s.get(OfProviderReadBudget,account)
        assert row.lease_token is None and row.backoff_until>utcnow()
    assert poll_recent(store,TENANT,account,reader,connection_revision=1)==0
