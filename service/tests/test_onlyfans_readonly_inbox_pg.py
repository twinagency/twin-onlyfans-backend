"""Connected inboxes sync without granting model, send or purchase authority.

Uses disposable PostgreSQL and synthetic, account-bound provider read pages.
"""
from datetime import datetime, timedelta, timezone
import threading

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_onlyfans_connections_pg import (
    TENANT, OWNER, MODEL, LIMITS, verified, settings, provision, data, save, change,
)
from db.models import Chat, Fan, GrokCall, Message, Transaction, OfConversationState, OfCreatorBinding
from db.onlyfans_models import OfRuntime, OfWork, OfIngress, OfScan, OfRecovery
from db.onlyfans_connection_models import OfProviderConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from onlyfans_runtime.connections import load_worker_accounts, load_worker_cohort
from onlyfans_runtime.native_worker import NativeWorker
from onlyfans_runtime.provider_connector import NativeProviderConnector
from onlyfans_runtime.provider_model import NativeProviderModel
from onlyfans_runtime.store import Store, Event, Refused, utcnow
from onlyfans_runtime.worker import ChatPage, MessagePage, read_step
from ownership import owned_media_in_session
import onlyfans_worker as entry


def incoming(mid=10):
    return Event(901, mid, datetime.now(timezone.utc).isoformat(), "in", "Synthetic inbox input")


def readonly(store):
    accounts, _, _ = load_worker_cohort(store, TENANT, include_read_only=True)
    assert len(accounts) == 1 and accounts[0].read_connection_revision == 1
    assert accounts[0].model is None
    return accounts[0]


def forbid_outgoing(monkeypatch):
    from onlyfans_runtime.native_planning import NativeTextPlanner
    from onlyfans_runtime.text_delivery import TextAuthority
    from onlyfans_runtime.campaigns import CampaignService
    def forbidden(*_, **__):
        pytest.fail("read-only inbox entered a model, planning, repair or send phase")
    monkeypatch.setattr(NativeProviderModel, "__init__", forbidden)
    for owner, names in [(NativeTextPlanner, ["prepare", "recover", "queue"]),
                         (TextAuthority, ["expire_dispatches", "repair", "dispatch"]),
                         (CampaignService, ["queue_due"])]:
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)


def test_real_entry_syncs_and_polls_again_without_activation_model_or_purchase(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch, model={"modelId":MODEL, "apiKey":"synthetic-key"})
    forbid_outgoing(monkeypatch)
    history = [Event(901, 11, datetime.now(timezone.utc).isoformat(), "out", "Synthetic paid media", (44,), 300), incoming()]
    calls = []
    monkeypatch.setattr(NativeProviderConnector, "chats", lambda self, **kw: ChatPage([901], None))
    def messages(self, **kw):
        calls.append((self.account_id, kw["fan_id"]))
        return MessagePage(history, False)
    monkeypatch.setattr(NativeProviderConnector, "messages", messages)
    reports = []
    for _ in range(4):
        assert entry.run_loop(store, TENANT, threading.Event(), once=True, observe=reports.append) == 0
    with Session(database[1]) as s, s.begin():
        runtime = s.get(OfRuntime, account)
        assert runtime.enabled is False and not runtime.recovery_needed and runtime.last_sync_at is not None
        runtime.last_sync_at = utcnow() - timedelta(seconds=6)
    history.insert(0, incoming(20))
    for _ in range(3):
        assert entry.run_loop(store, TENANT, threading.Event(), once=True, observe=reports.append) == 0
    assert len(calls) >= 2 and all(not r.get("errors") and not r.get("held") for r in reports)
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Message)) == 3
        assert s.get(Chat, (account, 901)).last_message_id == 20
        assert s.get(OfConversationState, (account, 901)).mode == "paused"
        assert s.get(OfDeliveryPolicy, account) is None
        assert not s.get(OfRuntime, account).enabled
        assert set(s.scalars(select(OfIngress.source))) == {"import"}
        for table in (OfWork, OfTextIntent, GrokCall, Transaction):
            assert s.scalar(select(func.count()).select_from(table)) == 0
        paid = s.get(Message, (account, 901, 11))
        assert paid.is_paid is None and paid.purchased_at is None
        assert owned_media_in_session(s, account, 901) == set()
    store.close()


@pytest.mark.parametrize("change_kind", ["pause", "reconnect", "expire", "revoke"])
def test_old_read_worker_cannot_start_after_connection_or_binding_changes(database, monkeypatch, change_kind):
    store, account, payload = verified(database, monkeypatch)
    item = readonly(store)
    def forbidden(*_, **__): pytest.fail("stale connection performed a provider read")
    monkeypatch.setattr(NativeProviderConnector, "chats", forbidden)
    monkeypatch.setattr(NativeProviderConnector, "messages", forbidden)
    if change_kind == "pause": change(store, account, "pause", 1, "c"*64)
    elif change_kind == "reconnect": save(store, account, payload, 1, "c"*64)
    else:
        with Session(database[1]) as s, s.begin():
            if change_kind == "expire": s.get(OfProviderConnection, account).expires_at = utcnow()-timedelta(seconds=1)
            else: s.get(OfCreatorBinding, s.get(OfRuntime, account).binding_id).revoked_at = utcnow()
    with pytest.raises(Refused): store.request_poll(TENANT, account, connection_revision=1)
    report = NativeWorker(store, TENANT, (item,)).step()
    assert report.held == 1 and report.errors == report.ingress == 0
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Message)) == 0
    store.close()


@pytest.mark.parametrize("phase", ["chats", "messages"])
@pytest.mark.parametrize("change_kind", ["pause", "reconnect", "activate"])
def test_revision_change_during_get_rejects_page_and_preserves_recovery_cursor(database, monkeypatch, phase, change_kind):
    store, account, payload = verified(database, monkeypatch)
    item = readonly(store)
    def changed():
        if change_kind == "reconnect": save(store, account, payload, 1, "c"*64)
        else: change(store, account, change_kind, 1, "c"*64, LIMITS if change_kind == "activate" else None)
    def chats(self, **kw):
        if phase == "chats": changed()
        return ChatPage([901], None)
    def messages(self, **kw):
        changed()
        return MessagePage([incoming()], False)
    monkeypatch.setattr(NativeProviderConnector, "chats", chats)
    monkeypatch.setattr(NativeProviderConnector, "messages", messages)
    with pytest.raises(Refused):
        read_step(store, TENANT, account, item.reader, connection_revision=1)
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfIngress)) == 0
        assert s.scalar(select(func.count()).select_from(Message)) == 0
        if phase == "chats": assert s.get(OfScan, account).page_count == 0
        else: assert s.get(OfRecovery, (account, 901)).high_water == 0
    store.close()


def test_import_queued_before_activation_stays_historical_when_drained_after_it(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    item = readonly(store)
    monkeypatch.setattr(NativeProviderConnector, "chats", lambda self, **kw: ChatPage([901], None))
    monkeypatch.setattr(NativeProviderConnector, "messages", lambda self, **kw: MessagePage([incoming()], False))
    read_step(store, TENANT, account, item.reader, connection_revision=1)
    change(store, account, "activate", 1, "c"*64, LIMITS)
    # Even the normal active drain must not convert the queued import into work.
    assert store.drain(TENANT, account) == 1
    with Session(database[1]) as s:
        assert s.get(OfConversationState, (account, 901)).mode == "paused"
        assert s.scalar(select(func.count()).select_from(OfWork)) == 0
    store.close()


def test_readonly_drain_of_old_pending_intake_does_not_create_reply_work(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    assert store.record(TENANT, account, incoming(), source="catchup") == "accepted"
    assert store.drain(TENANT, account, connection_revision=1) == 1
    with Session(database[1]) as s:
        assert s.get(Message, (account, 901, 10)) is not None
        assert s.scalar(select(func.count()).select_from(OfWork)) == 0
    store.close()


def test_unavailable_provider_cooldown_fences_cached_readers_then_recovers(database, monkeypatch):
    store, account, _ = verified(database, monkeypatch)
    item = readonly(store)
    forbid_outgoing(monkeypatch)
    with Session(database[1]) as s, s.begin():
        row=s.get(OfProviderConnection,account)
        original_expiry=row.expires_at
        row.last_error="provider_connection_check_retry";row.updated_at=utcnow()
    def forbidden(*_,**__):pytest.fail("provider cooldown was bypassed")
    monkeypatch.setattr(NativeProviderConnector,"verify_identity",forbidden)
    monkeypatch.setattr(NativeProviderConnector,"chats",forbidden)
    monkeypatch.setattr(NativeProviderConnector,"messages",forbidden)
    cohort,cursor,scanned=load_worker_cohort(store,TENANT,include_read_only=True)
    assert cohort==[] and cursor is None and scanned==1
    assert NativeWorker(store,TENANT,(item,)).step().held==1
    with Session(database[1]) as s, s.begin():
        row=s.get(OfProviderConnection,account)
        assert row.expires_at==original_expiry and row.revision==1
        assert s.get(OfScan,account).status=="needed"
        assert not s.get(OfRuntime,account).enabled
        row.updated_at=utcnow()-timedelta(minutes=2)
    calls=[]
    monkeypatch.setattr(NativeProviderConnector,"verify_identity",lambda self:calls.append(self.account_id))
    monkeypatch.setattr(NativeProviderConnector,"chats",lambda self,**kw:ChatPage([901],None))
    monkeypatch.setattr(NativeProviderConnector,"messages",lambda self,**kw:MessagePage([incoming()],False))
    recovered=readonly(store)
    assert calls==[account]
    worker=NativeWorker(store,TENANT,(recovered,))
    for _ in range(3):assert worker.step().errors==0
    with Session(database[1]) as s:
        assert s.get(OfProviderConnection,account).last_error is None
        assert s.get(Message,(account,901,10)) is not None
        assert not s.get(OfRuntime,account).enabled
        assert s.scalar(select(func.count()).select_from(OfWork))==0
        assert s.scalar(select(func.count()).select_from(OfTextIntent))==0
    store.close()


def test_mixed_cohort_keeps_paging_and_never_adopts_unconnected_or_foreign_creators(database, monkeypatch):
    settings(monkeypatch); store = Store(database[0]); accounts = []
    monkeypatch.setattr(NativeProviderConnector, "verify_identity", lambda self: None)
    for index in range(4):
        owner = OWNER + index
        account = provision(store, owner_id=owner, creator_id="creator-"+str(index), command_key=f"{index+1:064x}")
        accounts.append(account)
        if index < 3:
            payload = data(); payload["ownerId"] = owner; payload["capture"]["headers"]["user_id"] = str(owner)
            save(store, account, payload)
    with Session(database[1]) as s, s.begin(): s.get(OfProviderConnection, accounts[0]).sealed_config = "tampered"
    assert load_worker_accounts(store, TENANT) == []
    change(store, accounts[2], "activate", 1, "f"*64, LIMITS)
    cursor = None; loaded = []
    for _ in range(3):
        cohort, cursor, scanned = load_worker_cohort(store, TENANT, limit=1, after_account=cursor, include_read_only=True)
        assert scanned == 1
        loaded.extend(cohort)
    assert cursor is None and [item.account_id for item in loaded] == accounts[1:3]
    assert loaded[0].read_connection_revision == 1 and loaded[1].read_connection_revision is None
    assert load_worker_cohort(store, "foreign-tenant", include_read_only=True) == ([], None, 0)
    with pytest.raises(Refused): store.request_poll("foreign-tenant", accounts[1], connection_revision=1)
    store.close()


def test_new_connection_recovers_first_provider_failure_then_imports_and_keeps_syncing(database, monkeypatch):
    from onlyfans_runtime.provider_connector import NativeProtocolError
    settings(monkeypatch); store=Store(database[0]); account=provision(store)
    save(store,account,data())
    forbid_outgoing(monkeypatch)
    calls=[]
    def unavailable(self):
        calls.append(self.account_id)
        raise NativeProtocolError("synthetic unavailable provider")
    monkeypatch.setattr(NativeProviderConnector,"verify_identity",unavailable)
    assert load_worker_cohort(store,TENANT,include_read_only=True)[0]==[]
    with Session(database[1]) as s:
        row=s.get(OfProviderConnection,account)
        original=(row.sealed_config,row.expires_at,row.revision)
        assert row.status=="pending" and row.verified_at is None
        assert row.last_error=="provider_connection_check_retry"
        assert s.get(OfScan,account).status=="needed"
    assert load_worker_cohort(store,TENANT,include_read_only=True)[0]==[]
    assert calls==[account]
    with Session(database[1]) as s,s.begin():
        s.get(OfProviderConnection,account).updated_at=utcnow()-timedelta(minutes=2)
    monkeypatch.setattr(NativeProviderConnector,"verify_identity",lambda self:calls.append(self.account_id))
    history=[incoming()]
    monkeypatch.setattr(NativeProviderConnector,"chats",lambda self,**kw:ChatPage([901],None))
    monkeypatch.setattr(NativeProviderConnector,"messages",lambda self,**kw:MessagePage(history,False))
    for _ in range(4):assert entry.run_loop(store,TENANT,threading.Event(),once=True,observe=lambda _:None)==0
    with Session(database[1]) as s,s.begin():
        row=s.get(OfProviderConnection,account)
        assert row.status=="verified" and row.last_error is None
        assert (row.sealed_config,row.expires_at,row.revision)==original
        runtime=s.get(OfRuntime,account)
        assert not runtime.enabled and not runtime.recovery_needed and runtime.last_sync_at is not None
        runtime.last_sync_at=utcnow()-timedelta(seconds=6)
    history.insert(0,incoming(20))
    for _ in range(3):assert entry.run_loop(store,TENANT,threading.Event(),once=True,observe=lambda _:None)==0
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(Message))==2
        assert s.get(Chat,(account,901)).last_message_id==20
        assert s.get(OfConversationState,(account,901)).mode=="paused"
        for table in (OfWork,OfTextIntent,GrokCall,Transaction):
            assert s.scalar(select(func.count()).select_from(table))==0
    assert calls==[account,account]
    store.close()


@pytest.mark.parametrize("outcome",["authentication","expired","paused","replaced"])
def test_first_verification_retry_respects_auth_expiry_and_connection_changes(database,monkeypatch,outcome):
    from onlyfans_runtime.provider_connector import NativeProtocolError,NativeAuthenticationError
    settings(monkeypatch);store=Store(database[0]);account=provision(store)
    payload=data();save(store,account,payload)
    def unavailable(self):raise NativeProtocolError("synthetic temporary failure")
    monkeypatch.setattr(NativeProviderConnector,"verify_identity",unavailable)
    assert load_worker_cohort(store,TENANT,include_read_only=True)[0]==[]
    if outcome=="paused":change(store,account,"pause",1,"c"*64)
    elif outcome=="replaced":
        save(store,account,payload,1,"c"*64)
        with Session(database[1]) as s:
            row=s.get(OfProviderConnection,account)
            assert row.revision==2 and row.status=="pending" and row.last_error is None
        store.close();return
    elif outcome=="expired":
        with Session(database[1]) as s,s.begin():s.get(OfProviderConnection,account).expires_at=utcnow()-timedelta(seconds=1)
    else:
        with Session(database[1]) as s,s.begin():s.get(OfProviderConnection,account).updated_at=utcnow()-timedelta(minutes=2)
    calls=[]
    def rejected(self):
        calls.append(self.account_id)
        raise NativeAuthenticationError("synthetic rejected identity")
    monkeypatch.setattr(NativeProviderConnector,"verify_identity",rejected)
    assert load_worker_cohort(store,TENANT,include_read_only=True)[0]==[]
    assert load_worker_cohort(store,TENANT,include_read_only=True)[0]==[]
    assert calls==([account] if outcome=="authentication" else [])
    with Session(database[1]) as s:
        row=s.get(OfProviderConnection,account)
        assert row.status=={"authentication":"failed","paused":"paused","expired":"expired"}[outcome]
        assert not s.get(OfRuntime,account).enabled
    store.close()
