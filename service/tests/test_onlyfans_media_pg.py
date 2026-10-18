"""Existing media, native same-call selection and the common one-use authority."""
from dataclasses import replace
from datetime import timedelta
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database, due, event
from test_onlyfans_native_planning_pg import ready
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT, FAN, configured, accepted, rejected
from test_onlyfans_loopback_transport_pg import peer_server, transport_for
from db.models import (AccountAiConfig, CatalogItem, ContentOffer, Fan, GrokDailyCost,
    LadderQuote, LadderState, Message, OfConversationState, Transaction, VaultItem, VaultSend)
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfNativePlan, OfTextIntent
from onlyfans_runtime.native_planning import SyntheticNativeModel
from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import Dispatch, SyntheticTextTransport
from ownership import owned_media_in_session

ASK = "Can you send me a video?"


def shelf(engine, *, account=ACCOUNT):
    with Session(engine) as s, s.begin():
        for mid, kind in [(11,"image"),(12,"video"),(13,"image")]:
            s.add(VaultItem(account_id=account,media_id=mid,kind=kind,created_at=utcnow(),
                content_hash="synthetic-file-"+str(mid)))
        item=CatalogItem(account_id=account,kind="video",description_for_ai="A short scenic video with two still photos.",
            media_ids="[11,12,13]",preview_media_ids="[11]",price_cents=1000,enabled=True)
        s.add(item);s.flush();return item.id


def media_policy(authority):
    authority.configure(TENANT,ACCOUNT,expected_revision=1,enabled=True,media_enabled=True,
        limit_units=20,throttle_seconds=1,actor="synthetic-operator",reason="Offline existing-media test")


def manual(database):
    _,engine=database
    store,authority=configured(database)
    item=shelf(engine);media_policy(authority)
    return store,authority,item


def native(database, monkeypatch, *, ask=ASK):
    _,engine=database
    store,authority,_,planner=ready(database,monkeypatch)
    item=shelf(engine);media_policy(authority)
    with Session(engine) as s,s.begin():
        s.get(AccountAiConfig,ACCOUNT).ai_chatter_config_json=canonical({
            "pack_send_enabled":True,"pack_on_ask_enabled":True,"qualification_gate_enabled":True})
    store.record(TENANT,ACCOUNT,event(mid=2,body=ask));store.drain(TENANT,ACCOUNT);due(engine)
    claim=store.claim(TENANT);assert claim is not None
    return store,authority,claim,planner,item


def intent(authority,item,*,key="media-1"):
    return authority.prepare(TENANT,ACCOUNT,FAN,command_key=key,expected_revision=0,
        body="The approved existing set.",origin="manual",actor="synthetic-operator",catalog_item_id=item)


def no_commerce(engine):
    with Session(engine) as s:
        assert all(s.scalar(select(func.count()).select_from(m))==0 for m in (ContentOffer,VaultSend,Transaction))


def test_manual_media_requires_explicit_permission_and_preserves_text_cap(database):
    _,engine=database
    store,authority=configured(database);item=shelf(engine)
    with pytest.raises(Refused,match="media delivery disabled"):intent(authority,item)
    media_policy(authority)
    identity=intent(authority,item);assert intent(authority,item)==identity
    no_commerce(engine)
    wire=SyntheticTextTransport(ACCOUNT)
    assert authority.dispatch(TENANT,ACCOUNT,identity,wire)=="accepted"
    assert authority.dispatch(TENANT,ACCOUNT,identity,wire)=="accepted" and len(wire.calls)==1
    sent=wire.calls[0]
    assert sent.media_ids==(11,12,13) and sent.preview_ids==(11,) and sent.price_cents==1000
    with Session(engine) as s:
        row=s.get(OfTextIntent,(ACCOUNT,identity));offer=s.get(ContentOffer,row.offer_id)
        message=s.get(Message,(ACCOUNT,FAN,9001))
        assert json.loads(message.media_ids)==[11,12,13] and message.media_count==3 and message.price_cents==1000
        assert message.is_paid is False and message.purchased_at is None
        assert (offer.status,offer.resolved_by,offer.price_cents,offer.offer_message_id)==("open",None,1000,9001)
        assert len(list(s.scalars(select(VaultSend))))==3
        assert owned_media_in_session(s,ACCOUNT,FAN)==set()
        assert s.get(OfDeliveryPolicy,ACCOUNT).used_units==1
        assert list(s.scalars(select(Transaction)))==[]
    store.close()


@pytest.mark.parametrize("change",["price","description","files","removed","hash","preview","disabled","legacy","owned","policy"])
@pytest.mark.parametrize("stage",["queued","admitted"])
def test_media_changed_or_owned_before_wire_is_cancelled_without_post(database,change,stage):
    _,engine=database;store,authority,item=manual(database)
    identity=intent(authority,item)
    grant=authority.admit(TENANT,ACCOUNT,identity) if stage=="admitted" else None
    with Session(engine) as s,s.begin():
        row=s.get(CatalogItem,item)
        if change=="price":row.price_cents=1100
        elif change=="description":row.description_for_ai="A newer approved description."
        elif change=="files":row.media_ids="[11,13]"
        elif change=="removed":s.get(VaultItem,(ACCOUNT,12)).removed_at=utcnow()
        elif change=="hash":s.get(VaultItem,(ACCOUNT,12)).content_hash="changed-file"
        elif change=="preview":row.preview_media_ids="[13]"
        elif change=="disabled":row.enabled=False
        elif change=="legacy":row.script_id=999 # defer FK check by using disabled below
        elif change=="owned":s.add(VaultSend(account_id=ACCOUNT,fan_id=FAN,media_id=12,was_purchased=True))
        elif change=="policy":s.get(OfDeliveryPolicy,ACCOUNT).media_enabled=False
        if change=="legacy":
            row.script_id=None;row.is_free_teaser=True
    if stage=="queued":assert authority.admit(TENANT,ACCOUNT,identity)=="cancelled"
    else:assert authority.consume_dispatch(TENANT,grant) is None
    with Session(engine) as s:
        row=s.get(OfTextIntent,(ACCOUNT,identity))
        assert row.wire_started_at is None and row.status=="cancelled"
        assert s.get(OfDeliveryPolicy,ACCOUNT).used_units==0
        assert s.get(OfConversationState,(ACCOUNT,FAN)).unresolved_attempt_id is None
        assert list(s.scalars(select(ContentOffer)))==[]
    store.close()


def test_lost_media_acceptance_retains_one_hold_and_never_fabricates_sale(database):
    _,engine=database;store,authority,item=manual(database)
    identity=intent(authority,item);wire=SyntheticTextTransport(ACCOUNT,outcome="accepted_lost_response")
    assert authority.dispatch(TENANT,ACCOUNT,identity,wire)=="unknown"
    assert authority.dispatch(TENANT,ACCOUNT,identity,wire)=="unknown" and len(wire.calls)==1
    no_commerce(engine)
    with Session(engine) as s:
        assert list(s.scalars(select(Message).where(Message.direction=="out")))==[]
        assert s.get(OfDeliveryPolicy,ACCOUNT).used_units==1
    grant=wire.calls[0]
    assert authority.record_accepted(TENANT,ACCOUNT,identity,grant.token,accepted(grant))=="accepted"
    assert authority.materialize(TENANT,ACCOUNT,identity)
    assert authority.materialize(TENANT,ACCOUNT,identity)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(ContentOffer))==1
        assert s.scalar(select(func.count()).select_from(VaultSend))==3
    store.close()


def test_saved_media_receipt_uses_original_files_after_catalogue_edit(database):
    _,engine=database;store,authority,item=manual(database)
    identity=intent(authority,item);grant=authority.admit(TENANT,ACCOUNT,identity)
    assert authority.consume_dispatch(TENANT,grant)==grant
    assert authority.record_accepted(TENANT,ACCOUNT,identity,grant.token,accepted(grant))=="accepted"
    with Session(engine) as s,s.begin():
        row=s.get(CatalogItem,item);row.media_ids="[13]";row.price_cents=500;row.enabled=False
    assert authority.materialize(TENANT,ACCOUNT,identity)
    with Session(engine) as s,s.begin():
        message=s.get(Message,(ACCOUNT,FAN,9001));offer=s.scalar(select(ContentOffer))
        assert json.loads(message.media_ids)==[11,12,13] and offer.price_cents==1000
        offer.status="delivered";offer.resolved_by="ppv_ledger";message.is_paid=True
        s.get(CatalogItem,item).media_ids="[999]"
    with Session(engine) as s:assert owned_media_in_session(s,ACCOUNT,FAN)=={11,12,13}
    store.close()


@pytest.mark.parametrize("change",[{"media_ids":(11,13)},{"preview_ids":()},{"price_cents":999}])
def test_modified_media_grant_cannot_consume_the_original_wire_permission(database,change):
    _,engine=database;store,authority,item=manual(database)
    identity=intent(authority,item);grant=authority.admit(TENANT,ACCOUNT,identity)
    with pytest.raises(Refused):authority.consume_dispatch(TENANT,replace(grant,**change))
    assert authority.consume_dispatch(TENANT,grant)==grant
    with pytest.raises(Refused):authority.consume_dispatch(TENANT,grant)
    no_commerce(engine)
    store.close()


def test_native_reply_and_existing_media_use_one_call_quote_and_real_http(database,monkeypatch):
    _,engine=database;store,authority,claim,planner,item=native(database,monkeypatch)
    model=SyntheticNativeModel("Here is the available choice.",sale={"item_id":item,"quote":ASK},reported_cost_millicents=7)
    plan=planner.prepare(TENANT,claim,model)
    with Session(engine) as s:
        saved=s.get(OfNativePlan,(ACCOUNT,plan));assert saved.status=="ready",saved.reason
        sale=json.loads(saved.sale_json);price=sale["price_cents"]
        assert sale["source_message_id"]==2 and sale["source_quote"]==ASK
        assert "USD $" in saved.body and "A short scenic video" in saved.body
        assert "item_id" not in saved.body and "source_quote" not in saved.body
    assert planner.prepare(TENANT,claim,model)==plan and len(model.calls)==1
    no_commerce(engine)
    identity=planner.queue(TENANT,ACCOUNT,plan)
    assert planner.queue(TENANT,ACCOUNT,plan)==identity
    with Session(engine) as s:
        from onlyfans_runtime import media_delivery, native_sales, native_context
        row=s.get(OfTextIntent,(ACCOUNT,identity))
        assert authority._media_current(s,row,s.get(OfDeliveryPolicy,ACCOUNT),utcnow()), (
            media_delivery.terms_match(s,row),native_sales.eligible(s,ACCOUNT,FAN,utcnow(),own_intent_id=identity),
            native_sales.current_sale(s,ACCOUNT,FAN,json.loads(row.catalog_snapshot_json),utcnow(),own_intent_id=identity),
            json.loads(row.catalog_snapshot_json), native_sales.choice(s,ACCOUNT,FAN,item,utcnow()))
    def check(record):
        with Session(engine) as s:
            assert s.get(OfTextIntent,(ACCOUNT,identity)).wire_started_at is not None
            assert list(s.scalars(select(ContentOffer)))==[]
        payload=record["json"]["payload"]
        assert payload["media_ids"]==[11,12,13] and payload["preview_ids"]==[11] and payload["price_cents"]==price
    with peer_server(on_request=check) as peer:
        from onlyfans_runtime.loopback_transport import LoopbackTextTransport, from_server
        wire=LoopbackTextTransport(from_server(peer.server,account_id=ACCOUNT,policy_revision=2))
        assert authority.dispatch(TENANT,ACCOUNT,identity,wire)=="accepted"
        assert authority.dispatch(TENANT,ACCOUNT,identity,wire)=="accepted" and len(peer.records)==1
    with Session(engine) as s:
        assert s.scalar(select(ContentOffer)).status=="open"
        quote=s.scalar(select(LadderQuote));assert quote.price_cents==price and quote.message_id==9001 and not quote.paid
        assert s.get(LadderState,(ACCOUNT,FAN)).rung_index==1
        cost=s.scalar(select(GrokDailyCost));assert (cost.call_count,cost.cost_cents)==(1,7)
        assert s.get(Message,(ACCOUNT,FAN,9001)).is_paid is False
    store.close()


@pytest.mark.parametrize("cause",["no_ask","decline","regret","custom_owed","companion","cooldown","stopped","tapped","configuration","unknown_item"])
def test_native_sale_cannot_escape_existing_brakes_or_its_bounded_shelf(database,monkeypatch,cause):
    _,engine=database
    ask={"no_ask":"How has your day been?","decline":"I want a refund. Can you send me a video?",
         "regret":"I have no money left. Can you send me a video?"}.get(cause,ASK)
    store,authority,claim,planner,item=native(database,monkeypatch,ask=ask)
    with Session(engine) as s,s.begin():
        if cause=="custom_owed":s.get(Fan,(ACCOUNT,FAN)).customs_owed_at=utcnow()
        elif cause in {"companion","cooldown","stopped","tapped"}:
            s.add(LadderState(account_id=ACCOUNT,fan_id=FAN,status={"stopped":"stopped","tapped":"tapped","companion":"companion"}.get(cause,"idle"),
                cooldown_until=utcnow()+timedelta(hours=1) if cause=="cooldown" else None))
        elif cause=="configuration":s.get(AccountAiConfig,ACCOUNT).ai_chatter_config_json="{}"
    model=SyntheticNativeModel("Thanks for asking.",sale={"item_id":item+999 if cause=="unknown_item" else item,"quote":ask})
    identity=planner.prepare(TENANT,claim,model)
    with Session(engine) as s:
        plan=s.get(OfNativePlan,(ACCOUNT,identity));assert plan.status=="refused",(cause,plan.reason)
        assert plan.charged_millicents==1
        assert plan.sale_json is None
    no_commerce(engine);assert len(model.calls)==1
    store.close()


def test_native_sale_rechecks_policy_after_generation_and_after_admission(database,monkeypatch):
    _,engine=database;store,authority,claim,planner,item=native(database,monkeypatch)
    model=SyntheticNativeModel("Thanks for asking.",sale={"item_id":item,"quote":ASK})
    plan=planner.prepare(TENANT,claim,model);identity=planner.queue(TENANT,ACCOUNT,plan)
    grant=authority.admit(TENANT,ACCOUNT,identity);assert type(grant) is Dispatch
    with Session(engine) as s,s.begin():s.add(LadderState(account_id=ACCOUNT,fan_id=FAN,status="stopped"))
    assert authority.consume_dispatch(TENANT,grant) is None
    no_commerce(engine)
    with Session(engine) as s:assert s.get(OfDeliveryPolicy,ACCOUNT).used_units==0
    store.close()
