"""Explicit positive receipt recovery; never sends, retries or infers non-delivery."""
from datetime import datetime, timedelta
import hashlib
import json
import re

from .ledger import Refused, _dt, _json
from .scope import AccountScope
from twin_bridge import store


def freeze(ledger, *, pilot_id, owner_token, attempt_id, body, terms):
    """Retain the exact admitted payload before any provider operation."""
    if type(body) is not str or len(body.encode("utf-16-le"))//2>4000:
        raise Refused("invalid-frozen-dispatch")
    with ledger._tx() as con:
        ledger._outcome_owner(con,pilot_id,owner_token,attempt_id)
        p=con.execute("SELECT * FROM native_pilots WHERE pilot_id=?",(pilot_id,)).fetchone()
        r=con.execute("SELECT * FROM native_pilot_receipts WHERE attempt_id=?",(attempt_id,)).fetchone()
        if r["state"]!="dispatching":
            raise Refused("attempt-not-dispatching")
        canonical=ledger._canonical_guard(con,p,lock=True,own_attempt=attempt_id)
        binding=None
        if canonical:
            binding=con.execute("SELECT binding_id FROM public.of_runtime WHERE account_id=?",(p["account_id"],)).fetchone()[0]
        intent={"version":1,"account_id":p["account_id"],"fan_id":p["fan_id"],"binding_id":binding,
            "body":body,"content":terms,"created_at":r["created_at"]}
        retained=json.loads(r["evidence_json"] or "{}")
        if "dispatch_intent" in retained and retained["dispatch_intent"]!=intent:
            raise Refused("frozen-dispatch-conflict")
        retained["dispatch_intent"]=intent
        if terms:retained["content_intent"]=terms
        con.execute("UPDATE native_pilot_receipts SET evidence_json=? WHERE attempt_id=?",(_json(retained),attempt_id))


def snapshot(ledger, *, pilot_id, attempt_id, account_id, fan_id):
    with store.readonly(ledger.path) as con:
        row=con.execute("SELECT p.account_id,p.fan_id,r.evidence_json,r.state FROM native_pilot_receipts r "
            "JOIN native_pilots p ON p.pilot_id=r.pilot_id WHERE r.pilot_id=? AND r.attempt_id=?",
            (pilot_id,attempt_id)).fetchone()
        if not row or (row["account_id"],row["fan_id"])!=(account_id,fan_id):
            raise Refused("recovery-scope-mismatch")
        value=json.loads(row["evidence_json"] or "{}")
        intent=value.get("dispatch_intent")
        if not intent or intent.get("version")!=1:
            raise Refused("frozen-dispatch-unavailable")
        return intent


def _matches(raw,intent):
    from onlyfans_runtime.native_protocol import message
    from twin_bridge.platform import validate_created_message
    owner=int(AccountScope.parse(intent["account_id"]).provider_id)
    fan=intent["fan_id"]
    validate_created_message(raw,chat_id=fan,creator_id=owner)
    event=message(raw,owner_id=owner,fan_id=fan,history=True)
    recipient=raw.get("toUser",raw.get("withUser"))
    terms=intent["content"]
    if (type(recipient) is not dict or recipient.get("id")!=fan
        or event.direction!="out" or event.body!=intent["body"] or raw.get("lockedText") is not False
        or event.media_ids!=tuple(terms["media_ids"] if terms else ())
        or event.price_cents!=(terms["price_cents"] if terms else 0)
        or raw.get("previews")!=(terms["preview_ids"] if terms else [])):
        raise Refused("recovery-payload-mismatch")
    if terms and "media_types" in terms and {str(m["id"]):m.get("type") for m in raw["media"]}!=terms["media_types"]:
        raise Refused("recovery-media-mismatch")
    created=datetime.fromisoformat(_dt(event.created_at))
    attempt=datetime.fromisoformat(_dt(intent["created_at"]))
    if not attempt-timedelta(seconds=5)<=created<=attempt+timedelta(minutes=5):
        raise Refused("recovery-time-mismatch")
    return event


def proof_hash(raw,intent):
    """Mutable opened/read flags do not change the identity of the sent payload."""
    event=_matches(raw,intent)
    value={"id":event.message_id,"account":intent["account_id"],"fan":event.fan_id,"body":event.body,
        "media":[{"id":m["id"],"type":m.get("type")} for m in raw["media"]],
        "price":event.price_cents,"previews":raw["previews"],"createdAt":event.created_at,"lockedText":False}
    return hashlib.sha256(_json(value).encode()).hexdigest()


def reconcile(ledger, *, pilot_id, attempt_id, account_id, fan_id, provider_message_id,
              history, complete, command_key, actor, reason, now):
    """Settle only one exact positive observation in a completely read bounded window."""
    from onlyfans_read.agency_scope import actor_allowed
    if (not actor_allowed(actor) or type(reason) is not str or not reason.strip() or len(reason)>512
        or "\x00" in reason or type(command_key) is not str or not re.fullmatch("[a-f0-9]{64}",command_key)
        or type(provider_message_id) is not int or provider_message_id<=0
        or complete is not True or type(history) is not list or len(history)>1000):
        raise Refused("invalid-recovery-command")
    intent=snapshot(ledger,pilot_id=pilot_id,attempt_id=attempt_id,account_id=account_id,fan_id=fan_id)
    matches=[]
    for raw in history:
        try:event=_matches(raw,intent)
        except Exception:continue
        matches.append((event,raw))
    if len(matches)!=1 or matches[0][0].message_id!=provider_message_id:
        raise Refused("positive-recovery-proof-unavailable")
    event,raw=matches[0]
    evidence_hash=proof_hash(raw,intent)
    audit={"command_key":command_key,"actor":actor,"reason":reason,"evidence_hash":evidence_hash,
        "provider_message_id":provider_message_id}
    now_s=_dt(now)
    with ledger._tx() as con:
        p=con.execute("SELECT * FROM native_pilots WHERE pilot_id=?",(pilot_id,)).fetchone()
        r=con.execute("SELECT * FROM native_pilot_receipts WHERE pilot_id=? AND attempt_id=?",(pilot_id,attempt_id)).fetchone()
        a=con.execute("SELECT * FROM twin_bridge_send_attempts WHERE id=?",(attempt_id,)).fetchone()
        if not p or not r or not a or (p["account_id"],p["fan_id"])!=(account_id,fan_id) or (a["account_id"],a["fan_id"])!=(account_id,fan_id):
            raise Refused("recovery-scope-mismatch")
        retained=json.loads(r["evidence_json"] or "{}")
        if retained.get("dispatch_intent")!=intent:
            raise Refused("frozen-dispatch-conflict")
        if ledger.pg:
            binding=con.execute("SELECT binding_id FROM public.of_runtime WHERE account_id=? FOR UPDATE",(account_id,)).fetchone()
            if not intent["binding_id"] or not binding or binding[0]!=intent["binding_id"]:
                raise Refused("recovery-binding-changed")
        command_hash=hashlib.sha256(_json([pilot_id,attempt_id,audit]).encode()).hexdigest()
        if ledger.pg:
            command=con.execute("SELECT binding_id,kind,request_hash FROM public.of_launch_command "
                "WHERE account_id=? AND command_key=?",(account_id,command_key)).fetchone()
            if command and (command["binding_id"],command["kind"],command["request_hash"])!=(intent["binding_id"],"native_send_reconciliation",command_hash):
                raise Refused("recovery-command-conflict")
        prior=retained.get("reconciliation")
        if prior:
            if prior!=audit or r["state"]!="sent" or a["state"]!="sent":
                raise Refused("recovery-command-conflict")
            return False
        if r["state"] not in ("dispatching","delivery_unknown") or a["state"] not in ("dispatching","delivery_unknown"):
            raise Refused("recovery-attempt-settled")
        if p["state"]=="active" and _dt(p["deadline"])>now_s:
            raise Refused("recovery-owner-live")
        if con.execute("SELECT 1 FROM native_pilot_receipts r JOIN native_pilots p ON p.pilot_id=r.pilot_id "
            "WHERE p.account_id=? AND r.provider_message_id=? AND r.attempt_id<>? LIMIT 1",
            (account_id,str(provider_message_id),attempt_id)).fetchone():
            raise Refused("provider-id-reused")
        if ledger.pg and not command:
            con.execute("INSERT INTO public.of_launch_command(account_id,command_key,binding_id,kind,request_hash,actor,reason,"
                "before_json,result_json,created_at) VALUES(?,?,?,'native_send_reconciliation',?,?,?,?,?,?)",
                (account_id,command_key,intent["binding_id"],command_hash,actor,reason,
                _json({"attemptId":attempt_id,"state":r["state"]}),_json(audit),now_s))
        retained["reconciliation"]=audit
        if intent["content"]:
            from .content import ContentPlan
            retained["content"]=ContentPlan.evidence(None,raw,intent["content"])
        encoded=_json(retained)
        con.execute("UPDATE native_pilot_receipts SET state='sent',provider_message_id=?,response_json=?,evidence_json=?,updated_at=? "
            "WHERE attempt_id=?",(str(provider_message_id),_json(raw),encoded,now_s,attempt_id))
        con.execute("UPDATE twin_bridge_send_attempts SET state='sent',provider_message_id=?,evidence_json=?,updated_at=? WHERE id=?",
            (str(provider_message_id),encoded,now_s,attempt_id))
        con.execute("UPDATE native_pilots SET state='stopped',reason='receipt-reconciled',updated_at=? WHERE pilot_id=?",(now_s,pilot_id))
        return True


def read_window(client, *, intent, max_pages=10):
    """Bounded descending history through the existing account client."""
    from onlyfans_runtime.native_protocol import message
    lower=datetime.fromisoformat(_dt(intent["created_at"]))-timedelta(seconds=5)
    owner=int(AccountScope.parse(intent["account_id"]).provider_id)
    before=None
    result=[]
    seen=set()
    previous=None
    for _ in range(max_pages):
        page=client.get_messages(intent["fan_id"],limit=100,order="desc",before_id=before)
        if type(page) is not dict or type(page.get("list")) is not list or type(page.get("hasMore")) is not bool:
            raise Refused("recovery-history-unavailable")
        rows=page["list"]
        if len(rows)>100 or (not rows and page["hasMore"]):
            raise Refused("recovery-history-unavailable")
        crossed=False
        for raw in rows:
            event=message(raw,owner_id=owner,fan_id=intent["fan_id"],history=True)
            stamp=datetime.fromisoformat(_dt(event.created_at))
            if event.message_id in seen or (before is not None and event.message_id>=before) or (previous is not None and stamp>previous):
                raise Refused("recovery-history-loop")
            seen.add(event.message_id)
            previous=stamp
            if stamp<lower:crossed=True
            else:result.append(raw)
        if crossed or not page["hasMore"]:
            return result,True
        before=min(seen)
    raise Refused("recovery-history-bound")


async def command(ledger,args):
    """Management CLI: read current scoped history, then reconcile without a POST."""
    import asyncio
    from .connection import load_verified_connection,build_client
    from .reporting import report_outcomes
    scope=AccountScope.parse(args.account)
    if (not scope.twin_form or not args.tenant or not args.fan or not args.message_id
        or bool(args.intent_id)==bool(args.pilot_id)):
        raise Refused("recovery-scope-required")
    from onlyfans_read.agency_scope import actor_allowed
    if (not actor_allowed(args.actor) or type(args.reason) is not str or not args.reason.strip() or len(args.reason)>512
        or type(args.command_key) is not str or not re.fullmatch("[a-f0-9]{64}",args.command_key)):
        raise Refused("invalid-recovery-command")
    canonical_store=None
    if args.intent_id:
        from onlyfans_runtime.store import Store
        from onlyfans_runtime.text_delivery import TextAuthority
        from onlyfans_runtime import send_recovery
        canonical_store=Store(ledger.path)
        authority=TextAuthority(canonical_store)
        intent=send_recovery.snapshot(authority,args.tenant,scope.db_id,args.fan,args.intent_id)
    else:
        if not args.attempt_id:raise Refused("recovery-scope-required")
        intent=snapshot(ledger,pilot_id=args.pilot_id,attempt_id=args.attempt_id,account_id=scope.db_id,fan_id=args.fan)
    connection=await load_verified_connection(scope)
    if connection.tenant_id!=args.tenant:
        raise Refused("recovery-scope-mismatch")
    client=await asyncio.to_thread(build_client,connection)
    try:
        me=await asyncio.to_thread(client.me)
        if str(me.get("id"))!=scope.provider_id:
            raise Refused("live-account-mismatch")
        history,complete=await asyncio.to_thread(read_window,client,intent=intent)
        if args.intent_id:
            return await asyncio.to_thread(send_recovery.reconcile,authority,args.tenant,scope.db_id,args.fan,args.intent_id,
                provider_message_id=args.message_id,history=history,complete=complete,
                command_key=args.command_key,actor=args.actor,reason=args.reason)
        changed=await asyncio.to_thread(reconcile,ledger,pilot_id=args.pilot_id,attempt_id=args.attempt_id,
            account_id=scope.db_id,fan_id=args.fan,provider_message_id=args.message_id,history=history,complete=complete,
            command_key=args.command_key,actor=args.actor,reason=args.reason,now=datetime.utcnow())
        await project(ledger,pilot_id=args.pilot_id,attempt_id=args.attempt_id,account_id=scope.db_id,fan_id=args.fan)
        await report_outcomes(account_id=scope.db_id,fan_id=args.fan,receipts=ledger.receipts(pilot_id=args.pilot_id))
        return {"recorded":True,"changed":changed,"outcome":"sent","attemptId":args.attempt_id,"resumed":False}
    finally:
        close=getattr(client,"close",None)
        if close:await asyncio.to_thread(close)
        if canonical_store is not None:canonical_store.close()


async def project(ledger, *, pilot_id,attempt_id,account_id,fan_id):
    """Repair the accepted transcript through FASTT's existing attribution writer."""
    from sqlalchemy import select
    from db.engine import get_session
    from db.models import Message,Employee
    from db.onlyfans_models import OfRuntime
    from attribution import write_outbound_attribution
    with store.readonly(ledger.path) as con:
        r=con.execute("SELECT * FROM native_pilot_receipts WHERE pilot_id=? AND attempt_id=?",(pilot_id,attempt_id)).fetchone()
        evidence=json.loads(r["evidence_json"] or "{}") if r else {}
        if not r or r["state"]!="sent" or not evidence.get("reconciliation"):
            raise Refused("reconciled-receipt-required")
        intent=snapshot(ledger,pilot_id=pilot_id,attempt_id=attempt_id,account_id=account_id,fan_id=fan_id)
        raw=json.loads(r["response_json"])
    event=_matches(raw,intent)
    terms=intent["content"]
    async with get_session() as s:
        runtime=await s.get(OfRuntime,account_id)
        if runtime is None or runtime.binding_id!=intent["binding_id"]:
            raise Refused("recovery-binding-changed")
        messages=(await s.scalars(select(Message).where(Message.account_id==account_id,Message.message_id==event.message_id))).all()
        if any(m.fan_id!=fan_id or m.direction!="out" or m.body!=event.body or m.price_cents!=event.price_cents
               or tuple(json.loads(m.media_ids))!=event.media_ids for m in messages):
            raise Refused("recovery-canonical-conflict")
        employees=(await s.scalars(select(Employee.id).where(Employee.display_name=="Automation",Employee.is_active.is_(False)))).all()
        if len(employees)!=1:
            raise Refused("attribution-sentinels-missing")
        employee=employees[0]
    await write_outbound_attribution(account_id=account_id,fan_id=fan_id,message_id=event.message_id,
        sent_by_employee_id=employee,body=event.body,price_cents=event.price_cents,
        created_at=datetime.fromisoformat(_dt(event.created_at)),automation_kind="ai_chatter",
        media_ids=list(event.media_ids),emit_live=False)
    async with get_session() as s:
        row=await s.get(Message,(account_id,fan_id,event.message_id))
        if (row is None or row.direction!="out" or row.body!=event.body or row.price_cents!=event.price_cents
            or tuple(json.loads(row.media_ids))!=event.media_ids or row.sent_by_employee_id!=employee):
            raise Refused("recovery-attribution-unavailable")
    return True
