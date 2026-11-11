"""Budgeted account preparation using existing scope, link, vault and audit owners.

No sender, planner, media download or model runtime is imported here. Preparation
is always disabled for delivery; fixture authority can only use a mocked wire.
"""
from hashlib import sha256
import json
import os
import re
from urllib.parse import urlsplit,parse_qsl
from sqlalchemy import select,text
from sqlalchemy.orm import Session
from db.onlyfans_engine_models import OfEnginePilotScope,OfFansConnection
from db.onlyfans_delivery_models import OfDeliveryPolicy
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused,utcnow

VERSION="preparation.v1"
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
    query=select(OfEnginePilotScope).where(OfEnginePilotScope.account_id==runtime.account_id)
    row=s.scalar(query.with_for_update() if lock else query)
    if row is None or row.scope_id==1:return None,None
    if row.binding_id!=runtime.binding_id:raise Refused("preparation binding changed")
    try:config=validate(json.loads(row.config_json))
    except (ValueError,TypeError,Refused):raise Refused("invalid preparation authority") from None
    if row.config_json!=encoded(config) or row.fingerprint!=fingerprint(config):
        raise Refused("preparation authority fingerprint changed")
    return row,config

def assert_send_allowed(s,runtime):
    # Row identity is enough to hold even a damaged/stopped preparation scope.
    if s.scalar(select(OfEnginePilotScope.scope_id).where(
            OfEnginePilotScope.account_id==runtime.account_id,OfEnginePilotScope.scope_id>1)) is not None:
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
        if row is None:
            policy=s.get(OfDeliveryPolicy,account)
            return {"configured":False,"canPrepare":not runtime.enabled and
                (policy is None or not policy.enabled and not policy.media_enabled and policy.transport_kind=="fansapi")}
        result={"configured":True,**_view(row,config)}
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
            "selected":len(json.loads(job.window_json).get("selected",[]))}
        return result

class PreparationRequestGuard:
    """One short locked reservation before each allowlisted GET; never refunds."""
    def __init__(self,store,tenant,account,revision):
        self.store,self.tenant,self.account,self.revision=store,tenant,account,revision
    def ensure_current(self,s):
        runtime=self.store._runtime(s,self.tenant,self.account)
        row,config=current(s,runtime,lock=True)
        if row is None or row.status!="configured" or row.revision!=self.revision:
            raise Refused("preparation authorization changed")
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
            if parsed.path=="/api/accounts":
                if parsed.query:raise Refused("preparation identity query refused")
            elif parsed.path=="/api/"+config["provider_account"]+"/media/vault":
                pairs=parse_qsl(parsed.query,keep_blank_values=True)
                query=dict(pairs)
                if (len(pairs)!=4 or set(query)!={"limit","offset","field","sort"} or query["field"]!="recent" or query["sort"]!="desc"
                    or not query["limit"].isdecimal() or not 10<=int(query["limit"])<=24
                    or not query["offset"].isdecimal() or not 0<=int(query["offset"])<=4800):
                    raise Refused("preparation vault page refused")
            else:raise Refused("preparation provider endpoint refused")
            limits=config["limits"]
            if row.provider_requests_used>=limits["provider_requests"] or row.provider_credits_reserved+limits["provider_credits_per_request"]>limits["provider_credit_budget"]:
                raise Refused("preparation provider budget exhausted")
            row.provider_requests_used+=1;row.provider_credits_reserved+=limits["provider_credits_per_request"];row.updated_at=utcnow()

def prepare_one(store,tenant,account,*,client_factory):
    """One identity proof and at most one shared-import page; no send code path."""
    from fans_api_adapter.intake import FansWebhookAdapter
    from fans_api_adapter.vault_sync import sync_vault
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
                connection=s.scalar(select(OfFansConnection).where(OfFansConnection.account_id==account,OfFansConnection.status=="active"))
                same=connection is not None and (connection.binding_id,connection.provider_account_id,connection.provider_creator_id)==(runtime.binding_id,config["provider_account"],config["creator_id"])
                from db.onlyfans_fans_intake_models import OfFansLink
                link=s.get(OfFansLink,(account,config["creator_id"]))
                if same and link is not None and link.provider_account_id==config["provider_account"] and link.state=="connected":
                    latest=s.scalar(select(OfVaultSyncJob).where(OfVaultSyncJob.account_id==account,
                        OfVaultSyncJob.binding_id==runtime.binding_id,OfVaultSyncJob.connection_revision==connection.epoch,
                        OfVaultSyncJob.provider_kind=="fansapi",OfVaultSyncJob.provider_account==config["provider_account"])
                        .order_by(OfVaultSyncJob.requested_at.desc(),OfVaultSyncJob.job_id.desc()).limit(1))
                    # A new explicit refresh supersedes the completed shortcut.
                    # A semantic import failure needs an explicit new sync command;
                    # transient read failures keep the same pending cursor.
                    if latest is not None and latest.status=="complete":return {"state":"complete"}
                    if latest is not None and latest.status in ("failed","superseded"):
                        return {"state":"held","error":latest.last_error}
                epochs=s.scalars(select(OfFansConnection.epoch).where(OfFansConnection.account_id==account)).all()
                epoch=connection.epoch if same else max(epochs,default=0)+1
                revision=row.revision
            guard=PreparationRequestGuard(store,tenant,account,revision)
            client=client_factory(config,epoch,guard)
            FansWebhookAdapter(store).bind_verified(tenant,account,client,guard=guard)
            result=sync_vault(store,tenant,account,client,max_pages=1)
            return {"state":"complete" if result["complete"] else "partial","vault":result}
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
            raise Refused("preparation identity lacks deployment approval")
        limits=config["limits"];ceiling=entry["limits"]
        if (limits["provider_requests"]>ceiling["provider_requests"] or limits["provider_credit_budget"]>ceiling["provider_credit_budget"]
                or limits["provider_credits_per_request"]!=ceiling["provider_credits_per_request"]):
            raise Refused("preparation limits exceed approved deployment ceilings")
        secret=load_real_config({ENV_FILE:entry["env_file"]},require_flag=False)
        if secret.provider_account!=config["provider_account"]:raise Refused("preparation credential account mismatch")
        return build_client(secret,creator_id=config["creator_id"],epoch=epoch,request_guard=guard)
    return make

def run_preparation_once(store,tenant,*,env=None,stop_event=None):
    """Bounded existing-worker mode, only explicitly installed read-only accounts."""
    entries=approved_entries(env)
    results=[]
    for (approved_tenant,account),entry in entries.items():
        if stop_event is not None and stop_event.is_set():break
        if approved_tenant!=tenant:continue
        try:result=prepare_one(store,tenant,account,client_factory=production_factory(entry))
        except Exception:result={"state":"held"}
        results.append({"accountId":account,**result})
    return results
