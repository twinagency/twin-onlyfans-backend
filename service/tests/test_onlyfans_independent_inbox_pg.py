"""Independent intake: synthetic provider responses and disposable PG only."""
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
import json
import threading
import time
import subprocess
import sys

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_launch_provider_budget_pg import native
from test_onlyfans_connections_pg import TENANT
from test_onlyfans_readonly_inbox_pg import forbid_outgoing
from test_onlyfans_recent_inbox_pg import incoming
from db.models import Message, OfCreatorBinding
from db.onlyfans_models import OfRuntime, OfWork
from db.onlyfans_connection_models import OfProviderConnection
from onlyfans_runtime import inbox_worker as module
from onlyfans_runtime.inbox_worker import InboxWorker, account_ids, poll_account, healthy
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.store import utcnow, Refused, Store
from onlyfans_runtime.worker import ChatPage, MessagePage, read_step


def test_paused_account_imports_without_model_or_outgoing_activation(native, monkeypatch):
    store, account, _ = native
    forbid_outgoing(monkeypatch)
    monkeypatch.setattr(NativeProviderConnector, 'chats', lambda self, **kw: ChatPage([901], None, {901:200}))
    monkeypatch.setattr(NativeProviderConnector, 'messages', lambda self, **kw: MessagePage([incoming()], False))
    assert account_ids(store, TENANT) == [account]
    result = poll_account(store, TENANT, account)
    assert result['imported'] == 1 and result['lastHeadAt'] is not None
    with Session(store.engine) as s:
        assert s.get(Message, (account, 901, 200)) is not None
        assert not s.get(OfRuntime, account).enabled
        assert not s.get(OfProviderConnection, account).automate_new_conversations
        assert s.scalar(select(func.count()).select_from(OfWork)) == 0


def test_scope_revocation_and_connection_expiry_remove_inbox_authority(native):
    store, account, _ = native
    assert account_ids(store, TENANT) == [account]
    assert account_ids(store, 'other-tenant') == []
    with Session(store.engine) as s, s.begin():
        s.get(OfProviderConnection, account).expires_at = utcnow()-timedelta(seconds=1)
    assert account_ids(store, TENANT) == []
    with pytest.raises(Refused):
        poll_account(store, TENANT, account)


def test_slow_account_does_not_block_other_accounts_or_new_connections():
    stop, release, slow_started, fast_done, new_done = (threading.Event() for _ in range(5))
    names, now, seen = ['slow', 'fast'], [100.], []
    def poll(store, tenant, account):
        seen.append(account)
        if account == 'slow':
            slow_started.set()
            assert release.wait(3)
        elif account == 'fast':
            fast_done.set()
        else:
            new_done.set()
        return {'lastHeadAt': utcnow().isoformat()+'Z'}
    worker = InboxWorker(object(), TENANT, stop, workers=3, roster=lambda *a: list(names),
                         poll=poll, clock=lambda:now[0], health_path=None)
    with ThreadPoolExecutor(max_workers=3) as pool:
        try:
            worker.step(pool)
            assert slow_started.wait(1) and fast_done.wait(1)
            assert not release.is_set()
            # A verified account added after startup is discovered automatically.
            names.append('new')
            now[0] += 6
            worker.step(pool)
            assert new_done.wait(1)
            assert seen.count('slow') == 1
        finally:
            stop.set()
            release.set()


def test_scheduler_recovers_after_one_account_fails_without_duplicate_inflight():
    now, calls = [100.], []
    class Pool:
        def submit(self, fn, *args):
            future = Future()
            calls.append(args[-1])
            future.set_exception(ValueError('synthetic failure'))
            return future
    worker = InboxWorker(object(), TENANT, threading.Event(), roster=lambda *a:['a','b'],
                         clock=lambda:now[0], health_path=None)
    pool = Pool()
    worker.step(pool)
    assert calls == ['a','b']
    worker.step(pool)
    assert not worker.futures and calls == ['a','b']
    now[0] += 2
    worker.step(pool)
    assert calls == ['a','b','a','b']


def test_stale_head_and_dead_supervisor_fail_healthcheck(tmp_path):
    path = tmp_path/'health.json'
    payload = {'observedAt':time.time(), 'rosterUpdatedAt':time.time(), 'accounts':{'synthetic':{'stale':False}}}
    path.write_text(json.dumps(payload))
    assert healthy(path)
    payload['accounts']['synthetic']['stale'] = True
    path.write_text(json.dumps(payload))
    assert not healthy(path)
    payload['accounts'] = {}
    payload['observedAt'] -= 60
    path.write_text(json.dumps(payload))
    assert not healthy(path)


def test_background_worker_yields_live_inbox_ownership(native, monkeypatch):
    store, account, reader = native
    monkeypatch.setenv('ONLYFANS_RECENT_INBOX_ENABLED', '1')
    store._independent_inbox = True
    monkeypatch.setattr('onlyfans_runtime.recent_inbox.poll_recent', lambda *a, **k: pytest.fail('duplicate live intake'))
    monkeypatch.setattr(reader, 'chats', lambda **kw: ChatPage([], None))
    monkeypatch.setattr(reader, 'messages', lambda **kw: MessagePage([], False))
    read_step(store, TENANT, account, reader, connection_revision=1)


def test_healthcheck_imports_no_application_or_database_modules():
    result = subprocess.run([sys.executable, '-c',
        'import sys; from onlyfans_runtime.inbox_health import healthy; '
        'assert "sqlalchemy" not in sys.modules; assert "db.models" not in sys.modules'],
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_independent_pool_can_serve_all_inbox_slots(database):
    store = Store(database[0], pool_size=8)
    try:
        assert store.engine.pool.size() == 8
        connections = [store.engine.connect() for _ in range(8)]
        try:
            assert store.engine.pool.checkedout() == 8
        finally:
            for connection in connections:
                connection.close()
    finally:
        store.close()
