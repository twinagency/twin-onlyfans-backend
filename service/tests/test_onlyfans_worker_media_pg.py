"""Worker-level existing-media and pacing, through actual owned HTTP."""
from datetime import timedelta
import json

import pytest
from sqlalchemy import func,select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database,event,due
from test_onlyfans_worker_pg import start,worker
from test_onlyfans_media_pg import shelf,media_policy,ASK
from test_onlyfans_loopback_transport_pg import peer_server
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT
from db.models import AccountAiConfig,ContentOffer,Transaction,VaultSend,Message
from db.onlyfans_delivery_models import OfDeliveryPolicy,OfNativePlan
from onlyfans_runtime.loopback_transport import LoopbackTextTransport,from_server
from onlyfans_runtime.native_planning import NativeTextPlanner,SyntheticNativeModel
from onlyfans_runtime.native_context import canonical
from onlyfans_runtime.store import utcnow


@pytest.mark.parametrize("mode",["accepted","accepted_drop"])
def test_worker_media_keeps_price_and_ownership_semantics_through_local_http(database,monkeypatch,mode):
    store,authority=start(database,monkeypatch);item=shelf(database[1]);media_policy(authority)
    with Session(database[1]) as s,s.begin():
        s.get(AccountAiConfig,ACCOUNT).ai_chatter_config_json=canonical({"pack_send_enabled":True,
            "pack_on_ask_enabled":True,"qualification_gate_enabled":True})
    store.record(TENANT,ACCOUNT,event(mid=2,body=ASK));store.drain(TENANT,ACCOUNT);due(database[1])
    model=SyntheticNativeModel("Here are the saved collection details.",sale={"item_id":item,"quote":ASK},reported_cost_millicents=7)
    with peer_server(mode) as peer:
        transport=LoopbackTextTransport(from_server(peer.server,account_id=ACCOUNT,policy_revision=2))
        runtime=worker(store,model=model,transport=transport);result=runtime.step()
        assert result.accepted==int(mode=="accepted") and result.unknown==int(mode!="accepted")
        for _ in range(2):runtime.step()
        assert len(peer.records)==len(model.calls)==1
        payload=peer.records[0]["json"]["payload"]
        assert payload["media_ids"]==[11,12,13] and payload["preview_ids"]==[11]
        with Session(database[1]) as s:
            terms=json.loads(s.scalar(select(OfNativePlan)).sale_json)
            assert payload["price_cents"]==terms["price_cents"]==terms["native_quote"]["price_cents"]
            assert 1000<=payload["price_cents"]<=20000
            assert f'USD ${payload["price_cents"]//100}.{payload["price_cents"]%100:02d} to unlock.' in payload["text"]
    with Session(database[1]) as s:
        assert s.scalar(select(func.count()).select_from(ContentOffer))==int(mode=="accepted")
        assert s.scalar(select(func.count()).select_from(VaultSend).where(VaultSend.was_purchased.is_(True)))==0
        assert s.scalar(select(func.count()).select_from(Transaction))==0
        assert s.scalar(select(func.count()).select_from(Message).where(Message.direction=="out"))==int(mode=="accepted")
        assert s.get(OfDeliveryPolicy,ACCOUNT).used_units==1
        if mode=="accepted":assert s.scalar(select(ContentOffer)).price_cents==payload["price_cents"]
    store.close()


def test_recovered_queue_waits_for_existing_pacing_without_model_or_cap_reset(database,monkeypatch):
    store,_=start(database,monkeypatch);planner=NativeTextPlanner(store);claim=store.claim(TENANT)
    original=SyntheticNativeModel();plan=planner.prepare(TENANT,claim,original);planner.queue(TENANT,ACCOUNT,plan)
    with Session(database[1]) as s,s.begin():s.get(OfDeliveryPolicy,ACCOUNT).next_dispatch_at=utcnow()+timedelta(seconds=10)
    replacement=SyntheticNativeModel()
    with peer_server() as peer:
        transport=LoopbackTextTransport(from_server(peer.server,account_id=ACCOUNT,policy_revision=1))
        runtime=worker(store,model=replacement,transport=transport)
        assert runtime.step().accepted==0 and peer.records==[] and replacement.calls==[]
        with Session(database[1]) as s,s.begin():
            policy=s.get(OfDeliveryPolicy,ACCOUNT);assert policy.used_units==0
            policy.next_dispatch_at=utcnow()-timedelta(seconds=1)
        assert runtime.step().accepted==1 and len(peer.records)==1 and replacement.calls==[]
    store.close()
