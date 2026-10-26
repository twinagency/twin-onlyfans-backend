"""Positive unknown-send recovery and consecutive human messages on real PostgreSQL."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_fastt_controls_pg import database,template
from test_onlyfans_fastt_takeover_pg import prepared,takeover,state,report,ACCOUNT,FAN,OWNER
from test_onlyfans_text_delivery_pg import current,TENANT
from test_onlyfans_foundation_pg import seed
from onlyfans_runtime.text_delivery import TextAuthority
from db.models import Message,OfConversationState,Transaction
from db.onlyfans_delivery_models import OfTextIntent,OfDeliveryPolicy
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.text_delivery import SyntheticTextTransport
from onlyfans_runtime.store import utcnow,Refused as TwinRefused
from onlyfans_runtime import send_recovery
from native_pilot import recovery
from native_pilot.ledger import Refused
from twin_bridge.platform import escape_plain_text
from twin_bridge import store as bridge

ACTOR="op-session:"+"a"*64


def configured(database,**_):
    store=seed(*database,account=ACCOUNT,tenant=TENANT,fan=FAN)
    current(store.engine,account=ACCOUNT,fan=FAN,mode="human_controlled")
    authority=TextAuthority(store)
    authority.configure(TENANT,ACCOUNT,expected_revision=0,enabled=True,limit_units=20,throttle_seconds=1,
        actor="synthetic-operator",reason="Offline receipt and consecutive-message proof")
    return store,authority


def observed(body,when):
    return {"id":9001,"fromUser":{"id":7001},"toUser":{"id":FAN},"createdAt":when.isoformat()+"Z",
        "text":escape_plain_text(body),"media":[],"mediaCount":0,"price":0,"previews":[],
        "lockedText":False,"responseType":"message"}


def test_native_recovery_settles_takeover_without_replay_or_budget_reset(database,monkeypatch):
    store,authority,ledger,pilot,args=prepared(database,service=True)
    try:
        attempt=ledger.begin_attempt(**args)
        recovery.freeze(ledger,pilot_id=pilot.pilot_id,owner_token=OWNER,attempt_id=attempt.id,body="Synthetic reply.",terms=None)
        ledger.record_unknown(pilot_id=pilot.pilot_id,owner_token=OWNER,attempt_id=attempt.id,now=args["now"])
        assert takeover(store)==1 and state(store)[0]=="requested"
        kwargs=dict(pilot_id=pilot.pilot_id,attempt_id=attempt.id,account_id=ACCOUNT,fan_id=FAN,
            provider_message_id=9001,history=[observed("Synthetic reply.",args["now"])],complete=True,
            command_key="b"*64,actor=ACTOR,reason="Offline positive provider proof",now=args["now"]+timedelta(seconds=30))
        assert recovery.reconcile(ledger,**kwargs)
        kwargs["history"][0]["isOpened"]=True
        assert not recovery.reconcile(ledger,**kwargs)
        from db.models import Employee
        with Session(store.engine) as s,s.begin():
            s.add(Employee(id=1,display_name="Automation",is_active=False))
        asyncio.run(report(store,ledger,pilot,monkeypatch,project_attempt=attempt.id))
        # Completion is a separate audited control event after the request.
        assert state(store)==("effective",None,2)
        with Session(store.engine) as s:
            assert s.get(OfDeliveryPolicy,ACCOUNT).used_units==1
            assert s.scalar(select(func.count()).select_from(OfLaunchCommand))==1
            assert s.get(Message,(ACCOUNT,FAN,9001)).body=="Synthetic reply."
            assert s.scalar(select(func.count()).select_from(Transaction))==0
    finally:store.close()


@pytest.mark.parametrize("case",["valid","absent","duplicate","recipient","foreign_tenant"])
def test_canonical_unknown_receipt_uses_original_authority_and_audited_command(database,case):
    store,authority=configured(database,mode="human_controlled")
    try:
        identity=authority.prepare(TENANT,ACCOUNT,FAN,command_key="manual-proof",expected_revision=0,
            body="Synthetic operator reply.",origin="manual",actor=ACTOR)
        transport=SyntheticTextTransport(ACCOUNT,outcome="accepted_lost_response")
        assert authority.dispatch(TENANT,ACCOUNT,identity,transport)=="unknown"
        raw=observed(transport.calls[0].body,utcnow())
        history=[raw]
        if case=="absent":history=[]
        elif case=="duplicate":history.append({**deepcopy(raw),"id":9002})
        elif case=="recipient":raw["toUser"]["id"]=FAN+1
        tenant="another-tenant" if case=="foreign_tenant" else TENANT
        kwargs=dict(history=history,complete=True,provider_message_id=9001,command_key="c"*64,
            actor=ACTOR,reason="Offline scoped reconciliation")
        if case!="valid":
            with pytest.raises((TwinRefused,Refused)):
                send_recovery.reconcile(authority,tenant,ACCOUNT,FAN,identity,**kwargs)
            with Session(store.engine) as s:
                assert s.get(OfTextIntent,(ACCOUNT,identity)).status=="unknown"
                assert s.scalar(select(func.count()).select_from(OfLaunchCommand))==0
        else:
            result=send_recovery.reconcile(authority,tenant,ACCOUNT,FAN,identity,**kwargs)
            assert result["outcome"]=="accepted" and result["projected"] and not result["resumed"]
            raw["isOpened"]=True
            assert send_recovery.reconcile(authority,tenant,ACCOUNT,FAN,identity,**kwargs)==result
            with Session(store.engine) as s:
                assert s.scalar(select(func.count()).select_from(OfLaunchCommand))==1
                assert s.scalar(select(func.count()).select_from(Message))==1
                assert s.get(OfConversationState,(ACCOUNT,FAN)).mode=="human_controlled"
                assert s.get(OfConversationState,(ACCOUNT,FAN)).requires_attention
                assert s.get(OfDeliveryPolicy,ACCOUNT).used_units==1
        assert len(transport.calls)==1
    finally:store.close()


@pytest.mark.parametrize("change_control",[False,True])
def test_two_queued_human_messages_survive_first_projection_but_obey_controls(database,change_control):
    store,authority=configured(database,mode="human_controlled")
    try:
        identities=[authority.prepare(TENANT,ACCOUNT,FAN,command_key="consecutive-"+str(n),expected_revision=0,
            body="Operator reply "+str(n),origin="manual",actor=ACTOR) for n in range(2)]
        first=SyntheticTextTransport(ACCOUNT,message_id=9001)
        assert authority.dispatch(TENANT,ACCOUNT,identities[0],first)=="accepted"
        with Session(store.engine) as s,s.begin():
            s.get(OfDeliveryPolicy,ACCOUNT).next_dispatch_at=utcnow()-timedelta(seconds=1)
            if change_control:
                row=s.get(OfConversationState,(ACCOUNT,FAN))
                row.mode,row.revision="no_contact",row.revision+1
        second=SyntheticTextTransport(ACCOUNT,message_id=9002)
        assert authority.dispatch(TENANT,ACCOUNT,identities[1],second)==("cancelled" if change_control else "accepted")
        assert len(second.calls)==int(not change_control)
    finally:store.close()
