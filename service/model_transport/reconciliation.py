"""Manual provider-billing reconciliation under the existing worker credential owner.

This is not imported by worker startup or polling and has no generation/send path.
The operator supplies a call identity and reason, never a cost or provider ID.
"""
import json
import os
from hashlib import sha256
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from conversation_engine.model_config import ModelConfig
from conversation_engine.model_ledger import ModelLedger, _BUDGET_LOCK
from conversation_engine.operator_v2 import _key, _actor
from conversation_engine.pilot_scope import require, require_model, authorize
from db.onlyfans_engine_models import OfEngineModelCall
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused, utcnow
from .openrouter import OpenRouterClient


def pin_model_canonical(store, config, client, *, tenant, account, actor,
                        expected_pilot_revision, reason, environ=None):
    """Record provider mapping for future reservations; never retrofit older calls."""
    from conversation_engine.model_mapping import scope_key,scope_pin,_encoded,_hash
    env=os.environ if environ is None else environ
    if (env.get("ONLYFANS_PROCESS_ROLE")!="worker" or type(config) is not ModelConfig
            or not config.enabled or config.provider!="openrouter" or config.fallback_enabled
            or type(client) is not OpenRouterClient):
        raise Refused("model mapping requires the configured worker credential owner")
    actor=_actor(actor)
    if (type(expected_pilot_revision) is not int or expected_pilot_revision<1 or
            type(reason) is not str or not reason.strip() or len(reason)>512 or
            any(ord(c)<32 for c in reason)):
        raise Refused("explicit model mapping revision and reason required")
    endpoint=sha256((config.base_url.rstrip("/")+"/chat/completions").encode()).hexdigest()
    if client.endpoint_fingerprint()!=endpoint:
        raise Refused("model mapping endpoint differs from configured worker")
    def inspect(s,lock=False):
        runtime=store._runtime(s,tenant,account,lock=lock)
        pilot,raw=require(s,runtime)
        authorize(raw,actor,"reconcile")
        require_model(raw,account,config)
        if pilot.revision!=expected_pilot_revision:
            raise Refused("pilot revision changed before model mapping")
        found=scope_pin(s,account,runtime.binding_id,pilot.revision,pilot.fingerprint,endpoint,config.primary_model)
        return runtime,pilot,found
    with Session(store.engine) as s:
        runtime,pilot,found=inspect(s)
        if found is not None:
            return {**json.loads(found[0].result_json),"replayed":True}
        stamp=(runtime.binding_id,pilot.revision,pilot.fingerprint)
    mapping=client.lookup_canonical_model(config.primary_model,timeout_seconds=config.timeout_seconds)
    if mapping["requestedModel"]!=config.primary_model or mapping["endpointSha256"]!=endpoint:
        raise Refused("provider model mapping does not match the configured worker")
    with Session(store.engine) as s,s.begin():
        s.execute(text("SELECT pg_advisory_xact_lock(:key)"),{"key":_BUDGET_LOCK})
        runtime,pilot,found=inspect(s,True)
        if (runtime.binding_id,pilot.revision,pilot.fingerprint)!=stamp:
            raise Refused("pilot scope changed during model mapping")
        if found is not None:
            if (found[1]["canonicalModel"]!=mapping["canonicalModel"] or
                    found[1]["mappingReceiptSha256"]!=mapping["mappingReceiptSha256"]):
                raise Refused("provider model mapping changed during concurrent qualification")
            return {**json.loads(found[0].result_json),"replayed":True}
        data={"account":account,"binding":runtime.binding_id,"scopeRevision":pilot.revision,
              "scopeFingerprint":pilot.fingerprint,**mapping}
        key=scope_key(account,runtime.binding_id,pilot.revision,pilot.fingerprint,endpoint,config.primary_model)
        result={"ok":True,"commandKey":key,"scopeRevision":pilot.revision,
                "requestedModel":config.primary_model,"canonicalModel":mapping["canonicalModel"],
                "mappingReceiptSha256":mapping["mappingReceiptSha256"]}
        s.add(OfLaunchCommand(account_id=account,command_key=key,binding_id=runtime.binding_id,
            kind="plan_c_model_canonical_pin",request_hash=_hash(data),actor=actor,reason=reason,
            before_json=_encoded(data),result_json=_encoded(result),created_at=utcnow()))
        return {**result,"replayed":False}


def reconcile_model_usage(store, config, client, *, tenant, account, call_id, actor, command_key,
                          expected_pilot_revision, reason, environ=None):
    env = os.environ if environ is None else environ
    if (env.get('ONLYFANS_PROCESS_ROLE') != 'worker' or type(config) is not ModelConfig
            or not config.enabled or config.provider != 'openrouter' or config.fallback_enabled
            or type(client) is not OpenRouterClient):
        raise Refused('model billing reconciliation requires the configured worker credential owner')
    actor, key = _actor(actor), _key(command_key)
    if (type(expected_pilot_revision) is not int or expected_pilot_revision < 1
            or type(reason) is not str or not reason.strip() or len(reason)>512
            or any(ord(c)<32 for c in reason)):
        raise Refused('explicit model reconciliation revision and reason required')
    endpoint = sha256((config.base_url.rstrip('/')+'/chat/completions').encode()).hexdigest()
    if client.endpoint_fingerprint() != endpoint:
        raise Refused('model billing endpoint differs from configured worker')
    def scope(s, lock):
        runtime = store._runtime(s,tenant,account,lock=lock)
        pilot, raw = require(s,runtime)
        authorize(raw,actor,'reconcile')
        require_model(raw,account,config)
        return runtime,pilot,raw
    def prior(s,binding,request_hash):
        found=s.get(OfLaunchCommand,(account,key))
        if found is not None:
            if (found.binding_id!=binding or found.kind!='plan_c_model_cost_reconcile'
                    or found.request_hash!=request_hash):
                raise Refused('model reconciliation command key conflict')
            return {**json.loads(found.result_json),'replayed':True}
    def target(s,runtime,pilot,raw,lock=False):
        if pilot.revision!=expected_pilot_revision:
            raise Refused('pilot revision changed before model reconciliation')
        row=s.get(OfEngineModelCall,(account,call_id),with_for_update=lock)
        if (row is None or row.binding_id!=runtime.binding_id or row.fan_id not in raw['fans']
                or row.provider!=config.provider or row.model!=config.primary_model):
            raise Refused('model call is outside current reconciliation scope')
        if row.status!='unknown' or row.usage_known:
            raise Refused('only unknown usage can be reconciled')
        if not row.provider_generation_id or row.provider_endpoint_sha256!=endpoint:
            raise Refused('call-correlated provider billing evidence is unavailable; reservation retained')
        return row
    with Session(store.engine) as s:
        runtime,pilot,raw=scope(s,False)
        request_hash=sha256(json.dumps([account,runtime.binding_id,call_id,actor,
            expected_pilot_revision,reason],separators=(',',':')).encode()).hexdigest()
        replay=prior(s,runtime.binding_id,request_hash)
        if replay is not None:return replay
        try:row=target(s,runtime,pilot,raw)
        except Refused:
            # A concurrent identical command may have committed its whole settlement between the replay check
            # above and this unlocked read. Re-check the replay once (fresh READ COMMITTED statement) so the
            # loser returns the same idempotent replay; any other refusal is re-raised unchanged.
            replay=prior(s,runtime.binding_id,request_hash)
            if replay is not None:return replay
            raise
        observed=(row.binding_id,row.provider_generation_id,row.provider_endpoint_sha256,row.updated_at,row.outcome)
        model=row.model
    # The only external operation is one authenticated GET outside every DB transaction.
    evidence=client.lookup_generation(observed[1],timeout_seconds=config.timeout_seconds)
    if evidence['endpointSha256']!=endpoint or evidence['generationId']!=observed[1]:
        raise Refused('provider billing evidence does not match this exact call')
    with Session(store.engine) as s,s.begin():
        # Same ordering as model reservations: global budget, runtime, then call.
        s.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':_BUDGET_LOCK})
        runtime,pilot,raw=scope(s,True)
        replay=prior(s,runtime.binding_id,request_hash)
        if replay is not None:return replay
        row=target(s,runtime,pilot,raw,True)
        if observed!=(row.binding_id,row.provider_generation_id,row.provider_endpoint_sha256,row.updated_at,row.outcome):
            raise Refused('model call changed during billing review; no settlement applied')
        mapping=None
        if evidence['model']!=model:
            from conversation_engine.model_mapping import call_pin
            mapping=call_pin(s,row,endpoint)
            if mapping is None or mapping['canonicalModel']!=evidence['model']:
                raise Refused('provider billing evidence does not match this exact call')
        known_output=(row.outcome=='verified_ok' and row.response_text is not None
            and row.response_sha256==sha256(row.response_text.encode()).hexdigest())
        before={'callId':row.call_id,'fanId':str(row.fan_id),'bindingId':row.binding_id,
            'pilotRevision':pilot.revision,'status':row.status,'outcome':row.outcome,
            'reservedMicrousd':row.reserved_microusd,'costDay':row.cost_day.isoformat(),
            'promptSha256':row.prompt_sha256,'snapshotSha256':row.snapshot_sha256,
            'requestedModel':row.model,'providerReportedModel':evidence['model'],
            'canonicalMappingEvidence':mapping,'providerEvidence':evidence}
        ModelLedger.apply_verified_usage(row,evidence['actualMicrousd'],now=utcnow(),
            retain_unknown_outcome=not known_output)
        result={'ok':True,'accountId':account,'callId':call_id,'commandKey':key,
            'actualMicrousd':row.actual_microusd,'usageKnown':True,'outcomeStillUnknown':not known_output,
            'providerGenerationId':row.provider_generation_id,'providerReceiptSha256':evidence['receiptSha256'],
            'retryAuthorized':False,'sendAuthorized':False}
        s.add(OfLaunchCommand(account_id=account,command_key=key,binding_id=runtime.binding_id,
            kind='plan_c_model_cost_reconcile',request_hash=request_hash,actor=actor,reason=reason,
            before_json=json.dumps(before,sort_keys=True,separators=(',',':')),
            result_json=json.dumps(result,sort_keys=True,separators=(',',':')),created_at=utcnow()))
        return {**result,'replayed':False}
