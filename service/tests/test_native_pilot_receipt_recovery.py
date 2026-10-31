"""Positive history proof may settle one frozen unknown; absence never permits replay."""
from copy import deepcopy
from datetime import timedelta
import pytest
from test_native_pilot_recovery import prepared
from test_native_pilot_ledger import ACCT,FAN,NOW
from native_pilot import recovery
from native_pilot.ledger import Refused
from twin_bridge.platform import escape_plain_text


def proof(tmp_path,paid=False):
    f,led,pilot,call,key,now=prepared(tmp_path)
    a=led.begin_attempt(**call,input_key=key,bubble_no=1,revision=pilot.revision,now=now)
    terms={"catalog_id":1,"kind":"photo","price_cents":1007,"media_ids":[11,12],
        "preview_ids":[11],"media_types":{"11":"photo","12":"photo"}} if paid else None
    body="A synthetic saved reply."
    recovery.freeze(led,**call,attempt_id=a.id,body=body,terms=terms)
    led.record_unknown(**call,attempt_id=a.id,now=now)
    raw={"id":1001,"fromUser":{"id":int(ACCT)},"toUser":{"id":FAN},"createdAt":now.isoformat()+"Z",
        "text":escape_plain_text(body),"media":[{"id":i,"type":"photo"} for i in ([11,12] if paid else [])],
        "mediaCount":2 if paid else 0,"price":10.07 if paid else 0,"previews":[11] if paid else [],
        "lockedText":False,"responseType":"message"}
    args=dict(pilot_id=pilot.pilot_id,attempt_id=a.id,account_id=ACCT,fan_id=FAN,
        provider_message_id=1001,history=[raw],complete=True,command_key="a"*64,
        actor="op-session:"+"b"*64,reason="Synthetic scoped receipt recovery",now=now+timedelta(seconds=30))
    return f,led,args,raw


@pytest.mark.parametrize("paid",[False,True])
def test_positive_receipt_is_audited_idempotent_and_keeps_hold_and_counts(tmp_path,paid):
    f,led,args,raw=proof(tmp_path,paid)
    before=f.sql("SELECT send_budget_used,send_budget_limit FROM twin_bridge_conversations")[0]
    frozen=recovery.snapshot(led,pilot_id=args["pilot_id"],attempt_id=args["attempt_id"],account_id=ACCT,fan_id=FAN)
    assert recovery._matches(raw,frozen).message_id==1001
    assert recovery.reconcile(led,**args) is True
    assert recovery.reconcile(led,**args) is False
    assert led.receipts(pilot_id=args["pilot_id"])[0]["state"]=="sent"
    assert f.sql("SELECT state FROM twin_bridge_send_attempts WHERE id=?",(args["attempt_id"],))[0][0]=="sent"
    assert f.sql("SELECT state FROM twin_bridge_conversations")[0][0]=="attention"
    assert f.sql("SELECT send_budget_used,send_budget_limit FROM twin_bridge_conversations")[0]==before
    assert led.status(args["pilot_id"])["model_calls"]==1
    with pytest.raises(Refused,match="recovery-command-conflict"):
        recovery.reconcile(led,**{**args,"reason":"Changed command content"})


@pytest.mark.parametrize("case",["absent","incomplete","duplicate","text","owner","fan","price","previews","media","old","legacy"])
def test_uncertain_or_conflicting_evidence_stays_unknown(tmp_path,case):
    f,led,args,raw=proof(tmp_path,True)
    if case=="absent":args["history"]=[]
    elif case=="incomplete":args["complete"]=False
    elif case=="duplicate":args["history"].append({**deepcopy(raw),"id":1002})
    elif case=="text":raw["text"]="<p>Different saved reply.</p>"
    elif case=="owner":raw["fromUser"]["id"]=999
    elif case=="fan":raw["toUser"]["id"]=999
    elif case=="price":raw["price"]=9
    elif case=="previews":raw["previews"]=[]
    elif case=="media":raw["media"][0]["id"]=13
    elif case=="old":raw["createdAt"]=(NOW-timedelta(days=1)).isoformat()
    elif case=="legacy":f.sql("UPDATE native_pilot_receipts SET evidence_json=NULL WHERE attempt_id=?",(args["attempt_id"],))
    with pytest.raises(Refused):recovery.reconcile(led,**args)
    assert led.receipts(pilot_id=args["pilot_id"])[0]["state"]=="delivery_unknown"


def test_bounded_history_does_not_treat_missing_message_as_rejection(tmp_path):
    f,led,args,raw=proof(tmp_path)
    intent=recovery.snapshot(led,pilot_id=args["pilot_id"],attempt_id=args["attempt_id"],account_id=ACCT,fan_id=FAN)
    class Client:
        def get_messages(self,*a,**kw):return {"list":[],"hasMore":False}
    history,complete=recovery.read_window(Client(),intent=intent)
    with pytest.raises(Refused,match="positive-recovery-proof-unavailable"):
        recovery.reconcile(led,**{**args,"history":history,"complete":complete})


# ---- reconcile of an attempt whose payload was never frozen (frozen-dispatch-unavailable loop) ----
import asyncio
from types import SimpleNamespace
from native_pilot import autoreconcile, runner


def _unfrozen(tmp_path):
    f,led,pilot,call,key,now=prepared(tmp_path)
    a=led.begin_attempt(**call,input_key=key,bubble_no=1,revision=pilot.revision,now=now)
    led.record_unknown(**call,attempt_id=a.id,now=now)  # no recovery.freeze: the POST was never authorised
    return f,led,a,now


def _reconcile(led,client,at,tmp=None):
    svc=SimpleNamespace(tuning=SimpleNamespace(auto_reconcile=True,reconcile_wait_s=0,absent_after_s=0),
        ledger=led,client=client,db=ACCT)
    runner.now=lambda:at
    return svc,asyncio.run(autoreconcile.reconcile_unresolved(svc))


class _Client:
    def __init__(self,rows):self.rows,self.reads,self.posts=rows,0,0
    def get_messages(self,*a,**kw):
        self.reads+=1
        return {"list":self.rows,"hasMore":False}
    def __getattr__(self,name):
        if name.startswith(("send","post")):raise AssertionError("reconcile must never send: "+name)
        raise AttributeError(name)


def _out_raw(now):
    return {"id":2001,"fromUser":{"id":int(ACCT)},"toUser":{"id":FAN},"createdAt":(now+timedelta(seconds=5)).isoformat()+"Z",
        "text":"x","media":[],"mediaCount":0,"price":0,"previews":[],"lockedText":False,"responseType":"message"}


def test_unfrozen_attempt_younger_than_threshold_is_left_alone_without_reading(tmp_path,monkeypatch):
    f,led,a,now=_unfrozen(tmp_path);c=_Client([])
    monkeypatch.setattr(runner,"now",runner.now)
    _,out=_reconcile(led,c,now+timedelta(seconds=100))
    assert out[0]["reason"]=="delivery-unresolved:frozen-dispatch-unavailable" and c.reads==0
    assert f.sql("SELECT state FROM native_pilot_receipts WHERE attempt_id=?",(a.id,))[0][0]=="delivery_unknown"


def test_unfrozen_attempt_with_complete_empty_window_settles_not_sent(tmp_path,monkeypatch):
    f,led,a,now=_unfrozen(tmp_path);c=_Client([])
    monkeypatch.setattr(runner,"now",runner.now)
    _,out=_reconcile(led,c,now+timedelta(seconds=700))
    assert out[0]["outcome"]=="not_sent" and out[0]["reason"]=="no-frozen-dispatch-no-outbound"
    assert f.sql("SELECT state FROM native_pilot_receipts WHERE attempt_id=?",(a.id,))[0][0]=="not_sent"


def test_unfrozen_attempt_with_creator_outbound_is_parked_in_attention_and_backs_off(tmp_path,monkeypatch):
    f,led,a,now=_unfrozen(tmp_path);c=_Client([_out_raw(now)])
    monkeypatch.setattr(runner,"now",runner.now)
    svc,out=_reconcile(led,c,now+timedelta(seconds=700))
    assert out[0]["reason"]=="delivery-unresolved:frozen-dispatch-outbound-present"
    assert f.sql("SELECT state FROM native_pilot_receipts WHERE attempt_id=?",(a.id,))[0][0]=="delivery_unknown"
    reads=c.reads
    runner.now=lambda:now+timedelta(seconds=800)   # inside the 300 s backoff: no new provider read
    asyncio.run(autoreconcile.reconcile_unresolved(svc))
    assert c.reads==reads
