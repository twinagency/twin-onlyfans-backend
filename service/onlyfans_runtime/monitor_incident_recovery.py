"""Audit this one approved stopped-pilot monitor incident; grants no send rights."""
import json
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

def incident_spec(incident_hash):
    if incident_hash==INCIDENT:
        return {'classification':'credit_monitor_unavailable','scope_revision':12}
    if incident_hash==WEBHOOK_INCIDENT:
        return {'classification':'provider_webhook_health_unavailable','scope_revision':16}
    return None

ACCOUNT='of-323358988'
BINDING='01612323-b06f-5320-b623-4e032f474a6e'
PROVIDER='acct_7bf8540de5a84f459058490fee26c47f'
CREATOR='323358988'
FANS=frozenset((318022380,601164546))

def record_resolution(store,tenant,account,*,proof,actor,command_key):
    required={'version','incident_sha256','account_id','binding_id','connection_epoch','scope_revision',
              'classification','actor','audit_command_key','evidence_sha256','resolved_at'}
    spec=incident_spec(proof.get('incident_sha256')) if type(proof) is dict else None
    if (spec is None or type(proof) is not dict or set(proof)!=required or proof.get('version')!='plan-c-monitor-resolution.v1'
            or account!=ACCOUNT or proof.get('account_id')!=account
            or proof.get('binding_id')!=BINDING or type(proof.get('connection_epoch')) is not int
            or proof['connection_epoch']!=1 or type(proof.get('scope_revision')) is not int
            or proof['scope_revision']!=spec['scope_revision'] or proof.get('classification')!=spec['classification'] or proof.get('actor')!=actor
            or proof.get('audit_command_key')!=command_key):
        raise Refused('unsupported monitor incident resolution')
    request_hash=digest(proof)
    reason='Evidence-based resolution of the explicitly pinned monitor incident; sending remains stopped'
    command_identity(command_key,actor,reason,request_hash)
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
        connection=s.scalar(select(OfFansConnection).where(OfFansConnection.account_id==account,
            OfFansConnection.status=='active'))
        if (runtime.binding_id!=BINDING or runtime.enabled or scope.status!='stopped'
                or scope.revision!=proof['scope_revision'] or set(config['fans'])!=FANS
                or policy is None or policy.enabled or connection is None
                or connection.binding_id!=BINDING or connection.epoch!=proof['connection_epoch']
                or connection.provider_account_id!=PROVIDER
                or connection.provider_creator_id!=CREATOR):
            raise Refused('stopped incident scope changed')
        prior=_prior(s,runtime,command_key,'plan_c_monitor_resolution',request_hash)
        if prior is not None:return prior
        if not timedelta(0)<=utcnow()-observed<=timedelta(minutes=5):
            raise Refused('monitor incident evidence is stale')
        link=s.get(OfFansLink,(account,connection.provider_creator_id))
        before={'scopeRevision':scope.revision,'runtimeEnabled':runtime.enabled,'policyEnabled':policy.enabled,
                'providerRequestsUsed':scope.provider_requests_used,'providerCreditsReserved':scope.provider_credits_reserved,
                'linkState':link.state if link else None,'linkSendBlocked':link.send_blocked if link else None}
        result={'recorded':True,'incidentSha256':proof['incident_sha256'],'evidenceSha256':proof['evidence_sha256'],
                'sendingEnabled':False,'holdsReleased':False,'proof':proof}
        return _record(s,runtime,command_key,'plan_c_monitor_resolution',request_hash,actor,reason,before,result,utcnow())
