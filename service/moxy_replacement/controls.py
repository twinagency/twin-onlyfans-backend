"""Operator controls reusing existing tenant configuration authentication."""
import json
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.onlyfans_moxy_models import MoxyAccount,MoxyFan,MoxyEvent,MoxyAudit
from onlyfans_runtime.store import Refused,utcnow
from onlyfans_runtime.native_context import canonical
from .runtime import MoxyService,_fan,account_readiness
from .customs import open_customs


def view(store,tenant,account):
    with Session(store.engine) as s,s.begin():
        store._runtime(s,tenant,account);owner=s.get(MoxyAccount,account)
        if owner is None:return {'engine':'legacy','moxySelected':False}
        rows=s.scalars(select(MoxyEvent).where(MoxyEvent.account_id==account).order_by(MoxyEvent.created_at.desc()).limit(50)).all()
        return {'engine':'moxy','moxySelected':True,'mode':owner.mode,'revision':owner.revision,
            'readiness':account_readiness(s,account,json.loads(owner.settings_json)),
            'customs':open_customs(s,account),'settings':json.loads(owner.settings_json),
            'decisions':[{'id':e.event_id,'fanId':e.fan_id,'kind':e.kind,'status':e.status,'reason':e.reason,
                'decision':json.loads(e.decision_json) if e.decision_json else None,'intentId':e.intent_id} for e in rows]}


def update_fan(store,tenant,account,fan,*,state,actor,evidence_ref):
    allowed={'obligations','ordinary_chat_only','sales_refused','financial_hardship','rejected_products','budget_cents','requested_resend_product'}
    if not actor or not evidence_ref or type(state) is not dict or not set(state)<=allowed:
        raise Refused('verified_operator_state_required')
    for flag in ('ordinary_chat_only','sales_refused','financial_hardship'):
        if flag in state and type(state[flag]) is not bool:raise Refused('invalid_fan_control')
    for o in state.get('obligations',[]):
        if not {'id','status','owner','source_ref'}<=set(o) or o['status'] not in {'open','fulfilled'} or not o['source_ref'] or not o['owner']:
            raise Refused('owned_source_backed_obligation_required')
    with Session(store.engine) as s,s.begin():
        store._runtime(s,tenant,account)
        if not s.get(MoxyAccount,account):raise Refused('moxy_owner_required')
        mf=_fan(s,account,fan);before=mf.state_json
        mf.state_json=canonical({**json.loads(before),**state});mf.revision+=1;mf.updated_at=utcnow()
        s.add(MoxyAudit(audit_id=str(uuid.uuid4()),account_id=account,actor=actor,
            before_json=canonical({'fan':fan,'state':json.loads(before)}),
            after_json=canonical({'fan':fan,'state':json.loads(mf.state_json),'evidence_ref':evidence_ref}),created_at=utcnow()))
        return mf.revision
