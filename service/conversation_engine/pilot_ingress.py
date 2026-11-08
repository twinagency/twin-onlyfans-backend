"""Retain signed contact stops through paused intake and crashes, using the existing control audit."""
from hashlib import sha256
import json
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.models import Message, OfConversationState
from db.onlyfans_models import OfIngress, OfControlAudit
from onlyfans_runtime.store import Refused
from .controls import explicit_stop
from .pilot_scope import require


def preserve_contact_stops(store, tenant, account):
    with Session(store.engine) as s:
        runtime=store._runtime(s,tenant,account,lock=False)
        scope,config=require(s,runtime)
        binding=runtime.binding_id
        rows=s.execute(select(OfIngress.fan_id,OfIngress.message_id,Message.body).join(Message,
            (Message.account_id==OfIngress.account_id)&(Message.fan_id==OfIngress.fan_id)&
            (Message.message_id==OfIngress.message_id)).where(OfIngress.account_id==account,
            OfIngress.binding_id==binding,OfIngress.fan_id.in_(config['fans']),OfIngress.status=='applied',
            OfIngress.source=='ws',OfIngress.received_at>=scope.created_at,Message.direction=='in',
            Message.is_unsent.is_(False)).order_by(OfIngress.received_at).execution_options(yield_per=100))
        for fan,mid,body in rows:
            if not explicit_stop(body):continue
            digest=sha256(json.dumps([account,fan,mid],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
            key='engine-stop:'+digest[:52]
            if s.get(OfControlAudit,(account,key)) is not None:continue
            for attempt in range(3):
                with Session(store.engine) as current:
                    state=current.get(OfConversationState,(account,fan))
                    if state is None:raise Refused('contact stop state is unavailable')
                    revision=state.revision
                try:
                    store.control(tenant,account,fan,mode='no_contact',expected_revision=revision,
                        command_key=key,actor='engine',reason=f'Fan requested no contact in message {mid}',
                        action='mark_no_contact',expected_binding_id=binding)
                    break
                except Refused as exc:
                    if str(exc)!='control revision conflict' or attempt==2:raise
