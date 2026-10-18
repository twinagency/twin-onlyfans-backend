"""Native-shaped HTTP intake -> worker -> one-use send -> canonical PostgreSQL."""
import json
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, due, seed
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured, prepare
from test_onlyfans_worker_pg import start, worker
from test_onlyfans_media_pg import ASK, shelf, media_policy
from onlyfans_native_protocol_fixture import OWNER, native_message, native_peer
from db.models import (AccountAiConfig, ContentOffer, GrokDailyCost, Message,
                       OfConversationState, OfMessageDelivery, Transaction, VaultSend)
from db.onlyfans_models import OfIngress, OfRuntime, OfWork
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from onlyfans_runtime.native_connector import NativeLoopbackConnector, from_native_server
from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.native_protocol import message
from onlyfans_runtime.native_worker import NativeWorker, WorkerAccount
from onlyfans_runtime.store import Refused
from onlyfans_runtime.text_delivery import TextAuthority


def connector(peer, *, account=ACCOUNT, revision=1, timeout=1):
    return NativeLoopbackConnector(from_native_server(peer.server, account_id=account,
        owner_id=OWNER, policy_revision=revision), timeout_seconds=timeout)


def fresh(database, monkeypatch):
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setenv("DATABASE_URL", database[0])
    store = seed(*database)
    with Session(database[1]) as s, s.begin():
        s.add(AccountAiConfig(account_id=ACCOUNT, model="grok-4-1-fast-non-reasoning",
            persona="Synthetic Creator enjoys hiking and coffee.", daily_cost_cap_cents=100))
    authority = TextAuthority(store)
    authority.configure(TENANT, ACCOUNT, expected_revision=0, enabled=True, limit_units=3,
        throttle_seconds=1, actor="synthetic-protocol", reason="Owned native-shaped fixture")
    store.begin_scan(TENANT, ACCOUNT)
    return store, authority


def test_short_native_pages_recover_all_history_then_worker_sends_once_without_db_locks(database, monkeypatch):
    store, _ = fresh(database, monkeypatch); engine = database[1]
    model = SyntheticNativeModel("Hello & hi\nHow has your day been?", reported_cost_millicents=7)
    history = {101: [native_message(mid, text=f"<p>Synthetic input {mid}</p>") for mid in (1,2,3)],
               202: [native_message(4, fan=202)]}

    def inspect(record):
        with engine.begin() as c:
            c.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
            assert c.scalar(text("SELECT account_id FROM of_runtime WHERE account_id=:a FOR UPDATE NOWAIT"), {"a":ACCOUNT}) == ACCOUNT
            if record["method"] == "POST":
                row = c.execute(text("SELECT wire_started_at,status,dispatch_token FROM of_text_intent WHERE account_id=:a"), {"a":ACCOUNT}).mappings().one()
                assert row["wire_started_at"] is not None and row["status"] == "dispatching"
                assert row["dispatch_token"].encode() not in record["raw"]
                assert c.scalar(text("SELECT count(*) FROM messages WHERE direction='out'")) == 0

    with native_peer(history=history, page_size=1, on_request=inspect) as peer:
        wire = connector(peer)
        runtime = NativeWorker(store, TENANT, (WorkerAccount(ACCOUNT,model,wire,wire),))
        # The tiny fixture pages force authoritative hasMore/cursor catch-up.
        for _ in range(8):
            result = runtime.step(); assert result.errors == result.accepted == 0
        with Session(engine) as s:
            assert s.get(OfRuntime,ACCOUNT).recovery_needed is False
            assert s.scalar(select(func.count()).select_from(Message)) == 4
            assert s.get(OfConversationState,(ACCOUNT,202)).mode == "paused"
        due(engine)
        assert runtime.step().accepted == 1
        for _ in range(3): assert runtime.step().claims == 0
        assert len(model.calls) == len(peer.posts) == 1
        post = peer.posts[0]
        assert post["path"] == "/api2/v2/chats/101/messages"
        assert post["json"]["text"] == "<p>Hello &amp; hi<br />How has your day been?</p>"
        assert post["json"]["rfTag"] == [] and "userTags" not in post["json"]
        assert all(not any(key in row["headers"] for key in ("cookie","authorization")) for row in peer.records)
        queries = [(urlsplit(row["path"]).path, parse_qs(urlsplit(row["path"]).query)) for row in peer.records if "?" in row["path"]]
        assert [q["offset"] for path,q in queries if path == "/api2/v2/chats"] == [["0"],["1"]]
        assert [q["id"] for path,q in queries if path == "/api2/v2/chats/101/messages" and "id" in q] == [["3"],["2"]]
        # Replayed native REST/bound-feed messages share canonical deduplication.
        page = wire.messages(fan_id=101, before_id=None, limit=100)
        assert page.events[0].message_id == 9001
        for source in ("ws","rest"):
            for incoming in page.events:
                # Authority projection has no ingress row yet. The first
                # observation is recorded, then compared with canonical history.
                assert store.record(TENANT,ACCOUNT,incoming,source=source) == ("accepted" if source=="ws" else "duplicate")
            for row in history[101]:
                assert store.record(TENANT,ACCOUNT,message(row,owner_id=OWNER,fan_id=101),source=source) == "duplicate"
            store.drain(TENANT,ACCOUNT)
        assert runtime.step().claims == 0 and len(model.calls) == len(peer.posts) == 1
    with Session(engine) as s:
        assert s.get(Message,(ACCOUNT,FAN,9001)).body == model.body
        assert s.get(OfMessageDelivery,(ACCOUNT,FAN,9001)).outcome == "sent"
        assert s.scalar(select(GrokDailyCost)).cost_cents == 7
        assert s.get(OfDeliveryPolicy,ACCOUNT).used_units == 1
        assert s.scalar(select(OfTextIntent)).evidence_ref.startswith("native-local:")
    store.close()


@pytest.mark.parametrize("mode", ["wrong_text","wrong_owner_post","wrong_recipient","wrong_price","wrong_media",
    "missing_id","drop","slow","duplicate_key","redirect","401","403","429","500","locked"])
def test_native_response_uncertainty_keeps_unit_cost_and_never_retries(database, monkeypatch, mode):
    store, _ = start(database,monkeypatch); model = SyntheticNativeModel(reported_cost_millicents=7)
    with native_peer(mode=mode) as peer:
        runtime = worker(store,model=model,transport=connector(peer,timeout=.15 if mode=="slow" else 1))
        assert runtime.step().unknown == 1
        for _ in range(3): runtime.step()
        assert len(model.calls) == len(peer.posts) == 1
        assert len(peer.records) == 2  # identity GET plus one POST; no redirect/retry
    with Session(database[1]) as s:
        row = s.scalar(select(OfTextIntent)); policy = s.get(OfDeliveryPolicy,ACCOUNT)
        assert row.status == "unknown" and row.wire_started_at is not None and row.reservation_held
        assert row.provider_message_id is None and row.projected_at is None
        assert policy.used_units == 1 and policy.active_intent_id == row.intent_id
        assert s.scalar(select(GrokDailyCost)).cost_cents == 7
        assert s.get(OfConversationState,(ACCOUNT,FAN)).unresolved_attempt_id == row.attempt_id
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=="out")) == 0
    store.close()


def test_native_owner_mismatch_blocks_intake_and_planning_and_can_resume_catchup(database, monkeypatch):
    store, _ = fresh(database,monkeypatch); model = SyntheticNativeModel(reported_cost_millicents=7)
    with native_peer(mode="wrong_owner") as peer:
        wire = connector(peer); runtime = NativeWorker(store,TENANT,(WorkerAccount(ACCOUNT,model,wire,wire),))
        assert runtime.step().errors == 1
        assert len(peer.records) == 1 and peer.posts == model.calls == []
        with Session(database[1]) as s:
            assert s.get(OfRuntime,ACCOUNT).recovery_needed and s.scalar(select(func.count()).select_from(Message)) == 0
        # Only expire the fixture's failed read lease, never send/model authority.
        with database[1].begin() as c:
            c.exec_driver_sql("UPDATE of_scan SET lease_until=CURRENT_TIMESTAMP-INTERVAL '1 second'")
        peer.mode = "accepted"
        for _ in range(3): assert runtime.step().errors == 0
        due(database[1]); assert runtime.step().accepted == 1
        assert len(model.calls) == len(peer.posts) == 1
    store.close()


@pytest.mark.parametrize("malformation", ["chat_identity","has_more","message_identity","message_order","too_large"])
def test_invalid_native_page_never_partially_applies_or_clears_recovery(database, monkeypatch, malformation):
    store, _ = fresh(database,monkeypatch); model = SyntheticNativeModel()
    def mutate(record,value):
        if record["path"].startswith("/api2/v2/chats?"):
            if malformation == "chat_identity": return {"list":[{"withUser":{"id":True}}],"hasMore":False}
            if malformation == "has_more": return {"list":[],"hasMore":True}
        if "/messages?" in record["path"]:
            if malformation == "message_identity": return {"list":[native_message(3),native_message(True)],"hasMore":False}
            if malformation == "message_order": return {"list":[native_message(1),native_message(2)],"hasMore":False}
            if malformation == "too_large": return {"list":[native_message(mid,text="x"*20000) for mid in range(4,0,-1)],"hasMore":False}
        return value
    with native_peer(mutate=mutate) as peer:
        wire = connector(peer); runtime = NativeWorker(store,TENANT,(WorkerAccount(ACCOUNT,model,wire,wire),))
        assert runtime.step().errors == 1
        assert peer.posts == model.calls == []
    with Session(database[1]) as s:
        assert s.get(OfRuntime,ACCOUNT).recovery_needed
        assert s.scalar(select(func.count()).select_from(Message)) == 0
        assert s.scalar(select(func.count()).select_from(OfIngress)) == 0
    store.close()


@pytest.mark.parametrize("mode", ["accepted","missing_previews","wrong_previews","opened"])
def test_native_media_requires_exact_preview_price_and_unpaid_evidence(database, monkeypatch, mode):
    store, authority = start(database,monkeypatch); engine = database[1]
    item = shelf(engine); media_policy(authority)
    from test_onlyfans_foundation_pg import event
    with Session(engine) as s,s.begin():
        s.get(AccountAiConfig,ACCOUNT).ai_chatter_config_json = canonical({"pack_send_enabled":True,
            "pack_on_ask_enabled":True,"qualification_gate_enabled":True})
    store.record(TENANT,ACCOUNT,event(mid=2,body=ASK)); store.drain(TENANT,ACCOUNT); due(engine)
    model = SyntheticNativeModel("Here are the saved collection details.", sale={"item_id":item,"quote":ASK}, reported_cost_millicents=7)
    with native_peer(mode=mode) as peer:
        runtime = worker(store,model=model,transport=connector(peer,revision=2))
        report = runtime.step()
        assert report.accepted == int(mode=="accepted") and report.unknown == int(mode!="accepted")
        for _ in range(2): runtime.step()
        assert len(model.calls) == len(peer.posts) == 1
        payload = peer.posts[0]["json"]
        assert payload["mediaFiles"] == [11,12,13] and payload["previews"] == [11]
        with Session(engine) as s:
            intent = s.scalar(select(OfTextIntent)); assert payload["price"] == intent.price_cents/100
            assert intent.price_cents > 1000  # actual native price selection, not a sticker fallback
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(ContentOffer)) == int(mode=="accepted")
        assert s.scalar(select(func.count()).select_from(VaultSend).where(VaultSend.was_purchased.is_(True))) == 0
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
        assert s.get(OfDeliveryPolicy,ACCOUNT).used_units == 1
    store.close()


@pytest.mark.parametrize("change", ["pause","policy","input"])
def test_native_identity_preflight_does_not_replace_final_control_and_generation_checks(database, monkeypatch, change):
    store, authority = start(database,monkeypatch); model = SyntheticNativeModel(reported_cost_millicents=7)
    def mutate(record):
        if record["path"] != "/api2/v2/users/me": return
        if change == "pause":
            store.control(TENANT,ACCOUNT,FAN,mode="paused",expected_revision=0,command_key="during-identity",
                          actor="synthetic",reason="Pause before native POST")
        elif change == "policy":
            authority.configure(TENANT,ACCOUNT,expected_revision=1,enabled=False,limit_units=20,
                throttle_seconds=1,actor="synthetic",reason="Disable before native POST")
        else:
            from test_onlyfans_foundation_pg import event
            store.record(TENANT,ACCOUNT,event(mid=2)); store.drain(TENANT,ACCOUNT)
    with native_peer(on_request=mutate) as peer:
        runtime = worker(store,model=model,transport=connector(peer))
        assert runtime.step().accepted == 0
        assert peer.posts == [] and len(model.calls) == 1
    with Session(database[1]) as s:
        intent = s.scalar(select(OfTextIntent))
        assert intent.status == "cancelled" and intent.wire_started_at is None
        assert s.get(OfDeliveryPolicy,ACCOUNT).used_units == 0
        assert s.scalar(select(GrokDailyCost)).cost_cents == 7
    store.close()


def test_native_scope_role_and_same_connector_registration_fail_before_network(database, monkeypatch):
    store, authority = configured(database); identity = prepare(authority)
    with native_peer() as peer:
        wrong = connector(peer,account="acc-other")
        with pytest.raises(Refused,match="scope"):
            authority.dispatch(TENANT,ACCOUNT,identity,wrong)
        wire = connector(peer); monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","api")
        with pytest.raises(Refused,match="worker"):
            wire.chats(cursor=None,limit=100)
        monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
        other = connector(peer)
        with pytest.raises(Refused,match="same bound"):
            NativeWorker(store,TENANT,(WorkerAccount(ACCOUNT,SyntheticNativeModel(),wire,other),))
        assert peer.records == []
    store.close()


def test_native_forged_foreign_and_consumed_grants_cannot_trigger_identity_io(database, monkeypatch):
    store, authority = configured(database); identity = prepare(authority)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE","worker")
    grant = authority.admit(TENANT,ACCOUNT,identity)
    with native_peer() as peer:
        wire = connector(peer)
        with pytest.raises(Refused): wire.send(grant,authority=authority,tenant_id="tenant-other")
        with pytest.raises(Refused): wire.send(replace(grant,body="forged text"),authority=authority,tenant_id=TENANT)
        assert peer.records == []
        proof = wire.send(grant,authority=authority,tenant_id=TENANT)
        assert len(peer.records) == 2 and len(peer.posts) == 1
        with pytest.raises(Refused): wire.send(grant,authority=authority,tenant_id=TENANT)
        assert len(peer.records) == 2
        assert authority.record_accepted(TENANT,ACCOUNT,identity,grant.token,proof) == "accepted"
        assert authority.materialize(TENANT,ACCOUNT,identity)
    store.close()
