"""Exposure races and legitimate preview/resend cases through real dispatch."""
from copy import deepcopy
import json
import pytest
from sqlalchemy.orm import Session
from db.models import CatalogItem, Message, VaultSend
from db.onlyfans_moxy_models import MoxyEvent
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from onlyfans_runtime.store import utcnow
from test_moxy_integrated_pg import setup, decision, run, TENANT, ACCOUNT
from test_onlyfans_foundation_pg import database

@pytest.mark.parametrize("creator", ["amelia", "cristina"])
@pytest.mark.parametrize("case", ["free_after_queue","unpaid_after_queue","preview_reuse","explicit_resend","missing_count"])
def test_exposure_boundaries(database,monkeypatch,creator,case):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch,creator=creator)
    try:
        if case=="preview_reuse":
            settings=deepcopy(settings)
            settings["products"][0]["assets"].append(deepcopy(settings["products"][1]["assets"][0]))
            with Session(engine) as s,s.begin():
                item=s.get(CatalogItem,settings["catalogue_bindings"]["first"])
                item.media_ids="[7101,7102]";item.preview_media_ids="[7101]"
            service.configure(TENANT,ACCOUNT,expected_revision=1,mode="on",settings=settings,actor="fixture")
        if case in {"preview_reuse","explicit_resend","missing_count"}:
            with Session(engine) as s,s.begin():
                s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=7000,direction="out",
                    media_ids="[7101]",media_count=0 if case=="missing_count" else 1,
                    price_cents=0,is_paid=None,created_at=utcnow()))
                if case=="explicit_resend":
                    s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=7001,direction="in",
                        body="Please resend that outfit item",created_at=utcnow()))
        action="resend" if case=="explicit_resend" else "offer"
        extra={"request_kind":"resend","request_quote":"Please resend that outfit item"} if case=="explicit_resend" else {}
        payload={"source_message_id":7001} if case=="explicit_resend" else None
        identity,_=run(service,engine,decision(action,"Here is the outfit item.","first",sequence="fixture_level",**extra),payload=payload)
        with Session(engine) as s:
            saved=s.get(MoxyEvent,(ACCOUNT,identity))
            status,reason=saved.status,saved.reason
        transport=SyntheticTextTransport(ACCOUNT)
        if case=="missing_count":
            assert status=="held";assert transport.calls==[];return
        assert status=="ready", reason
        intent=service.queue(TENANT,ACCOUNT,identity)
        if case in {"free_after_queue","unpaid_after_queue"}:
            with Session(engine) as s,s.begin():
                s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=7000,direction="out",
                    media_ids="[7101]",media_count=1,price_cents=0 if case=="free_after_queue" else 7500,
                    is_paid=None if case=="free_after_queue" else False,created_at=utcnow()))
        status=authority.dispatch(TENANT,ACCOUNT,intent,transport)
        if case in {"free_after_queue","unpaid_after_queue"}:
            assert status in {"cancelled","refused"};assert transport.calls==[]
        else:
            assert status=="accepted";assert len(transport.calls)==1
            if case=="preview_reuse":
                assert transport.calls[0].preview_ids==(7101,)
    finally:store.close()

@pytest.mark.parametrize("action",["reply","offer"])
def test_unreadable_exposure_holds_sale_but_allows_normal_reply(database,monkeypatch,action):
    engine,store,service,authority,claim,settings=setup(database,monkeypatch)
    try:
        with Session(engine) as s,s.begin():
            s.add(Message(account_id=ACCOUNT,fan_id=101,message_id=7000,direction="out",
                media_ids="not-json",media_count=1,created_at=utcnow()))
        d=decision() if action=="reply" else decision("offer","Here is the outfit item.","first",sequence="fixture_level")
        identity,_=run(service,engine,d)
        with Session(engine) as s:status=s.get(MoxyEvent,(ACCOUNT,identity)).status
        if action=="offer":assert status=="held";return
        assert status=="ready"
        intent=service.queue(TENANT,ACCOUNT,identity)
        wire=SyntheticTextTransport(ACCOUNT)
        assert authority.dispatch(TENANT,ACCOUNT,intent,wire)=="accepted"
    finally:store.close()
