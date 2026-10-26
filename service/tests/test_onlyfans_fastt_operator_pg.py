"""The existing FASTT manual queue and canonical authority retain paid-send evidence."""
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_fastt_controls_pg import database
from test_onlyfans_media_pg import manual
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_onlyfans_fastt_operator import Client
from db.models import ContentOffer, Message, VaultSend
from db.onlyfans_delivery_models import OfTextIntent
from onlyfans_runtime import fastt_scope
from onlyfans_runtime.fastt_manual import FasttManualTransport, dispatch_next_manual, post_operator_once


@pytest.mark.parametrize("failure",[None,"timeout","price","recipient"])
def test_paid_manual_runner_queue_preserves_money_and_unknown_hold(database,monkeypatch,failure):
    store,authority,item=manual(database)
    try:
        authority.configure(TENANT,ACCOUNT,expected_revision=2,enabled=True,media_enabled=True,
            limit_units=20,throttle_seconds=1,actor="synthetic-operator",reason="Offline manual native boundary",transport_kind="native")
        monkeypatch.setattr(fastt_scope,"owned_scope",lambda:({(ACCOUNT,FAN)},set()))
        monkeypatch.setattr(fastt_scope,"serve_scope",lambda:({(ACCOUNT,FAN)},set()))
        change=(lambda row:row.update(price=0)) if failure=="price" else (
            (lambda row:row.update(toUser={"id":FAN+1})) if failure=="recipient" else None)
        client=Client(change=change,timeout=failure=="timeout")
        transport=FasttManualTransport(ACCOUNT,lambda grant:post_operator_once(client,grant,owner_id=7001),policy_revision=3)
        identity=authority.prepare(TENANT,ACCOUNT,FAN,command_key="paid-manual",expected_revision=0,
            body="The approved existing set.",origin="manual",actor="synthetic-operator",catalog_item_id=item)
        assert dispatch_next_manual(authority,TENANT,ACCOUNT,transport)==("unknown" if failure else "accepted")
        assert dispatch_next_manual(authority,TENANT,ACCOUNT,transport) is None
        assert authority.dispatch(TENANT,ACCOUNT,identity,transport)==("unknown" if failure else "accepted")
        assert len(client.calls)==1
        assert client.calls[0][1]["json_body"]["mediaFiles"]==[11,12,13]
        assert client.calls[0][1]["json_body"]["previews"]==[11]
        assert client.calls[0][1]["json_body"]["price"]==10
        with Session(store.engine) as s:
            intent=s.get(OfTextIntent,(ACCOUNT,identity))
            assert intent.wire_started_at is not None
            assert intent.status==("unknown" if failure else "accepted")
            assert s.scalar(select(func.count()).select_from(ContentOffer))==(0 if failure else 1)
            assert s.scalar(select(func.count()).select_from(VaultSend))==(0 if failure else 3)
            message=s.get(Message,(ACCOUNT,FAN,9001))
            if failure:
                assert message is None
            else:
                assert message.price_cents==1000 and message.media_count==3 and not message.is_paid
    finally:
        store.close()
