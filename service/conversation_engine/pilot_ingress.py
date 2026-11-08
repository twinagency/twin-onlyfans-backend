"""Bounded signed-ingress review; immutable audit receipts survive crashes and restarts."""
from hashlib import sha256
import json
from sqlalchemy import select, func, literal, cast, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from db.models import Message, OfConversationState
from db.onlyfans_models import OfIngress, OfControlAudit
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow
from .controls import explicit_stop
from .pilot_scope import require


def _review_key(binding, event_key):
    return sha256(('plan-c-ingress-review:' + binding + ':' + event_key).encode()).hexdigest()


def _unreviewed(runtime, scope, config):
    # Keyed receipts avoid a time cursor: late application/commit or an older
    # source message cannot land behind a cursor and escape classification.
    key = func.encode(func.sha256(func.convert_to(
        literal('plan-c-ingress-review:' + runtime.binding_id + ':') + OfIngress.event_key, 'UTF8')), 'hex')
    reviewed = select(OfLaunchCommand.command_key).where(
        OfLaunchCommand.account_id==OfIngress.account_id, OfLaunchCommand.command_key==key,
        OfLaunchCommand.binding_id==runtime.binding_id, OfLaunchCommand.kind=='plan_c_ingress_review').exists()
    payload = cast(OfIngress.payload, JSONB)
    return select(OfIngress).where(OfIngress.account_id==runtime.account_id,
        OfIngress.binding_id==runtime.binding_id,OfIngress.fan_id.in_(config['fans']),OfIngress.status=='applied',
        OfIngress.source=='ws',OfIngress.received_at>=scope.created_at,
        cast(payload['created_at'].astext, DateTime(timezone=True))>=scope.created_at,
        payload['direction'].astext=='in',~reviewed)


def review_pending(s, runtime, scope, config, fan_id):
    """Same transaction and account lock as authorization and final dispatch.

    A scheduler's earlier review cannot authorize a later, unclassified input.
    A deleted display message also cannot erase a received opt-out.
    """
    return s.scalar(_unreviewed(runtime, scope, config).where(
        OfIngress.fan_id == fan_id).limit(1)) is not None


def _record_review(store, tenant, account, binding, row, control_key):
    key = _review_key(binding, row.event_key)
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        require(s, runtime)
        if runtime.binding_id != binding:
            raise Refused('creator binding changed during contact review')
        if s.get(OfLaunchCommand, (account,key)) is not None:
            return
        if control_key is not None:
            audit = s.get(OfControlAudit, (account,control_key))
            if audit is None or audit.fan_id != row.fan_id or audit.mode != 'no_contact':
                raise Refused('contact stop control evidence is unavailable')
        s.add(OfLaunchCommand(account_id=account,command_key=key,binding_id=binding,
            kind='plan_c_ingress_review',request_hash=sha256(row.payload.encode()).hexdigest(),actor='engine',
            reason='Classified signed pilot inbound for explicit contact stop',
            before_json=json.dumps({'ingressKey':row.event_key},sort_keys=True),
            result_json=json.dumps({'classification':'contact_stop' if control_key else 'not_stop',
                                    'controlKey':control_key},sort_keys=True),created_at=utcnow()))


def preserve_contact_stops(store, tenant, account, *, limit=100):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Refused('invalid contact review batch')
    with Session(store.engine) as s:
        runtime=store._runtime(s,tenant,account,lock=False)
        scope,config=require(s,runtime)
        binding=runtime.binding_id
        rows=list(s.scalars(_unreviewed(runtime,scope,config).order_by(OfIngress.received_at,OfIngress.event_key).limit(limit)))
    for row in rows:
        # Applied ingress is canonical immutable source evidence. A later edit
        # of the message display cannot erase a previously received opt-out.
        event=json.loads(row.payload)
        key=None
        if explicit_stop(event['body']):
            digest=sha256(json.dumps([account,row.fan_id,row.message_id],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
            key='engine-stop:'+digest[:52]
            for attempt in range(3):
                with Session(store.engine) as s:
                    if s.get(OfControlAudit,(account,key)) is not None:
                        break
                    state=s.get(OfConversationState,(account,row.fan_id))
                    if state is None:raise Refused('contact stop state is unavailable')
                    revision=state.revision
                try:
                    store.control(tenant,account,row.fan_id,mode='no_contact',expected_revision=revision,
                        command_key=key,actor='engine',reason=f'Fan requested no contact in message {row.message_id}',
                        action='mark_no_contact',expected_binding_id=binding)
                    break
                except Refused as exc:
                    # A concurrent reviewer may have committed this same stop
                    # using its earlier revision. Verify the durable audit next.
                    if str(exc) not in ('control revision conflict','idempotency key conflict') or attempt==2:raise
        # Commit a receipt only AFTER any stop control. A crash in between
        # replays the existing control audit instead of skipping the stop.
        _record_review(store,tenant,account,binding,row,key)
    with Session(store.engine) as s:
        runtime=store._runtime(s,tenant,account,lock=False)
        scope,config=require(s,runtime)
        pending=s.scalar(_unreviewed(runtime,scope,config).limit(1)) is not None
    return {'reviewed':len(rows),'pending':pending}
