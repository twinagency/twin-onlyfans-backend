"""Bounded worker over the existing canonical owners.

No environment credential resolution or automatic activation exists here.
Explicit account-bound adapters are supplied by deployment. Intake records through Store; this
loop drains durable intake and coordinates native preparation, one-use
delivery and receipt repair. Injected seams must be explicitly registered types.
"""
from dataclasses import asdict,dataclass
import math
import time
import os
import threading

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from db.onlyfans_delivery_models import OfDeliveryPolicy,OfNativePlan,OfTextIntent
from onlyfans_read.contract import ID_PATTERN
from .store import Refused,Store
from .text_delivery import SyntheticTextTransport,TextAuthority,MAX_SYNC_AGE_SECONDS


@dataclass(frozen=True)
class WorkerAccount:
    account_id:str
    model:object
    transport:object
    reader:object=None
    # A revision selects import-only execution, never planning or delivery.
    read_connection_revision:int|None=None


@dataclass
class CycleReport:
    ingress:int=0
    claims:int=0
    queued:int=0
    repaired:int=0
    expired:int=0
    accepted:int=0
    not_accepted:int=0
    unknown:int=0
    held:int=0
    errors:int=0

    def public(self):
        return {"schemaVersion":"of-worker.v1",**asdict(self)}


class NativeWorker:
    """One serial lane per instance; separate processes compete through PG."""
    def __init__(self,store,tenant_id,accounts,*,accounts_per_cycle=4,model_timeout_seconds=10,facts_reader=None,
                 history_recovery=False,history_memory=False,engagement=False):
        if os.environ.get("ONLYFANS_PROCESS_ROLE")!="worker" or type(store) is not Store:
            raise Refused("native worker requires an explicit worker Store")
        from .native_planning import NativeTextPlanner,SyntheticNativeModel
        from .loopback_transport import LoopbackTextTransport
        from .native_connector import NativeLoopbackConnector
        from .provider_connector import NativeProviderConnector
        from .provider_model import NativeProviderModel
        if type(tenant_id) is not str or tenant_id=="deployment" or not ID_PATTERN.fullmatch(tenant_id):
            raise Refused("an explicit worker tenant is required")
        if (type(accounts) is not tuple or not 1<=len(accounts)<=32
                or any(type(item) is not WorkerAccount for item in accounts)):
            raise Refused("bounded explicit worker accounts are required")
        for item in accounts:
            if item.read_connection_revision is not None and (
                    type(item.read_connection_revision) is not int or item.read_connection_revision < 1
                    or type(item.transport) is not NativeProviderConnector or item.model is not None):
                raise Refused("read-only workers require a native connection revision and no model")
            if (type(item.account_id) is not str or not ID_PATTERN.fullmatch(item.account_id)
                    or (item.model is not None and type(item.model) not in {SyntheticNativeModel, NativeProviderModel})
                    or type(item.transport) not in {SyntheticTextTransport,LoopbackTextTransport,NativeLoopbackConnector,NativeProviderConnector}
                    or item.transport.account_id!=item.account_id):
                raise Refused("worker seams must be explicitly registered and account-bound")
            if item.model is None and type(item.transport) is not NativeProviderConnector:
                raise Refused("only native provider workers may omit the model credential")
            if item.model is not None and (type(item.transport) is NativeProviderConnector) != (type(item.model) is NativeProviderModel):
                raise Refused("native provider model and transport must be selected together")
            if type(item.model) is NativeProviderModel and item.model.account_id!=item.account_id:
                raise Refused("native provider model account mismatch")
            if item.reader is not None and (type(item.reader) not in {NativeLoopbackConnector,NativeProviderConnector} or item.reader is not item.transport):
                raise Refused("native intake and delivery must use the same bound connector")
            if type(item.transport) is NativeProviderConnector and item.reader is not item.transport:
                raise Refused("native provider workers require their account-bound history reader")
        models=[item.model for item in accounts if item.model is not None]
        if len({item.account_id for item in accounts})!=len(accounts) or len({id(item) for item in models})!=len(models):
            raise Refused("worker accounts and model credentials must be isolated")
        if (type(accounts_per_cycle) is not int or not 1<=accounts_per_cycle<=4
                or type(model_timeout_seconds) not in {int,float} or not math.isfinite(model_timeout_seconds)
                or not .01<=model_timeout_seconds<=15):raise Refused("invalid worker resource bound")
        self.store,self.tenant_id,self.accounts=store,tenant_id,accounts
        self.planner,self.authority=NativeTextPlanner(store),TextAuthority(store)
        from .campaigns import CampaignService
        self.campaigns=CampaignService(store)
        self.accounts_per_cycle,self.model_timeout=accounts_per_cycle,model_timeout_seconds
        self._cursor=0
        self._lock=threading.Lock()
        from .creator_facts import TwinFactsReader
        if facts_reader is not None and type(facts_reader) is not TwinFactsReader:
            raise Refused("explicit shared creator source required")
        self.facts_reader=facts_reader
        # Launch features: each is off unless the deployment sets its worker flag (and, for engagement, saved settings).
        for flag in (history_recovery,history_memory,engagement):
            if type(flag) is not bool:raise Refused("invalid worker feature flag")
        self.history_recovery,self.history_memory=history_recovery,history_memory
        self.engagement_service=None
        if engagement:
            from .launch_engagement.service import EngagementService
            self.engagement_service=EngagementService(store)

    @staticmethod
    def _role():
        if os.environ.get("ONLYFANS_PROCESS_ROLE")!="worker":raise Refused("native worker role is no longer active")

    def _can_plan(self,account):
        return account.model is not None and self._can_dispatch_campaign(account)

    def _can_dispatch_campaign(self,account):
        self._role()
        from .provider_connector import NativeProviderConnector
        kind="native" if type(account.transport) is NativeProviderConnector else "synthetic"
        with Session(self.store.engine) as s,s.begin():
            runtime=self.store._runtime(s,self.tenant_id,account.account_id)
            policy=s.get(OfDeliveryPolicy,account.account_id)
            now=self.authority._db_now(s)
            from .store import fan_current_override
            from . import concurrent_admission
            from native_pilot import admission
            current=bool(not runtime.recovery_needed and runtime.last_sync_at is not None and
                (now-runtime.last_sync_at).total_seconds()>=-300 and
                (now-runtime.last_sync_at).total_seconds()<=MAX_SYNC_AGE_SECONDS)
            concurrent = policy is not None and concurrent_admission.mode(policy) == "concurrent"
            cooldown = admission.cooldown_at(policy) if policy is not None else None
            # Per-fan eligibility still requires that fan's own recovery (text_delivery).
            return bool(runtime.enabled and ((current and not runtime.pending_events)
                or bool(fan_current_override(account.account_id)))
                and policy is not None and policy.enabled and policy.transport_kind==kind
                and policy.used_units<policy.limit_units
                and (concurrent or (not policy.active_intent_id and policy.queued_count==0
                                    and (policy.next_dispatch_at is None or policy.next_dispatch_at<=now)))
                and (not concurrent or cooldown is None or cooldown<=now)
                and (not hasattr(account.transport,"policy_revision") or account.transport.policy_revision==policy.revision))

    def _heartbeat(self, account, *, failed=False):
        """Record actual bounded-cycle execution without claiming provider coverage."""
        self._role()
        from .launch_operations_models import OfLaunchWorkerHealth
        from .native_context import canonical
        from .provider_connector import NativeProviderConnector
        from .store import utcnow
        with Session(self.store.engine) as s, s.begin():
            runtime=self.store._runtime(s,self.tenant_id,account.account_id)
            generation=0
            if type(account.transport) is NativeProviderConnector:
                generation=getattr(account.reader,"connection_revision",None)
                if account.read_connection_revision is not None:
                    if generation is not None and generation!=account.read_connection_revision:
                        raise Refused("worker connection generation changed")
                    generation=account.read_connection_revision
                if type(generation) is not int or generation<1:
                    raise Refused("worker connection generation unavailable")
                # A stale cohort cannot attest to the replacement session or
                # creator binding just because a refused turn reached finally.
                self.store._read_connection(s,runtime,generation)
            row=s.get(OfLaunchWorkerHealth,account.account_id)
            if row is None:
                row=OfLaunchWorkerHealth(account_id=account.account_id)
                s.add(row)
            row.binding_id=runtime.binding_id
            row.observed_at=utcnow()
            row.capabilities_json=canonical({"history_recovery":self.history_recovery,
                "delivery_resolution":__import__('onlyfans_runtime.delivery_resolution',fromlist=['operational']).operational(),
                "history_memory":self.history_memory,"engagement":self.engagement_service is not None,
                "connection_revision":generation})
            row.last_error="cycle_failed" if failed else None

    def _deliver(self,account,report):
        self._role()
        with Session(self.store.engine) as s,s.begin():
            self.store._runtime(s,self.tenant_id,account.account_id)
            from .fastt_manual import fastt_owned
            # A fan the FASTT runner owns is never sent by this worker's connector.
            identity=next((i for i,f in s.execute(select(OfTextIntent.intent_id,OfTextIntent.fan_id).where(
                OfTextIntent.account_id==account.account_id,OfTextIntent.status=="queued")
                .order_by(OfTextIntent.created_at,OfTextIntent.intent_id).limit(50)).all()
                if not fastt_owned(account.account_id,f)),None)
        if identity is None:return
        result=self.authority.dispatch(self.tenant_id,account.account_id,identity,account.transport)
        if result in {"accepted","not_accepted","unknown"}:setattr(report,result,getattr(report,result)+1)
        elif result!="queued":report.held+=1

    def _queue_due(self,account):
        """Operator campaigns first, then proactive engagement; both only when no reactive claim exists."""
        identity=self.campaigns.queue_due(self.tenant_id,account.account_id)
        if identity is None and self.engagement_service is not None:
            from . import engagement_worker
            engagement_worker.schedule_due(self.store,self.authority,self.engagement_service,self.tenant_id,account.account_id)
            identity=engagement_worker.queue_due(self.store,self.authority,self.engagement_service,self.tenant_id,account.account_id)
        return identity

    def _history_turn(self,account,report,*,revision=None,memory=True,archive=True):
        """Archive recovery and memory processing never abort the cycle: a failure only holds this turn."""
        scope={"connection_revision":revision} if revision is not None else {}
        if memory and self.history_memory:
            try:
                from .launch_history_memory.memory import HistoricalMemory
                HistoricalMemory(self.store).step(self.tenant_id,account.account_id,limit=40)
            except (Refused,SQLAlchemyError):report.held+=1
        if archive and self.history_recovery and account.reader is not None:
            try:
                from .launch_history_memory.recovery import HistoryRecovery
                recovery=HistoryRecovery(self.store)
                recovery.request_account(self.tenant_id,account.account_id,**scope)
                recovery.step(self.tenant_id,account.account_id,account.reader,**scope)
            except Exception:report.held+=1

    def _read_progress(self, account, **scope):
        """A paced provider read retry leaves durable recovery checkpoints due."""
        from .worker import read_step
        from .provider_connector import NativeReadTransientError, NativeRateLimitError
        try:
            return read_step(self.store,self.tenant_id,account.account_id,account.reader,
                recent_cutover=self.history_recovery,**scope)
        except (NativeReadTransientError,NativeRateLimitError):
            # The read owner already released its lease and saved provider
            # backoff. Skip planning this turn; other accounts still get work.
            return None

    def step(self,stop=None):
        """Bounded work, no waits; stop is cooperative between durable phases."""
        self._role()
        stop=stop if stop is not None else threading.Event()
        if not self._lock.acquire(blocking=False):raise Refused("worker instance is already running")
        report=CycleReport()
        try:
            from .delivery_resolution_jobs import poll
            poll(self.store,self.tenant_id)
            count=min(self.accounts_per_cycle,len(self.accounts))
            selected=[self.accounts[(self._cursor+i)%len(self.accounts)] for i in range(count)]
            self._cursor=(self._cursor+count)%len(self.accounts)
            for account in selected:
                if stop.is_set():break
                account_errors=report.errors
                try:
                    # Fresh arrivals have their independent inbox lane. Give
                    # archives a turn before legacy discovery for every account,
                    # even when that discovery is deferred or raises an error.
                    if not stop.is_set():self._history_turn(account,report,
                        revision=account.read_connection_revision,memory=False)
                    if account.read_connection_revision is not None:
                        progress=self._read_progress(account,connection_revision=account.read_connection_revision)
                        if progress is None:
                            report.held+=1
                            continue
                        report.ingress+=progress.applied
                        if not stop.is_set():self._history_turn(account,report,
                            revision=account.read_connection_revision,archive=False)
                        # Inactive verified accounts never enter planning or sending.
                        continue
                    # Historical acceptance is repaired even after a binding is
                    # revoked; this path grants no new planning or send rights.
                    report.expired+=self.authority.expire_dispatches(self.tenant_id,account.account_id,limit=4)
                    report.repaired+=self.authority.repair(self.tenant_id,account.account_id,limit=4)
                    if self.facts_reader is not None:
                        from .creator_facts import refresh
                        try:refresh(self.store,self.tenant_id,account.account_id,self.facts_reader)
                        except (Refused,ValueError,TypeError):report.held+=1
                    if stop.is_set():break
                    if account.reader is None:
                        report.ingress+=self.store.drain(self.tenant_id,account.account_id)
                    else:
                        progress=self._read_progress(account)
                        if progress is None:
                            report.held+=1
                            continue
                        report.ingress+=progress.applied
                    if stop.is_set():break
                    # After recent intake and before a new reply claim; yields to any claimed live work.
                    if self.history_memory:
                        self._history_turn(account,report,archive=False)
                        if stop.is_set():break
                    queued,retired=self.planner.recover(self.tenant_id,account.account_id,limit=4)
                    report.queued+=queued;report.held+=retired
                    if stop.is_set():break
                    self._deliver(account,report)
                    if stop.is_set():break
                    # One sender: an account the FASTT runner owns wholesale gets no
                    # Twin claim, plan, campaign or engagement from this worker.
                    from .fastt_manual import fastt_account_owned
                    if fastt_account_owned(account.account_id):continue
                    if not self._can_dispatch_campaign(account):continue
                    if account.model is None:
                        identity=self._queue_due(account)
                        if identity is not None:report.queued+=1
                        if not stop.is_set():self._deliver(account,report)
                        continue
                    from .creator_facts import effective
                    with Session(self.store.engine) as s,s.begin():
                        self.store._runtime(s,self.tenant_id,account.account_id)
                        from db.models import AccountAiConfig
                        config=s.get(AccountAiConfig,account.account_id)
                        source=effective(s,config)[1] if config is not None else None
                    if source is not None and source["state"]!="current":
                        report.held+=1
                        # Operator-authored campaigns do not depend on AI facts.
                        identity=self._queue_due(account)
                        if identity is not None:report.queued+=1
                        if not stop.is_set():self._deliver(account,report)
                        continue
                    claim=self.store.claim(self.tenant_id,account_ids=(account.account_id,))
                    if claim is None:
                        # Reactive inputs keep priority. A due operator-authored
                        # campaign uses the same authority and makes no model call.
                        identity=self._queue_due(account)
                        if identity is not None:report.queued+=1
                        if not stop.is_set():self._deliver(account,report)
                        continue
                    report.claims+=1
                    # Once a claim exists, finish a started model's durable cost
                    # evidence, but begin no new model phase after stop is seen.
                    if stop.is_set():break
                    try:
                        identity=self.planner.prepare(self.tenant_id,claim,account.model,timeout_seconds=self.model_timeout)
                        if stop.is_set():break
                        self._role()
                        with Session(self.store.engine) as s:
                            self.store._runtime(s,self.tenant_id,account.account_id)
                            status=s.get(OfNativePlan,(account.account_id,identity)).status
                        if status in {"ready","queued"}:
                            self.planner.queue(self.tenant_id,account.account_id,identity);report.queued+=1
                        else:
                            self.store.hold_claim(self.tenant_id,claim);report.held+=1
                    except Refused:
                        self.store.hold_claim(self.tenant_id,claim);report.held+=1
                    if stop.is_set():break
                    self._deliver(account,report)
                except Refused:report.held+=1
                except SQLAlchemyError:
                    report.errors+=1
                    # Stop this cycle on DB loss. A later cycle reopens normal
                    # pooled connections; no speculative model/send replay.
                    break
                except Exception:
                    # Keep private bodies/SQL/credential-bearing exceptions out
                    # of public health. Durable leases/markers handle recovery.
                    report.errors+=1
                finally:
                    try:self._heartbeat(account,failed=report.errors>account_errors)
                    except (Refused,SQLAlchemyError):pass
            return report
        finally:self._lock.release()

    def run(self,stop,*,max_cycles=None,poll_seconds=.5,max_backoff_seconds=5,observe=None,max_seconds=None,minimum_cycles=1):
        """Cooperative drain with interruptible, bounded failure backoff.

        A hard process kill may interrupt a phase; persisted invocation and wire
        markers then remain uncertain, with no repeat. This is not a supervisor
        or a hard wall-clock shutdown guarantee over database operations.
        """
        if (max_cycles is not None and (type(max_cycles) is not int or not 1<=max_cycles<=1000000)):
            raise Refused("invalid worker cycle limit")
        for value in (poll_seconds,max_backoff_seconds):
            if type(value) not in {int,float} or not math.isfinite(value):raise Refused("invalid worker polling bound")
        if not .05<=poll_seconds<=5 or not poll_seconds<=max_backoff_seconds<=30:raise Refused("invalid worker polling bound")
        if (max_seconds is not None and (type(max_seconds) not in {int,float}
                or not math.isfinite(max_seconds) or not .05<=max_seconds<=3600)):
            raise Refused("invalid worker time limit")
        if type(minimum_cycles) is not int or minimum_cycles<1 or (max_cycles is not None and minimum_cycles>max_cycles):
            raise Refused("invalid minimum worker cycles")
        deadline=time.monotonic()+max_seconds if max_seconds is not None else None
        cycles=failures=0
        while not stop.is_set() and (max_cycles is None or cycles<max_cycles):
            report=self.step(stop);cycles+=1
            failures=min(failures+1,6) if report.errors else 0
            if observe is not None:observe(report.public())
            if max_cycles is not None and cycles>=max_cycles:break
            # Slow HTTP turns must yield to credential refresh and import jobs.
            # Finish one pass over the selected cohort before yielding.
            if deadline is not None and cycles>=minimum_cycles and time.monotonic()>=deadline:break
            stop.wait(min(max_backoff_seconds,poll_seconds*2**failures))
        return cycles
