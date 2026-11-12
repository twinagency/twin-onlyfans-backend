"""Audit a stopped-pilot monitor incident (pinned or class/evidence-based); grants no send rights."""
import hashlib
import json
import re
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session
from conversation_engine import pilot_scope
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import OfFansConnection
from db.onlyfans_fans_intake_models import OfFansLink
from .launch_operations import _prior, _record, command_identity, digest
from .store import Refused, utcnow

INCIDENT='c59fd2459671cc3e0d3a82a1a8de26b9675cc24a4e0567e3f712ea3b89016725'
WEBHOOK_INCIDENT='c240c781574ad075939782179ab926c9f0a69cc3fc40a292f81f7d3f57e3ad14'

REPEATED_WEBHOOK_INCIDENT='df20805ad9525b3ccc54b5dba161940cbdca78bda053d916d0578bbb42a27f33'

# Must agree with ops/plan-c-supervision/incident_state.py (a test compares both owners).
# Network-check incidents use the same manual, interval/evidence-bound no-permit recovery.
# readiness_unavailable resolves only through the v3 receipt (operator evidence reference, named sends, caveat); never v2.
READINESS='readiness_unavailable'
V2_CLASSES=('credit_monitor_unavailable','provider_webhook_health_unavailable','sender_not_ready','network_check_failed')
CLASSES=V2_CLASSES+(READINESS,)
V1='plan-c-monitor-resolution.v1'
V2='plan-c-monitor-resolution.v2'
V3='plan-c-monitor-resolution.v3'
V1_FIELDS=frozenset(('version','incident_sha256','account_id','binding_id','connection_epoch','scope_revision',
    'classification','actor','audit_command_key','evidence_sha256','resolved_at'))
V2_FIELDS=V1_FIELDS|{'incident_id','affected_interval','delivery_url_sha256','config_version'}
V3_FIELDS=V2_FIELDS|{'evidence_reference','named_sends','cause_caveat'}
MAX_INTERVAL=timedelta(days=7)
READINESS_MAX_INTERVAL=timedelta(hours=24)
READINESS_CAVEAT=('UNPROVEN CAUSE: the supervisor recorded readiness_unavailable; the original readiness response was not retained. '
    'A slow model chain exceeding the completed-tick health deadline (possibly HTTP 503 loop_stale) is suspected, not proven. '
    'This record resolves the monitor stop only: it does not prove the cause, does not fix it, '
    'releases no hold, settles no send and grants no send permission.')
EVIDENCE_PREFIX='operator:fansapi:'
SEND_ID=re.compile(r'[A-Za-z0-9_-]{1,36}')

def evidence_reference_ok(ref):
    """Same rule as conversation_engine.operator.evidence_allowed('fansapi', ref); must equal the ops owner."""
    return (type(ref) is str and re.search(r'[\x00-\x1f]',ref) is None and ref.startswith(EVIDENCE_PREFIX)
        and 6<=len(ref)-len(EVIDENCE_PREFIX)<=108 and bool(ref[len(EVIDENCE_PREFIX):].strip()))

def named_sends_ok(named):
    return (type(named) is list and all(type(x) is str and SEND_ID.fullmatch(x) is not None for x in named)
        and named==sorted(set(named)))

def pinned_incidents():
    return {
        INCIDENT:{'classification':'credit_monitor_unavailable','scope_revision':12},
        WEBHOOK_INCIDENT:{'classification':'provider_webhook_health_unavailable','scope_revision':16},
        REPEATED_WEBHOOK_INCIDENT:{'classification':'provider_webhook_health_unavailable','scope_revision':20},
    }

def incident_spec(incident_hash):
    """The three pinned incidents only; any other incident is class-based (see proof_kind)."""
    return pinned_incidents().get(incident_hash)

def command_key_v2(incident_sha256,evidence_sha256,start,end,scope_revision):
    return hashlib.sha256(('monitor-resolution.v2:%s:%s:%s:%s:%d'%(incident_sha256,evidence_sha256,start,end,scope_revision)).encode()).hexdigest()

def command_key_v3(incident_sha256,evidence_sha256,start,end,scope_revision,evidence_reference,named_sends):
    return hashlib.sha256(('monitor-resolution.v3:%s:%s:%s:%s:%d:%s:%s'%(incident_sha256,evidence_sha256,start,end,scope_revision,
        hashlib.sha256(str(evidence_reference).encode()).hexdigest(),','.join(named_sends))).encode()).hexdigest()

def _moment(value):
    try:
        parsed=datetime.fromisoformat(value[:-1]+'+00:00' if value.endswith('Z') else value)
    except (AttributeError,TypeError,ValueError):return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None

def proof_kind(proof,account,binding,*,epoch=1):
    """Static proof validation shared by the audit and the agreement test. Returns 'v1' or 'v2'
    or None. The live stopped revision is compared separately, inside the locked transaction."""
    if type(proof) is not dict or not isinstance(proof.get('incident_sha256'),str):return None
    common=(proof.get('account_id')==account and proof.get('binding_id')==binding
        and type(proof.get('connection_epoch')) is int and proof['connection_epoch']==epoch and epoch>=1
        and type(proof.get('scope_revision')) is int and type(proof['scope_revision']) is not bool and proof['scope_revision']>=0
        and re.fullmatch(r'op-session:[a-f0-9]{64}',str(proof.get('actor',''))) is not None
        and all(re.fullmatch('[a-f0-9]{64}',str(proof.get(k,''))) is not None for k in ('audit_command_key','evidence_sha256'))
        and type(proof.get('resolved_at')) is str and bool(proof['resolved_at']))
    if not common:return None
    pinned=incident_spec(proof['incident_sha256'])
    if proof.get('version')==V1:
        return 'v1' if (pinned is not None and set(proof)==V1_FIELDS and proof['scope_revision']==pinned['scope_revision']
                        and proof.get('classification')==pinned['classification']) else None
    version=proof.get('version')
    if version not in (V2,V3) or pinned is not None or set(proof)!=(V2_FIELDS if version==V2 else V3_FIELDS):return None
    interval=proof.get('affected_interval')
    if type(interval) is not dict or set(interval)!={'start','end'}:return None
    start,end,done=_moment(interval['start']),_moment(interval['end']),_moment(proof['resolved_at'])
    if version==V2:
        classes,longest=V2_CLASSES,MAX_INTERVAL
        key=command_key_v2(proof['incident_sha256'],proof['evidence_sha256'],interval['start'],interval['end'],proof['scope_revision'])
    else:
        if not (evidence_reference_ok(proof['evidence_reference']) and named_sends_ok(proof['named_sends'])
                and proof['cause_caveat']==READINESS_CAVEAT):return None
        classes,longest=(READINESS,),READINESS_MAX_INTERVAL
        key=command_key_v3(proof['incident_sha256'],proof['evidence_sha256'],interval['start'],interval['end'],
                           proof['scope_revision'],proof['evidence_reference'],proof['named_sends'])
    if (proof['classification'] not in classes or start is None or end is None or done is None
            or not (start<end<=done and end-start<=longest)
            or re.fullmatch('[a-f0-9]{32}',str(proof.get('incident_id',''))) is None
            or any(re.fullmatch('[a-f0-9]{64}',str(proof.get(k,''))) is None for k in ('delivery_url_sha256','config_version'))
            or proof['audit_command_key']!=key):
        return None
    return 'v2' if version==V2 else 'v3'

def unresolved_sends(s,account):
    """Every send the database still considers unresolved: queued/dispatching/unknown text intents and
    dispatching/unknown independent admissions. A readiness stop can strand one; resolution must name each."""
    from db.onlyfans_delivery_models import OfTextIntent
    from db.onlyfans_engine_models import OfEngineIndependentAdmission
    ids=set(s.scalars(select(OfTextIntent.intent_id).where(OfTextIntent.account_id==account,
        OfTextIntent.status.in_(('queued','dispatching','unknown')))))
    ids|=set(s.scalars(select(OfEngineIndependentAdmission.attempt_id).where(OfEngineIndependentAdmission.account_id==account,
        OfEngineIndependentAdmission.status.in_(('dispatching','unknown')))))
    return sorted(ids)

ACCOUNT='of-323358988'
BINDING='01612323-b06f-5320-b623-4e032f474a6e'
PROVIDER='acct_7bf8540de5a84f459058490fee26c47f'
CREATOR='323358988'
FANS=frozenset((318022380,601164546))

def record_resolution(store,tenant,account,*,proof,actor,command_key):
    if type(proof) is not dict or proof.get('actor')!=actor or proof.get('audit_command_key')!=command_key:
        raise Refused('unsupported monitor incident resolution')
    request_hash=digest(proof)
    try:
        observed=datetime.fromisoformat(proof['resolved_at'])
        if observed.tzinfo is None:raise ValueError()
        observed=observed.astimezone(timezone.utc).replace(tzinfo=None)
        if len(proof['evidence_sha256'])!=64 or any(c not in '0123456789abcdef' for c in proof['evidence_sha256']):
            raise ValueError()
    except (KeyError,TypeError,ValueError):
        raise Refused('invalid monitor incident evidence') from None
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account)
        scope,config=pilot_scope.require(s,runtime,lock=True)
        pilot_scope.authorize(config,actor,'reconcile')
        policy=s.scalar(select(OfDeliveryPolicy).where(OfDeliveryPolicy.account_id==account).with_for_update())
        connections=list(s.scalars(select(OfFansConnection).where(OfFansConnection.account_id==account,
            OfFansConnection.binding_id==runtime.binding_id,OfFansConnection.status=='active').with_for_update()))
        connection=connections[0] if len(connections)==1 else None
        kind=proof_kind(proof,account,runtime.binding_id,epoch=connection.epoch) if connection else None
        if (kind is None or runtime.enabled or scope.status!='stopped'
                or scope.revision!=proof['scope_revision'] or policy is None or policy.enabled
                or connection is None or not config['fans']):
            raise Refused('stopped incident scope changed')
        # Pinned v1 receipts describe only the original legacy incidents. Current
        # class/evidence receipts are validated against the locked account state.
        if kind=='v1' and (account!=ACCOUNT or runtime.binding_id!=BINDING
                or connection.provider_account_id!=PROVIDER or connection.provider_creator_id!=CREATOR
                or set(config['fans'])!=FANS or connection.epoch!=1):
            raise Refused('pinned incident identity changed')
        reason=('Evidence-based resolution of the explicitly pinned monitor incident; sending remains stopped' if kind=='v1'
            else 'Class and evidence-based resolution of a monitor incident over its affected interval; sending remains stopped' if kind=='v2'
            else 'Operator-evidenced resolution of a readiness_unavailable stop with named sends and an unproven cause; sending remains stopped')
        command_identity(command_key,actor,reason,request_hash)
        prior=_prior(s,runtime,command_key,'plan_c_monitor_resolution',request_hash)
        if prior is not None:return prior
        sends=None
        if kind=='v3':
            # Inside the locked transaction: exactly the unresolved sends the operator named, no more, no fewer.
            sends=unresolved_sends(s,account)
            if sends!=proof['named_sends']:
                raise Refused('every unresolved send must be named by the operator, and only unresolved sends')
        if not timedelta(0)<=utcnow()-observed<=timedelta(minutes=5):
            raise Refused('monitor incident evidence is stale')
        link=s.get(OfFansLink,(account,connection.provider_creator_id))
        before={'scopeRevision':scope.revision,'runtimeEnabled':runtime.enabled,'policyEnabled':policy.enabled,
                'providerRequestsUsed':scope.provider_requests_used,'providerCreditsReserved':scope.provider_credits_reserved,
                'linkState':link.state if link else None,'linkSendBlocked':link.send_blocked if link else None}
        if sends is not None:before['unresolvedSends']=sends
        result={'recorded':True,'incidentSha256':proof['incident_sha256'],'evidenceSha256':proof['evidence_sha256'],
                'sendingEnabled':False,'holdsReleased':False,'proof':proof}
        if kind=='v3':result.update(namedSends=sends,evidenceReference=proof['evidence_reference'],causeCaveat=proof['cause_caveat'])
        return _record(s,runtime,command_key,'plan_c_monitor_resolution',request_hash,actor,reason,before,result,utcnow())
