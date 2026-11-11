"""Budgeted account preparation using existing scope, link, vault and audit owners.

No sender, planner, media download or model runtime is imported here. Preparation
is always disabled for delivery; fixture authority can only use a mocked wire.
"""
from datetime import datetime,timedelta
from hashlib import sha256
import json
import os
import re
from urllib.parse import urlsplit,parse_qsl
from sqlalchemy import select,text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from db.onlyfans_engine_models import OfEnginePilotScope,OfFansConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused,utcnow
from . import scope_contract

VERSION="preparation.v1"

class BudgetExhausted(Refused):
    """The explicit per-account read ceiling is spent; never an automatic increase."""
class SourceBudgetExhausted(BudgetExhausted):
    """One import source's own sub-budget is spent; the other sources and the account keep their allowance."""
    def __init__(self,source):
        super().__init__("preparation source budget exhausted");self.source=source
class AuthorityChanged(Refused):
    """Stop/reconfigure happened under an in-flight pass; the newer command owns the status."""
class PreparationHold(Refused):
    """A classified permanent hold raised before any provider read."""
    def __init__(self,message,*,outcome,cause):
        super().__init__(message);self.outcome,self.cause=outcome,cause

# Account-scoped last attempt. Fixed vocabulary only: no provider body, header, token or exception text.
OUTCOMES=("complete","partial","transient","identity_failed","credential_missing","budget_exhausted","schema","held")
ACTIONS={"complete":"None needed.","partial":"None needed; the import continues automatically.",
    "transient":"None needed; the worker retries when the provider allows.",
    "exhausted":"Retries are exhausted. Check provider status, then resume preparation or request a new vault sync.",
    "identity_failed":"Check the account credential and connection in FansAPI, then resume preparation.",
    "credential_missing":"Install the approved account credential on the worker, then resume preparation.",
    "budget_exhausted":"The read ceiling is spent. Save a higher approved ceiling to continue, or leave stopped.",
    "schema":"The provider response was not understood. Resume preparation after the integration is reviewed.",
    "held":"Request a new vault sync, or resume preparation after reviewing the cause."}
_CODE=re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
def _iso(moment):return None if moment is None else moment.replace(microsecond=0).isoformat()+"Z"
def _parse(value):
    try:return datetime.fromisoformat(value[:-1] if value.endswith("Z") else value).replace(tzinfo=None)
    except (ValueError,AttributeError):return None
def attempt_record(outcome,cause,*,revision,attempts=0,retry_not_before=None,terminal=False,exhausted=False,gates=False):
    # terminal: hold until a new operator command. gates: retryNotBefore is the authoritative due time
    # (identity phase, no job yet); otherwise the vault job owns the due time and this copy is display only.
    return {"at":_iso(utcnow()),"outcome":outcome,"cause":cause,"retryNotBefore":retry_not_before,"attempts":attempts,
        "revision":revision,"terminal":terminal,"exhausted":exhausted,"gates":gates,
        "operatorAction":ACTIONS["exhausted" if exhausted else outcome]}
def read_attempt(row):
    """Validated copy of the stored record, or None. A damaged record never blocks or crashes a pass."""
    try:data=json.loads(row.last_attempt_json) if row.last_attempt_json else None
    except ValueError:return None
    if type(data) is not dict or data.get("outcome") not in OUTCOMES:return None
    cause,at,due=data.get("cause"),data.get("at"),data.get("retryNotBefore")
    attempts,revision=data.get("attempts"),data.get("revision")
    if (type(cause) is not str or not _CODE.match(cause) or type(attempts) is not int or not 0<=attempts<=1000
            or type(revision) is not int or type(data.get("terminal")) is not bool or type(data.get("exhausted")) is not bool
            or type(data.get("gates",False)) is not bool
            or (at is not None and _parse(at) is None) or (due is not None and _parse(due) is None)):return None
    return {"at":at,"outcome":data["outcome"],"cause":cause,"retryNotBefore":due,"attempts":attempts,"revision":revision,
        "terminal":data["terminal"],"exhausted":data["exhausted"],"gates":data.get("gates",False),
        "operatorAction":ACTIONS["exhausted" if data["exhausted"] else data["outcome"]]}
def _record_attempt(store,account,revision,record):
    """Compare-and-set under the scope row lock: a stop or reconfiguration that raced ahead wins."""
    try:
        with Session(store.engine) as s,s.begin():
            row=s.scalar(select(OfEnginePilotScope).where(OfEnginePilotScope.account_id==account,
                OfEnginePilotScope.scope_id>1).with_for_update())
            if row is not None and row.revision==revision and row.status=="configured":
                row.last_attempt_json=encoded(record)
    except SQLAlchemyError:pass
def _provider_failure(failure,cause,retry_after,*,revision,prior):
    from fans_api_adapter.vault_sync import MAX_RETRY_ATTEMPTS,retry_delay
    cause=cause if type(cause) is str and _CODE.match(cause) else "provider_unavailable"
    if failure=="transient":
        attempts=prior+1
        if attempts>=MAX_RETRY_ATTEMPTS:
            return attempt_record("transient","vault_retry_exhausted",revision=revision,attempts=attempts,terminal=True,exhausted=True)
        due=utcnow()+timedelta(seconds=retry_delay(attempts,retry_after))
        return attempt_record("transient",cause,revision=revision,attempts=attempts,retry_not_before=_iso(due),gates=True)
    if failure=="schema":return attempt_record("schema",cause,revision=revision,attempts=prior+1,terminal=True)
    return attempt_record("identity_failed",cause,revision=revision,attempts=prior+1,terminal=True)
def _exception_record(exc,*,revision,prior):
    from fans_api_adapter.real_transport import ProviderIdentityRefused
    if isinstance(exc,AuthorityChanged):return None
    if isinstance(exc,PreparationHold):return attempt_record(exc.outcome,exc.cause,revision=revision,terminal=True)
    if isinstance(exc,ProviderIdentityRefused):return _provider_failure(exc.failure,exc.cause,exc.retry_after,revision=revision,prior=prior)
    if isinstance(exc,BudgetExhausted):return attempt_record("budget_exhausted","provider_budget_exhausted",revision=revision,terminal=True)
    return attempt_record("held","local_refusal" if isinstance(exc,Refused) else "unclassified_error",revision=revision)
def _result_record(result,*,revision):
    error=result.get("error")
    if result["complete"]:return attempt_record("complete","import_complete",revision=revision)
    if error in (None,"page_budget_exhausted"):return attempt_record("partial","page_imported",revision=revision)
    if error in ("preparation_authorization_changed","vault_job_busy"):return None
    kind=result.get("failure")
    if error=="vault_retry_exhausted":
        return attempt_record("transient","vault_retry_exhausted",revision=revision,attempts=result.get("attempts",0),exhausted=True)
    if kind=="transient":
        return attempt_record("transient",result.get("cause") or "provider_unavailable",revision=revision,
            attempts=result.get("attempts",1),retry_not_before=result.get("retryNotBefore"))
    if kind=="schema":return attempt_record("schema",result.get("cause") or "provider_response_invalid",revision=revision,attempts=result.get("attempts",0)+1,terminal=True)
    if kind=="auth":return attempt_record("identity_failed",result.get("cause") or "provider_auth_rejected",revision=revision,attempts=result.get("attempts",0)+1,terminal=True)
    if kind=="budget":return attempt_record("budget_exhausted","provider_budget_exhausted",revision=revision,terminal=True)
    return attempt_record("held",error if _CODE.match(error) else "vault_job_held",revision=revision)
def encoded(value):return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False)
def fingerprint(value):return sha256(encoded(value).encode()).hexdigest()
def _integer(value,low,high):
    if type(value) is not int or not low<=value<=high:raise Refused("invalid preparation limit")
    return value

def validate(config):
    if type(config) is not dict or set(config)!={"contract_version","provider_account","creator_id","qualification","limits"}:
        raise Refused("invalid preparation fields")
    if config["contract_version"]!=VERSION or not isinstance(config["provider_account"],str) or not re.fullmatch(r"acct_[A-Za-z0-9_-]{1,240}",config["provider_account"]):
        raise Refused("invalid preparation provider")
    creator=config["creator_id"]
    if type(creator) is not str or not re.fullmatch(r"[1-9][0-9]{0,18}",creator) or int(creator)>=2**63:
        raise Refused("invalid preparation creator")
    q=config["qualification"];limits=config["limits"]
    if type(q) is not dict or set(q)!={"mode","reference"} or q["mode"] not in ("fixture","production") or type(q["reference"]) is not str or not q["reference"].strip() or len(q["reference"])>200:
        raise Refused("preparation qualification evidence required")
    if type(limits) is not dict or set(limits)!={"provider_requests","provider_credit_budget","provider_credits_per_request"}:
        raise Refused("explicit preparation budgets required")
    _integer(limits["provider_requests"],1,10000)
    credits=_integer(limits["provider_credit_budget"],1,10**9)
    _integer(limits["provider_credits_per_request"],1,credits)
    return json.loads(encoded(config))

def current(s,runtime,*,lock=False):
    """Preparation authority of this account: a preparation row, or its commercial promotion until it is launched.

    A promoted row that is still configured keeps drawing on the same read budget (shown through the shared three-key
    projection). Once it is active or stopped it belongs to the pilot guard / the commercial commands, never to this owner."""
    row=scope_contract.row_for(s,runtime,lock=lock)
    if row is None or row.scope_id==1:return None,None
    if scope_contract.claim(row)==scope_contract.COMMERCIAL:
        config=scope_contract.validated(row,runtime,scope_contract.COMMERCIAL)
        return (row,scope_contract.preparation_projection(config)) if row.status=="configured" else (None,None)
    if row.binding_id!=runtime.binding_id:raise Refused("preparation binding changed")
    try:config=validate(json.loads(row.config_json))
    except (ValueError,TypeError,Refused):raise Refused("invalid preparation authority") from None
    if row.config_json!=encoded(config) or row.fingerprint!=fingerprint(config):
        raise Refused("preparation authority fingerprint changed")
    return row,config

def _held_scope(s,runtime):
    return s.execute(select(OfEnginePilotScope.status,OfEnginePilotScope.config_json,OfEnginePilotScope.scope_id).where(
        OfEnginePilotScope.account_id==runtime.account_id,OfEnginePilotScope.scope_id>1)).first()

def assert_send_allowed(s,runtime):
    """Delivery admission. Row identity alone holds a preparation or damaged scope; a promoted scope sends only once active."""
    held=_held_scope(s,runtime)
    if held is None:return
    if scope_contract.claim(held)==scope_contract.COMMERCIAL:
        if held.status!="active":raise Refused("commercial scope is not active")
        return
    raise Refused("preparation cannot authorize replies or sales")

def assert_enable_allowed(s,runtime):
    """Policy enablement: preparation and damaged scopes never; a promoted scope only through its operator's activation."""
    held=_held_scope(s,runtime)
    if held is not None and scope_contract.claim(held)!=scope_contract.COMMERCIAL:
        raise Refused("preparation cannot authorize replies or sales")

def _disabled(s,runtime):
    policy=s.get(OfDeliveryPolicy,runtime.account_id)
    if runtime.enabled or policy is None or policy.enabled or policy.media_enabled or policy.transport_kind!="fansapi":
        raise Refused("preparation requires disabled runtime and FansAPI delivery policy")
    return policy

def _command(key,actor,reason):
    from onlyfans_read.agency_scope import actor_allowed
    if type(key) is not str or re.fullmatch("[0-9a-f]{64}",key) is None or not actor_allowed(actor) or type(reason) is not str or not reason.strip() or len(reason)>512:
        raise Refused("managing preparation command required")

def _view(row,config):
    return {"configured":True,"revision":row.revision,"status":row.status,"fingerprint":row.fingerprint,"config":config,
        "providerRequestsUsed":row.provider_requests_used,"providerCreditsReserved":row.provider_credits_reserved,
        "automaticRepliesEnabled":False,"salesEnabled":False,
        "historyCoverage":"pending","ownershipCoverage":"pending"}

def configure(store,tenant,account,config,*,expected_revision,command_key,actor,reason):
    _command(command_key,actor,reason);config=validate(config);_integer(expected_revision,0,2**31-1)
    request_hash=fingerprint(["configure",tenant,account,config,expected_revision,actor,reason])
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account)
        prior=s.get(OfLaunchCommand,(account,command_key))
        if prior is not None:
            if prior.kind!="preparation" or prior.request_hash!=request_hash:raise Refused("preparation command conflict")
            return {**json.loads(prior.result_json),"replayed":True}
        # A new account has no selected connector yet. This explicit FansAPI
        # preparation command uses the shared policy owner, always disabled.
        if s.get(OfDeliveryPolicy,account) is None and not runtime.enabled:
            from onlyfans_runtime.text_delivery import TextAuthority
            TextAuthority(store)._configure_in_session(s,runtime,expected_revision=0,
                enabled=False,media_enabled=False,limit_units=0,throttle_seconds=1,
                transport_kind="fansapi",actor=actor,reason=reason)
        _disabled(s,runtime)
        from db.onlyfans_delivery_models import OfTextIntent
        from db.onlyfans_engine_models import OfEngineIndependentAdmission
        unresolved=s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id==account,
            OfTextIntent.status.in_(("dispatching","unknown"))).limit(1))
        independent=s.scalar(select(OfEngineIndependentAdmission.attempt_id).where(
            OfEngineIndependentAdmission.account_id==account,OfEngineIndependentAdmission.status.in_(("dispatching","unknown"))).limit(1))
        if unresolved is not None or independent is not None:
            raise Refused("reconcile unresolved sends before preparation")
        raw=s.scalar(select(OfEnginePilotScope).where(OfEnginePilotScope.account_id==account).with_for_update())
        if raw is not None and raw.scope_id==1:raise Refused("existing pilot authority is retained")
        if raw is not None and scope_contract.claim(raw)==scope_contract.COMMERCIAL:raise Refused("promoted to a commercial scope; preparation is no longer configurable")
        row,old=current(s,runtime,lock=True)
        if (row.revision if row else 0)!=expected_revision:raise Refused("preparation revision conflict")
        if old is not None and (old["provider_account"],old["creator_id"],old["qualification"]["mode"])!=(config["provider_account"],config["creator_id"],config["qualification"]["mode"]):
            raise Refused("preparation provider identity is retained")
        if row is not None and (config["limits"]["provider_requests"]<row.provider_requests_used or config["limits"]["provider_credit_budget"]<row.provider_credits_reserved):
            raise Refused("preparation reservations are retained")
        before=_view(row,old) if row else {}
        now=utcnow()
        if row is None:
            row=OfEnginePilotScope(scope_id=s.scalar(text("SELECT nextval('of_engine_preparation_scope_seq')")),
                account_id=account,binding_id=runtime.binding_id,revision=1,status="configured",
                config_json=encoded(config),fingerprint=fingerprint(config),
                provider_requests_used=0,provider_credits_reserved=0,created_at=now,updated_at=now)
            s.add(row)
        elif row.config_json!=encoded(config) or row.status!="configured":
            row.revision+=1;row.status="configured";row.active_since=None
            row.config_json=encoded(config);row.fingerprint=fingerprint(config);row.updated_at=now
            row.last_attempt_json=None
        else:
            # Same authority, new explicit command: the operator's answer to a permanent hold.
            held=read_attempt(row)
            if held is not None and held["terminal"]:row.last_attempt_json=None;row.updated_at=now
        result=_view(row,config)
        s.add(OfLaunchCommand(account_id=account,command_key=command_key,binding_id=runtime.binding_id,
            kind="preparation",request_hash=request_hash,actor=actor,reason=reason,
            before_json=encoded(before),result_json=encoded(result),created_at=now))
        return {**result,"replayed":False}

def stop(store,tenant,account,*,expected_revision,command_key,actor,reason):
    _command(command_key,actor,reason)
    request_hash=fingerprint(["stop",tenant,account,expected_revision,actor,reason])
    with Session(store.engine) as s,s.begin():
        runtime=store._runtime(s,tenant,account)
        prior=s.get(OfLaunchCommand,(account,command_key))
        if prior is not None:
            if prior.kind!="preparation" or prior.request_hash!=request_hash:raise Refused("preparation command conflict")
            return {**json.loads(prior.result_json),"replayed":True}
        raw=s.scalar(select(OfEnginePilotScope).where(OfEnginePilotScope.account_id==account).with_for_update())
        if raw is not None and raw.scope_id>1 and scope_contract.claim(raw)==scope_contract.COMMERCIAL:
            raise Refused("promoted to a commercial scope; stop it through the commercial command")
        row,config=current(s,runtime,lock=True)
        if row is None or type(expected_revision) is not int or row.revision!=expected_revision:raise Refused("preparation revision conflict")
        before=_view(row,config)
        if row.status!="stopped":row.revision+=1;row.status="stopped";row.active_since=None;row.updated_at=utcnow()
        result=_view(row,config)
        s.add(OfLaunchCommand(account_id=account,command_key=command_key,binding_id=runtime.binding_id,
            kind="preparation",request_hash=request_hash,actor=actor,reason=reason,
            before_json=encoded(before),result_json=encoded(result),created_at=utcnow()))
        return {**result,"replayed":False}

def view(store,tenant,account):
    from db.onlyfans_vault_models import OfVaultSyncJob
    with Session(store.engine) as s:
        runtime=store._runtime(s,tenant,account,lock=False);row,config=current(s,runtime)
        promoted=False
        if row is None:
            # A promoted scope stays visible here in every status (its detail lives in the commercial view).
            held=scope_contract.row_for(s,runtime)
            if held is not None and held.scope_id>1 and scope_contract.claim(held)==scope_contract.COMMERCIAL:
                row=held;promoted=True
                config=scope_contract.preparation_projection(scope_contract.validated(held,runtime,scope_contract.COMMERCIAL))
        if row is None:
            policy=s.get(OfDeliveryPolicy,account)
            return {"configured":False,"canPrepare":not runtime.enabled and
                (policy is None or not policy.enabled and not policy.media_enabled and policy.transport_kind=="fansapi")}
        result={"configured":True,**_view(row,config)}
        if promoted or row.transition_command_key is not None:result["commercial"]={"state":"promoted","status":row.status}
        connection=s.scalar(select(OfFansConnection).where(OfFansConnection.account_id==account,
            OfFansConnection.binding_id==runtime.binding_id,OfFansConnection.status=="active",
            OfFansConnection.provider_account_id==config["provider_account"],OfFansConnection.provider_creator_id==config["creator_id"]))
        from db.onlyfans_fans_intake_models import OfFansLink
        link=s.get(OfFansLink,(account,config["creator_id"]))
        result["connected"]=bool(connection is not None and link is not None and link.state=="connected"
            and link.provider_account_id==config["provider_account"])
        job=s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id==account,
            OfVaultSyncJob.binding_id==runtime.binding_id,OfVaultSyncJob.provider_kind=="fansapi",
            OfVaultSyncJob.provider_account==config["provider_account"],
            OfVaultSyncJob.connection_revision==connection.epoch).order_by(OfVaultSyncJob.requested_at.desc()).limit(1)) if connection else None
        result["vault"]=None if job is None else {"jobId":job.job_id,"status":job.status,
            "pagesRead":job.pages_read,"itemsSeen":job.items_seen,"error":job.last_error,
            "selected":len(json.loads(job.window_json).get("selected",[])),
            "retryAttempts":job.retry_attempts,"retryNotBefore":_iso(job.retry_not_before)}
        attempt=read_attempt(row)
        result["lastAttempt"]=None if attempt is None else {k:attempt[k] for k in
            ("at","outcome","cause","retryNotBefore","attempts","exhausted","operatorAction")}
        # Honest field for a failed latest attempt; absent when the last attempt made progress.
        result["failure"]=None if attempt is None or attempt["outcome"] in ("complete","partial") else {
            "outcome":attempt["outcome"],"cause":attempt["cause"],"retryNotBefore":attempt["retryNotBefore"],
            "exhausted":attempt["exhausted"],"operatorAction":attempt["operatorAction"]}
        return result

class PreparationRequestGuard:
    """One short locked reservation before each allowlisted GET; never refunds."""
    def __init__(self,store,tenant,account,revision):
        self.store,self.tenant,self.account,self.revision=store,tenant,account,revision
    def ensure_current(self,s):
        runtime=self.store._runtime(s,self.tenant,self.account)
        row,config=current(s,runtime,lock=True)
        if row is None or row.status!="configured" or row.revision!=self.revision:
            raise AuthorityChanged("preparation authorization changed")
        _disabled(s,runtime)
        return runtime,row,config
    def __call__(self,method,url,mock_wire=False):
        from fans_api_adapter.protocol import ALLOWED_BASES
        if method!="GET" or type(url) is not str:raise Refused("preparation permits read-only requests")
        parsed=urlsplit(url)
        with Session(self.store.engine) as s,s.begin():
            _,row,config=self.ensure_current(s)
            if (config["qualification"]["mode"]=="fixture")!=(mock_wire is True):
                raise Refused("preparation wire does not match qualification")
            if parsed.scheme!="https" or parsed.netloc not in {urlsplit(b).netloc for b in ALLOWED_BASES} or parsed.username or parsed.password or parsed.fragment:
                raise Refused("preparation provider endpoint refused")
            source=None
            if parsed.path=="/api/accounts":
                if parsed.query:raise Refused("preparation identity query refused")
            elif parsed.path=="/api/"+config["provider_account"]+"/media/vault":
                pairs=parse_qsl(parsed.query,keep_blank_values=True)
                query=dict(pairs)
                if (len(pairs)!=4 or set(query)!={"limit","offset","field","sort"} or query["field"]!="recent" or query["sort"]!="desc"
                    or not query["limit"].isdecimal() or not 10<=int(query["limit"])<=24
                    or not query["offset"].isdecimal() or not 0<=int(query["offset"])<=4800):
                    raise Refused("preparation vault page refused")
                source="vault_window"
            else:
                # Read-only chat history, transactions and chargebacks of a configured history import (0136).
                from fans_api_adapter import history_import
                source=history_import.validate_read(s,self.account,config,parsed)
                if source is None:raise Refused("preparation provider endpoint refused")
            limits=config["limits"]
            if row.provider_requests_used>=limits["provider_requests"] or row.provider_credits_reserved+limits["provider_credits_per_request"]>limits["provider_credit_budget"]:
                raise BudgetExhausted("preparation provider budget exhausted")
            now=utcnow()
            if source is not None:
                # Refusal here rolls back this whole transaction, so no level keeps a reservation for a refused read.
                from fans_api_adapter import history_import
                history_import.reserve(s,self.account,source,limits["provider_credits_per_request"],now)
            row.provider_requests_used+=1;row.provider_credits_reserved+=limits["provider_credits_per_request"];row.updated_at=now

def prepare_one(store,tenant,account,*,client_factory):
    """One identity proof and at most one shared-import page; no send code path.

    Every provider I/O (client, identity, page, reservation) is preceded by the durable checks
    below: permanent holds need a new operator command, transient failures wait for their due time.
    """
    from fans_api_adapter.intake import FansWebhookAdapter
    from fans_api_adapter.vault_sync import sync_vault
    from fans_api_adapter import history_import
    from db.onlyfans_vault_models import OfVaultSyncJob
    # Session-level advisory lock across I/O; no transaction spans HTTP.
    key="preparation:"+tenant+":"+account
    with store.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
        acquired=lock.scalar(text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))"),{"key":key})
        if not acquired:return {"state":"busy"}
        try:
            with Session(store.engine) as s,s.begin():
                runtime=store._runtime(s,tenant,account);row,config=current(s,runtime,lock=True)
                if row is None or row.status!="configured":return {"state":"stopped"}
                _disabled(s,runtime)
                now=utcnow();revision=row.revision
                connection=s.scalar(select(OfFansConnection).where(OfFansConnection.account_id==account,OfFansConnection.status=="active"))
                same=connection is not None and (connection.binding_id,connection.provider_account_id,connection.provider_creator_id)==(runtime.binding_id,config["provider_account"],config["creator_id"])
                from db.onlyfans_fans_intake_models import OfFansLink
                link=s.get(OfFansLink,(account,config["creator_id"]))
                latest=None;vault_done=False;vault_job=None;history_started=history_import.started(s,account)
                if same and link is not None and link.provider_account_id==config["provider_account"] and link.state=="connected":
                    latest=s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id==account,
                        OfVaultSyncJob.binding_id==runtime.binding_id,OfVaultSyncJob.connection_revision==connection.epoch,
                        OfVaultSyncJob.provider_kind=="fansapi",OfVaultSyncJob.provider_account==config["provider_account"])
                        .order_by(OfVaultSyncJob.requested_at.desc(),OfVaultSyncJob.job_id.desc()).limit(1))
                    # A new explicit refresh supersedes the completed shortcut.
                    if latest is not None and latest.status=="complete":
                        # Preparation is finished unless a configured history import still has automatic work.
                        if not history_started:return {"state":"complete"}
                        vault_done=True;vault_job=latest.job_id
                    # A semantic import failure needs an explicit new sync command;
                    # transient read failures keep the same pending cursor.
                    if latest is not None and latest.status in ("failed","superseded"):
                        exhausted=latest.last_error=="vault_retry_exhausted"
                        known=read_attempt(row)
                        if known is None or known["revision"]!=revision or known["cause"]!=(latest.last_error or "vault_job_held"):
                            cause=latest.last_error if latest.last_error and _CODE.match(latest.last_error) else "vault_job_held"
                            row.last_attempt_json=encoded(attempt_record("transient" if exhausted else "held",cause,revision=revision,
                                attempts=latest.retry_attempts,exhausted=exhausted))
                        return {"state":"held","error":latest.last_error}
                known=read_attempt(row)
                live=known if known is not None and known["revision"]==revision else None
                if live is not None and live["terminal"]:
                    return {"state":"held","outcome":live["outcome"],"cause":live["cause"]}
                due=[x for x in (_parse(live["retryNotBefore"]) if live is not None and live["gates"] and live["retryNotBefore"] else None,
                    latest.retry_not_before if latest is not None and latest.status=="pending" else None) if x is not None]
                if due and max(due)>now:
                    return {"state":"delayed","cause":live["cause"] if live is not None else "provider_unavailable",
                        "retryNotBefore":_iso(max(due))}
                if vault_done:
                    # Nothing due means no identity read and no reservation: pacing is persisted, not an in-process timer.
                    plan=history_import.eligibility(s,account,now)
                    if plan["state"]=="idle":return {"state":"complete"}
                    if plan["state"]=="delayed":return {"state":"delayed","cause":"history_read_pacing","retryNotBefore":_iso(plan["at"])}
                limits=config["limits"]
                if row.provider_requests_used>=limits["provider_requests"] or row.provider_credits_reserved+limits["provider_credits_per_request"]>limits["provider_credit_budget"]:
                    row.last_attempt_json=encoded(attempt_record("budget_exhausted","provider_budget_exhausted",revision=revision,terminal=True))
                    return {"state":"held","outcome":"budget_exhausted","cause":"provider_budget_exhausted"}
                prior=live["attempts"] if live is not None and live["outcome"]=="transient" and not live["exhausted"] else 0
                epochs=s.scalars(select(OfFansConnection.epoch).where(OfFansConnection.account_id==account)).all()
                epoch=connection.epoch if same else max(epochs,default=0)+1
            guard=PreparationRequestGuard(store,tenant,account,revision)
            history=None
            try:
                client=client_factory(config,epoch,guard)
                FansWebhookAdapter(store).bind_verified(tenant,account,client,guard=guard)
                if vault_done:
                    result={"complete":True,"pages":0,"upserted":0,"skipped":{},"error":None,"jobId":vault_job}
                else:
                    result=sync_vault(store,tenant,account,client,max_pages=1)
                    if history_started:history_import.record_vault_outcome(store,account,result)
                if history_started:history=history_import.step(store,tenant,account,client)
            except Exception as exc:
                record=_exception_record(exc,revision=revision,prior=prior)
                if record is not None:_record_attempt(store,account,revision,record)
                raise
            record=None if vault_done else _result_record(result,revision=revision)
            if history is not None:
                if history["failure"]=="auth":
                    record=attempt_record("identity_failed",history["cause"] or "provider_auth_rejected",revision=revision,terminal=True)
                elif history["failure"]=="budget":
                    record=attempt_record("budget_exhausted","provider_budget_exhausted",revision=revision,terminal=True)
                elif vault_done and any(r["outcome"]=="ok" for r in history["reads"]):
                    record=attempt_record("partial","page_imported",revision=revision) if history["pending"] else attempt_record("complete","import_complete",revision=revision)
            if record is not None:_record_attempt(store,account,revision,record)
            out={"state":"complete" if result["complete"] and (history is None or not history["pending"]) else "partial","vault":result}
            if history is not None:
                out["history"]=history
                if history["failure"] in ("auth","budget"):out["state"]="held"
            return out
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"),{"key":key})


def approved_entries(env=None):
    """Explicit operator-installed read ceilings and credential references, default off.

    This registry is deployment input, outside git, never accepted from the API.
    Each account has its own reference and ceilings. No trial budget is inherited.
    """
    from pathlib import Path
    import stat
    env=os.environ if env is None else env
    if env.get("TWIN_FANSAPI_PREPARATION_REAL")!="1":raise Refused("real preparation is disabled")
    path=env.get("TWIN_FANSAPI_PREPARATION_REGISTRY")
    if not path:raise Refused("approved preparation registry required")
    file=Path(path).resolve();info=file.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid!=os.getuid() or info.st_size>65536:
        raise Refused("preparation registry must be private and owned")
    if any((parent/".git").exists() for parent in file.parents):raise Refused("preparation registry must be outside git")
    from fans_api_adapter.intake import _unique_object
    rows=json.loads(file.read_text(),object_pairs_hook=_unique_object)
    if type(rows) is not list or not 1<=len(rows)<=10:raise Refused("one to ten approved preparation accounts required")
    entries={}
    for row in rows:
        if type(row) is not dict or set(row)!={"tenant","account","provider_account","creator_id","env_file","limits"}:
            raise Refused("invalid preparation registry entry")
        if any(type(row[k]) is not str or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}",row[k]) for k in ("tenant","account")) or type(row["env_file"]) is not str or not row["env_file"]:
            raise Refused("invalid preparation registry identity")
        validate({"contract_version":VERSION,"provider_account":row["provider_account"],"creator_id":row["creator_id"],
            "qualification":{"mode":"production","reference":"approved deployment registry"},"limits":row["limits"]})
        key=(row["tenant"],row["account"])
        if key in entries or any(v["provider_account"]==row["provider_account"] or v["creator_id"]==row["creator_id"] for v in entries.values()):
            raise Refused("duplicate preparation registry account")
        entries[key]=row
    return entries

def production_factory(entry):
    def make(config,epoch,guard):
        from fans_api_adapter.real_transport import load_real_config,build_client,ENV_FILE
        if config["qualification"]["mode"]!="production" or (config["provider_account"],config["creator_id"])!=(entry["provider_account"],entry["creator_id"]):
            raise PreparationHold("preparation identity lacks deployment approval",outcome="held",cause="deployment_approval_missing")
        limits=config["limits"];ceiling=entry["limits"]
        if (limits["provider_requests"]>ceiling["provider_requests"] or limits["provider_credit_budget"]>ceiling["provider_credit_budget"]
                or limits["provider_credits_per_request"]!=ceiling["provider_credits_per_request"]):
            raise PreparationHold("preparation limits exceed approved deployment ceilings",outcome="held",cause="limits_exceed_ceiling")
        try:secret=load_real_config({ENV_FILE:entry["env_file"]},require_flag=False)
        except Exception:raise PreparationHold("preparation credential unavailable",outcome="credential_missing",cause="credential_unavailable") from None
        if secret.provider_account!=config["provider_account"]:
            raise PreparationHold("preparation credential account mismatch",outcome="identity_failed",cause="credential_account_mismatch")
        return build_client(secret,creator_id=config["creator_id"],epoch=epoch,request_guard=guard)
    return make

def _recorded_cause(store,account):
    try:
        with Session(store.engine) as s:
            row=s.scalar(select(OfEnginePilotScope).where(OfEnginePilotScope.account_id==account,OfEnginePilotScope.scope_id>1))
            attempt=read_attempt(row) if row is not None else None
            return attempt["cause"] if attempt is not None else "unclassified_error"
    except SQLAlchemyError:return "unclassified_error"

def run_preparation_once(store,tenant,*,env=None,stop_event=None):
    """Bounded existing-worker mode, only explicitly installed read-only accounts."""
    entries=approved_entries(env)
    results=[]
    for (approved_tenant,account),entry in entries.items():
        if stop_event is not None and stop_event.is_set():break
        if approved_tenant!=tenant:continue
        try:result=prepare_one(store,tenant,account,client_factory=production_factory(entry))
        except Exception:
            # The cause was classified and persisted by prepare_one; report only its fixed code.
            result={"state":"held","cause":_recorded_cause(store,account)}
        results.append({"accountId":account,**result})
    return results
