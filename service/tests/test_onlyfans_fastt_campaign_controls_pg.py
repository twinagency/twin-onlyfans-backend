"""Admission-to-wire races retain campaign gates in the actual FASTT runner."""
from types import SimpleNamespace
import pytest
from sqlalchemy.orm import Session
from test_fastt_controls_pg import database,template
from test_onlyfans_campaigns_pg import configured,create,CampaignService,ACCOUNT,TENANT,FAN,add_fan
from test_onlyfans_fastt_operator import Client
from db.onlyfans_models import OfRuntime
from db.models import OfConversationState
from onlyfans_runtime import fastt_scope
from onlyfans_runtime.text_delivery import TextAuthority
from native_pilot.service import Service

def setup(database,monkeypatch,fan=FAN):
    store,authority=configured(database,mode="automated")
    authority.configure(TENANT,ACCOUNT,expected_revision=1,enabled=True,limit_units=20,
        throttle_seconds=1,actor="synthetic-operator",reason="Isolated runner race",transport_kind="native")
    monkeypatch.setenv("DATABASE_URL",database[0]);monkeypatch.setenv("ONLYFANS_WORKER_TENANT",TENANT)
    monkeypatch.setattr(fastt_scope,"owned_scope",lambda:({(ACCOUNT,fan)},set()))
    monkeypatch.setattr(fastt_scope,"serve_scope",lambda:({(ACCOUNT,fan)},set()))
    client=Client(change=lambda row:row.update(toUser={"id":fan}))
    obj=object.__new__(Service);obj.db=ACCOUNT;obj.all_fans=False
    obj.args=SimpleNamespace(fan=fan);obj.scope=SimpleNamespace(provider_id=7001)
    obj.client=client;obj._twin_store=store
    return store,authority,obj,client

@pytest.mark.parametrize("change",["pause","cancel","takeover","history","scope"])
def test_change_between_admission_and_wire_never_posts(database,monkeypatch,change):
    store,authority,runner,client=setup(database,monkeypatch)
    try:
        service=CampaignService(store);created=create(service)
        assert service.queue_due(TENANT,ACCOUNT)
        original=TextAuthority.consume_dispatch
        changed=False
        def consume(self,tenant,grant,**kw):
            nonlocal changed
            if not changed:
                changed=True
                if change in {"pause","cancel"}:
                    service.action(TENANT,ACCOUNT,created["campaignId"],command_key="e"*64,
                        actor="synthetic-operator",expected_revision=1,action=change)
                elif change=="takeover":
                    store.control(TENANT,ACCOUNT,FAN,mode="human_controlled",expected_revision=0,
                        command_key="late-takeover",actor="synthetic-operator",reason="Offline race",action="take_over")
                elif change=="history":
                    with Session(store.engine) as s,s.begin():s.get(OfRuntime,ACCOUNT).recovery_needed=True
                else:
                    monkeypatch.setattr(fastt_scope,"serve_scope",lambda:(set(),set()))
            return original(self,tenant,grant,**kw)
        monkeypatch.setattr(TextAuthority,"consume_dispatch",consume)
        runner._twin_controls()
        assert changed
        assert client.calls==[]
        runner._twin_controls()
        assert client.calls==[]
    finally:store.close()

def test_runner_reaches_campaign_recipient_beyond_32_blocked(database,monkeypatch):
    store,_,runner,client=setup(database,monkeypatch,fan=250)
    try:
        with Session(store.engine) as s,s.begin():s.get(OfConversationState,(ACCOUNT,FAN)).mode="no_contact"
        for fan in range(102,172):add_fan(store.engine,fan,mode="no_contact")
        add_fan(store.engine,250)
        create(CampaignService(store))
        for _ in range(4):
            if runner._twin_controls()=="accepted":break
        assert len(client.calls)==1
        assert client.calls[0][0]==250
    finally:store.close()
