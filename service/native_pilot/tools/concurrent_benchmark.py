"""Isolated wall-clock PostgreSQL qualification of the existing FASTT path.

Reuses load_test's disposable database, synthetic provider socket and fixture
registry. Service, NativeEngine/ai_chatter, model adapter/budget, ownership,
admission, OFClient, receipt validation and attribution remain real. Only the
verified credential loader, WebSocket source and remote HTTP are substituted.
No clock scaling. No customer data, credentials or external network is needed.
Run under scripts/onlyfans_test_network/sitecustomize.py with a scrubbed env.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import ctypes
import functools
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import platform
import random
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

SERVICE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SERVICE))
from native_pilot.tools import load_test as H


def quantiles(values):
    values = sorted(values)
    return {f'p{p}': round(values[max(0, math.ceil(len(values)*p/100)-1)], 4)
            if values else None for p in (50, 95, 99)}


def rss_mb():
    if os.name != 'nt':
        return H.rss_kb()/1024
    class Counters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('faults', ctypes.c_ulong)] + [
            (n, ctypes.c_size_t) for n in ('peak_rss','rss','peak_paged','paged',
                                          'peak_nonpaged','nonpaged','pagefile','peak_pagefile')]
    c = Counters(); c.cb = ctypes.sizeof(c)
    get_process = ctypes.windll.kernel32.GetCurrentProcess
    get_process.restype = ctypes.c_void_p
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if not get_memory(get_process(), ctypes.byref(c), c.cb):
        raise OSError('process memory measurement failed')
    return c.rss/1048576


class Measurements:
    def __init__(self):
        self.values = defaultdict(list)
        self.lock = threading.Lock()
        self.fan = contextvars.ContextVar('benchmark-fan', default=None)
        self.marks = defaultdict(dict)
        self.events = Counter()
        self.reasons = Counter()
        self.error_samples = []
        self.exceptions = []

    def exception(self, label, exc):
        if len(self.exceptions)<30:
            self.exceptions.append(dict(stage=label,type=type(exc).__name__,
                code=getattr(exc,'pgcode',None),detail=str(exc)[:500],
                fixed_detail=getattr(exc,'detail',None),cause=repr(exc.__cause__)[:300]))

    def record(self, label, elapsed):
        with self.lock:
            self.values[label].append(elapsed)

    def wrap(self, stack, owner, name, label=None):
        original = getattr(owner, name); label = label or name
        if asyncio.iscoroutinefunction(original):
            @functools.wraps(original)
            async def wrapped(*a, **kw):
                t = time.perf_counter()
                if label == 'fan_total':
                    token = self.fan.set((a[0].db, a[1]))
                    self.marks[self.fan.get()].setdefault('picked', t)
                try:
                    return await original(*a, **kw)
                except Exception as exc:
                    self.exception(label,exc);raise
                finally:
                    self.record(label, time.perf_counter()-t)
                    if label == 'fan_total': self.fan.reset(token)
        else:
            @functools.wraps(original)
            def wrapped(*a, **kw):
                t = time.perf_counter()
                try:
                    return original(*a, **kw)
                except Exception as exc:
                    self.exception(label,exc);raise
                finally:
                    self.record(label, time.perf_counter()-t)
        stack.enter_context(patch.object(owner, name, wrapped))

    def emit(self, event):
        self.events[event.get('event')] += 1
        if event.get('reason'): self.reasons[str(event['reason'])] += 1
        if event.get('event') in ('held', 'turn-abandoned', 'turn-released', 'turn-requeued') and len(self.error_samples)<15:
            self.error_samples.append(event)
        key = self.fan.get()
        if key and event.get('event') == 'model-start':
            self.marks[key].setdefault('model_start', time.perf_counter())
        if key and event.get('event') == 'model-complete':
            self.marks[key]['model_end'] = time.perf_counter()

    def report(self):
        return {k: dict(n=len(v), total_s=round(sum(v), 4), **quantiles(v), max_s=round(max(v),4))
                for k,v in sorted(self.values.items())}


async def exercise(args, db):
    import httpx
    import importlib
    engine_module=importlib.import_module("db.engine")
    async_engine=engine_module.engine
    if args.fleet_db_pool:
        from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
        await async_engine.dispose()
        async_engine=create_async_engine(db.async_url,pool_size=args.fleet_db_pool,max_overflow=0,
                                         pool_timeout=10,pool_pre_ping=True)
        engine_module.engine=async_engine
        engine_module.async_session_factory=async_sessionmaker(async_engine,expire_on_commit=False)
    from db.models import AccountAiConfig
    from sqlalchemy.orm import Session
    from native_pilot import service, runner, model, connection, attribution, pacing
    from native_pilot.ledger import Ledger
    from native_pilot.scope import AccountScope
    from native_pilot.tuning import Tuning
    from native_pilot.intake import Intake
    from native_pilot import pacing_schema
    from native_pilot.engine import NativeEngine
    from twin_bridge import store
    from twin_bridge.platform import OnlyFansTextAdapter
    from of_client import OFClient

    m = Measurements()
    cfg = H.Cfg(name=args.name, accounts=args.creators, fans=args.fans, scale=1,
                throttle_s=20, provider_latency_s=args.provider_latency)
    w = H.World(cfg)
    db.seed(w)
    # The policy rate is one shared account bucket, including competing senders.
    db.run("UPDATE of_delivery_policy SET delivery_mode=:mode,provider_requests_per_second=:rate,"
           "provider_burst=:burst,provider_max_concurrent=:max,used_units=0",
           dict(mode=args.mode, rate=args.rate, burst=args.burst, max=args.send))
    settings = {'native_reply_pacing_enabled': args.paced,
                'native_reply_format_enabled': True, 'language': 'en',
                'split_reply_max_parts': 3}
    if args.media:
        from onlyfans_runtime.configuration import SALES_FLAGS
        settings.update({k:True for k in SALES_FLAGS})
    media_dir = tempfile.TemporaryDirectory(prefix='concurrent-media-')
    manifests = {}
    if args.media:
        from db.models import CatalogItem
        with Session(db.engine) as s:
            for i,a in enumerate(w.accts):
                ident=900000+i
                s.add(CatalogItem(id=ident,account_id=a,kind='video',media_ids='[7101,7102]',
                    preview_media_ids='[7101]',price_cents=300,tip_unlock_cents=0,
                    is_free_teaser=False,tags='[]',enabled=False,pricing_mode='fixed',
                    label='Hiking video',description_for_ai='A hiking video with a trail photo preview'))
                p=Path(media_dir.name)/(a+'.json')
                p.write_text(json.dumps(dict(account_id=a,fan_id='*',items=[dict(catalog_id=ident,kind='video',
                    price_cents=300,media=[dict(id=7101,type='photo'),dict(id=7102,type='video')],preview_ids=[7101])])),encoding='utf-8')
                manifests[a]=str(p)
            s.commit()
    with Session(db.engine) as s:
        for a in w.accts:
            c=s.get(AccountAiConfig,a)
            c.ai_chatter_config_json=json.dumps(settings)
            c.persona='Synthetic creator enjoys hiking and coffee.'
        s.commit()
    now=datetime.utcnow()
    db.many("INSERT INTO of_provider_connection(account_id,binding_id,owner_id,revision,status,"
            "sealed_config,expires_at,verified_at,updated_at) "
            "VALUES(:a,:b,:o,1,'verified','synthetic-unused',:e,:n,:n)",
            [dict(a=a,b='binding-'+a,o=int(w.provider[a]),e=now+timedelta(hours=3),n=now) for a in w.accts])
    if args.paced: pacing_schema.upgrade(db.sync_url)
    os.environ['ONLYFANS_FASTT_SCOPE']=','.join(a+':*' for a in w.accts)

    class Provider(H.FakeHTTP):
        fault_fired=False
        def get(self,url,**kw):
            t=time.perf_counter()
            try:
                if '/vault/media/' in url:
                    mid=int(url.rsplit('/',1)[-1])
                    return H.FakeResponse(dict(id=mid,type='photo' if mid==7101 else 'video',canView=True))
                time.sleep(args.read_latency)
                response=super().get(url,**kw)
                for row in response.data.get('list',[]):
                    row.setdefault('media',[]);row.setdefault('mediaCount',0)
                    row.setdefault('price',0);row.setdefault('responseType','message')
                return response
            finally: m.record('provider_history_http',time.perf_counter()-t)

        def request(self,method,url,json=None,**kw):
            t=time.perf_counter(); key=(self.acct,self.fan_of(url))
            wire_calls.append((t,key))
            m.marks[key].setdefault('wire',t)
            try:
                fault=None
                if key[1]==H.FAN_BASE and not self.fault_fired:
                    self.fault_fired=True;fault=args.fault
                if fault in ('429','401'):
                    w.posts.append(dict(acct=self.acct,fan=key[1],t=w.sim_t(),ok=False,status=int(fault)))
                    r=H.FakeResponse(dict(error='synthetic refusal'),status=int(fault),url=url)
                    if fault=='429':r.headers['retry-after']='8'
                    return r
                if fault=='timeout_lost':
                    w.posts.append(dict(acct=self.acct,fan=key[1],t=w.sim_t(),ok=False,status='timeout'))
                    raise TimeoutError('synthetic provider timeout without acceptance')
                r=super().request(method,url,json=json,**kw)
                media=json.get('mediaFiles',[])
                if media:
                    assert args.media and media==[7101,7102] and json['price']==3 and json['previews']==[7101]
                else:assert not json.get('price')
                r.data.update(media=[dict(id=mid,type='photo' if mid==7101 else 'video') for mid in media],
                              mediaCount=len(media),price=json.get('price',0),previews=json.get('previews',[]),
                              lockedText=False,responseType='message')
                m.marks[key].setdefault('accepted',time.perf_counter())
                if fault=='timeout_accepted':raise TimeoutError('synthetic accepted response lost')
                return r
            finally: m.record('provider_post_http',time.perf_counter()-t)

    w.http={a:Provider(w,a) for a in w.accts}
    class SyntheticModel(model.NativePilotModel):
        def __init__(self,**kw):
            async def respond(request):
                t=time.perf_counter()
                lag=args.slow_latency if self.fan_id==H.FAN_BASE and args.slow_latency else args.model_latency
                await asyncio.sleep(lag)
                body=(f'I enjoyed that hike too. The view near the top made it worthwhile. Your favourite trail sounds lovely, {self.fan_id}.'
                      if not args.multi else
                      f'I enjoyed that hike too. The view near the top made it worthwhile, {self.fan_id}.\n\nI usually take water and a light jacket. Your favourite trail sounds lovely.')
                if args.media and (self.fan_id-H.FAN_BASE)%5==0:
                    ident=900000+w.accts.index(self.account_id)
                    body+=f'\n\nThe hiking video is $3 with a trail photo preview.\n>>OFFER {ident}'
                m.record('model_http',time.perf_counter()-t)
                return httpx.Response(200,json={'model':model.MODEL,
                    'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':body}}],
                    'usage':{'prompt_tokens':2500,'completion_tokens':60,'cost':0.0001}})
            kw['transport']=httpx.MockTransport(respond)
            super().__init__(**kw)

    async def load(scope):
        return connection.PilotConnection(scope=scope,tenant_id='synthetic-tenant',revision=1,
            owner_id=int(scope.provider_id),expires_at=now.replace(tzinfo=timezone.utc)+timedelta(hours=3),
            capture={},x_hash='synthetic',proxy_url=None,model_id='openrouter/deepseek-v4.1-flash',
            model_api_key='synthetic-unused',daily_cost_cap_cents=1000000)

    def client(conn):
        a=conn.scope.db_id; c=OFClient.__new__(OFClient)
        c.account_id=c.user_id=conn.scope.provider_id; c.platform='onlyfans'
        c.http=w.http[a];c.timeout_s=5;c.proxy_url=c.proxy_label=None
        c._signed_headers=lambda url:{}
        c.me=lambda:dict(id=int(conn.scope.provider_id),username='synthetic-creator')
        return c

    class InjectedIntake(Intake):
        # No WebSocket network. handle(), dedupe and transcoder stay unchanged.
        def start(self,client): pass

    services=[]; tasks=[]; samples=[]; lag=[]; monitor_stop=asyncio.Event()
    takeover_at={}; contenders=[]; wire_calls=[]; lock_samples=[]
    main_thread=threading.get_ident()
    def lock_watch():
        # An async sampler cannot observe a loop blocked inside synchronous SQL.
        # This read-only connection remains independent of the application pool.
        while not monitor_stop.is_set():
            try:
                from sqlalchemy import text
                with db.mon.connect() as c:
                    rows=c.execute(text("SELECT pid,state,wait_event_type,wait_event,pg_blocking_pids(pid) AS blockers,"
                        "left(query,500) AS query FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()" )).mappings().all()
                if any(r['wait_event_type']=='Lock' for r in rows) and len(lock_samples)<20:
                    lock_samples.append(dict(activity=[dict(r) for r in rows],
                        loop_stack=traceback.format_stack(sys._current_frames()[main_thread])[-8:]))
            except Exception as exc:m.exception('lock_watch',exc)
            time.sleep(.2)
    async def take_over():
        from onlyfans_runtime.store import Store
        await asyncio.sleep(args.takeover_after)
        controls=Store(db.sync_url)
        try:
            for a in w.accts:
                await asyncio.to_thread(controls.control,'synthetic-tenant',a,H.FAN_BASE,
                    mode='human_controlled',expected_revision=0,command_key='qualification-takeover-'+a,
                    actor='qualification-operator',reason='Synthetic manual takeover during generation',action='request_takeover')
                takeover_at[(a,H.FAN_BASE)]=time.perf_counter()
        finally:controls.close()
    async def monitor():
        while not monitor_stop.is_set():
            t=time.perf_counter()
            await asyncio.sleep(.05)
            lag.append(max(0,time.perf_counter()-t-.05))
            if not samples or t-samples[-1]['t']>.5:
                rows=await asyncio.to_thread(db.run,
                    "SELECT count(*),count(*) FILTER(WHERE state='active'),"
                    "count(*) FILTER(WHERE wait_event_type='Lock') FROM pg_stat_activity WHERE datname=current_database()")
                samples.append(dict(t=t,rss_mb=rss_mb(),threads=threading.active_count(),
                                    connections=rows[0][0],active=rows[0][1],lock_waiters=rows[0][2]))
                if rows[0][2] and len(lock_samples)<20:
                    lock_samples.extend(await asyncio.to_thread(db.run,
                        "SELECT wait_event,query FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'"))

    with ExitStack() as stack:
        # Production uses one process per creator. This fleet simulation puts
        # those services in one process, so give their blocking send bridges the
        # aggregate bounded executor they would otherwise have separately.
        # Account admission/rates and per-fan ownership are unchanged.
        fleet_threads=3*min(args.creators*args.prepare,args.creators*args.fans)+12
        if args.creators>1:
            from concurrent.futures import ThreadPoolExecutor
            asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(
                max_workers=fleet_threads,thread_name_prefix='qualification-fleet'))
            stack.enter_context(patch.object(service.Service,'_size_thread_pool',lambda self:None))
        from sqlalchemy import event
        def sql_start(conn,cursor,statement,parameters,context,many):
            context.qualification_start=time.perf_counter()
        def sql_end(conn,cursor,statement,parameters,context,many):
            m.record('async_database_statement',time.perf_counter()-context.qualification_start)
        event.listen(async_engine.sync_engine,'before_cursor_execute',sql_start)
        event.listen(async_engine.sync_engine,'after_cursor_execute',sql_end)
        stack.callback(event.remove,async_engine.sync_engine,'before_cursor_execute',sql_start)
        stack.callback(event.remove,async_engine.sync_engine,'after_cursor_execute',sql_end)
        stack.enter_context(patch.object(runner,'emit',m.emit))
        stack.enter_context(patch.object(connection,'load_verified_connection',load))
        stack.enter_context(patch.object(connection,'build_client',client))
        stack.enter_context(patch.object(model,'NativePilotModel',SyntheticModel))
        for name in ('snapshot','claim_input','begin_attempt','record_sent','finish_input','activate_service',
                     'allowance_status','validate_dispatch','save_dispatch_proof'):
            if hasattr(Ledger,name): m.wrap(stack,Ledger,name,'ledger.'+name)
        for name in ('pending_fans','burst_state','held_reasons','_twin_controls','already_claimed'):
            m.wrap(stack,service.Service,name,'service.'+name)
        m.wrap(stack,service.Service,'_run_fan','fan_total')
        m.wrap(stack,service.ServiceGateway,'pace','delivery_budget_wait')
        m.wrap(stack,OnlyFansTextAdapter,'preflight_history','provider_preflight')
        m.wrap(stack,runner,'read_sources','context_sources')
        m.wrap(stack,pacing,'prepare','reply_schedule')
        m.wrap(stack,store.PgConnection,'execute','database_statement')
        m.wrap(stack,NativeEngine,'load_config','engine_config')
        m.wrap(stack,NativeEngine,'_no_obligations','engine_obligations')
        for a in w.accts:
            ns=argparse.Namespace(account=a,fan=None,all_fans=True,handle='synthetic-creator',
                enable_send=True,twin_context=False,content_manifest=manifests.get(a),vault_mode=False,
                service_id=None,followups=False)
            tuning=Tuning.from_env(env={},concurrency=args.prepare,send_concurrency=args.send)
            svc=service.Service(ns,intake_factory=InjectedIntake,tuning=tuning,quiet_s=0 if not args.paced else 7,
                                poll_s=args.poll,hold_s=1)
            services.append(svc);tasks.append(asyncio.create_task(svc.run()))
        try:
            deadline=time.perf_counter()+60
            while any(s.intake is None for s in services):
                if any(t.done() for t in tasks) or time.perf_counter()>deadline:
                    raise RuntimeError('service startup failed: '+str([repr(t.exception()) for t in tasks if t.done()]))
                await asyncio.sleep(.01)
            if args.competing_runner:
                original=services[0]
                contender=service.Service(original.args,intake_factory=InjectedIntake,tuning=original.tuning,
                    quiet_s=original.quiet_s,poll_s=args.poll,hold_s=1)
                contenders.append(contender)
                tasks.append(asyncio.create_task(contender.run()))
            if args.held:
                db.run("UPDATE of_conversation_state SET mode='paused' WHERE fan_id=:f",dict(f=H.FAN_BASE+args.fans-1))
            t0=time.perf_counter();cpu0=time.process_time()
            helper=asyncio.create_task(monitor())
            watcher=threading.Thread(target=lock_watch,name='qualification-lock-watch',daemon=True);watcher.start()
            # All inputs have the same earliest arrival. Persist as one transaction
            # for a strict simultaneous burst; a separate intake sample records the
            # real transcoder cost. No per-fan model, ownership or send bypass.
            rows=[]
            for a in w.accts:
                for f in w.fans:
                    r=w.new_inbound(a,f,'I liked the hiking trail. How was your walk?')
                    r['created']=w.ms(w.now())
                    m.marks[(a,f)]['incoming']=t0
                    rows.append(dict(a=a,f=f,m=r['id'],d='in',b=r['text'],c=r['created'],i=r['created']))
            # One actual Intake/transcoder and duplicate sample, alongside the
            # simultaneous persisted burst. This cost stays inside the clock.
            first=rows.pop(0); intake_t=time.perf_counter()
            frame=dict(api2_chat_message=dict(id=first['m'],text=first['b'],createdAt=first['c'].isoformat()+'Z',
                price=0,fromUser=dict(id=first['f'],username='synthetic-fan',name='Synthetic Fan')))
            assert await services[0].intake.handle(frame)=='persisted'
            assert await services[0].intake.handle(frame)=='duplicate'
            m.record('intake_transcoder_and_dedupe_sample',time.perf_counter()-intake_t)
            await asyncio.to_thread(db.many,db.MSG.format(extra='',vals=''),rows)
            m.record('burst_persistence',time.perf_counter()-t0)
            takeover_task=asyncio.create_task(take_over()) if args.takeover_after is not None else None
            expected=args.creators*(args.fans-int(args.held)-int(args.takeover_after is not None))
            while time.perf_counter()-t0<args.duration:
                accepted=len([k for k,v in m.marks.items() if 'accepted'in v])
                if accepted>=expected and not any(s.tasks for s in services) and (takeover_task is None or takeover_task.done()):
                    proven=await asyncio.to_thread(db.run,"SELECT count(DISTINCT (p.account_id,p.fan_id)) "
                        "FROM native_pilot_receipts r JOIN native_pilots p ON p.pilot_id=r.pilot_id WHERE r.state='sent'")
                    if proven[0][0]>=expected:break
                await asyncio.sleep(.05)
            elapsed=time.perf_counter()-t0; cpu=time.process_time()-cpu0
            monitor_stop.set();await helper
            await asyncio.to_thread(watcher.join,2)
            if takeover_task:await takeover_task
            # Take the snapshot before graceful draining. In-flight/unfinished
            # eligible turns remain in the denominator and are right-censored.
            receipts=db.run("SELECT a.account_id,a.fan_id,r.state,r.created_at,r.updated_at "
                "FROM native_pilot_receipts r JOIN twin_bridge_send_attempts a ON a.id=r.attempt_id")
            for a,f,state,admitted,settled in receipts:
                if state=='sent':m.marks[(a,f)].setdefault('receipt',settled)
            eligible=[(a,f) for a in w.accts for f in w.fans
                      if not(args.held and f==w.fans[-1]) and (a,f)not in takeover_at]
            complete=[k for k in eligible if 'receipt'in m.marks[k]]
            inputs=db.run("SELECT p.account_id,p.fan_id,i.state,i.updated_at FROM native_pilot_inputs i JOIN native_pilots p ON p.pilot_id=i.pilot_id")
            full_complete={ (a,f) for a,f,state,updated in inputs if state=='complete' } & set(eligible)
            full_lats=[w.t0+(datetime.fromisoformat(str(updated))-w.sim0).total_seconds()-t0
                       for a,f,state,updated in inputs if state=='complete' and (a,f) in eligible]
            lat=[m.marks[k]['accepted']-t0 for k in complete]
            censored=[elapsed for k in eligible if k not in complete]
            for k in eligible:
                z=m.marks[k]
                if 'picked'in z:m.record('pickup_and_slot_wait',z['picked']-t0)
                if 'model_start'in z:m.record('pre_model_context_and_ownership',z['model_start']-z['picked'])
                if 'model_end'in z and 'wire'in z:m.record('model_end_to_wire',z['wire']-z['model_end'])
                if 'wire'in z and 'accepted'in z:m.record('wire_to_acceptance',z['accepted']-z['wire'])
            result=dict(name=args.name,config=vars(args),clock='real wall clock; no time scaling',
                transports='synthetic model HTTP and synthetic provider HTTP; real NativeEngine and PostgreSQL',
                fleet_simulation=dict(service_processes=1,creator_services=args.creators,
                                      executor_bound=fleet_threads if args.creators>1 else 3*args.prepare+12),
                eligible=len(eligible),completed=len(complete),outstanding=len(censored),
                full_reply_completed=len(full_complete),full_reply_outstanding=len(eligible)-len(full_complete),
                full_reply_latency_s=quantiles(full_lats),full_reply_within_20=sum(x<=20 for x in full_lats),
                operator_owned_excluded=int(args.held)*args.creators+len(takeover_at),
                completion_pct=round(100*len(complete)/len(eligible),3),
                within_20=sum(x<=20 for x in lat),within_180=sum(x<=180 for x in lat),
                share_within_180=round(sum(x<=180 for x in lat)/len(eligible),4),
                latency_s=quantiles(lat),right_censored_age_s=quantiles(censored),
                completion_or_censor_s=quantiles(lat+censored),elapsed_s=round(elapsed,3),
                throughput_turns_s=round(len(complete)/elapsed,4),posts=len(w.posts),
                receipt_states=dict(Counter(r[2] for r in receipts)),
                cpu_seconds=round(cpu,3),cpu_one_core_pct=round(100*cpu/elapsed,2),
                rss_peak_mb=round(max([s['rss_mb'] for s in samples],default=rss_mb()),2),
                threads_peak=max([s['threads'] for s in samples],default=0),
                db_connections_peak=max([s['connections'] for s in samples],default=0),
                db_active_peak=max([s['active'] for s in samples],default=0),
                db_lock_waiters_peak=max([s['lock_waiters'] for s in samples],default=0),
                lock_samples=lock_samples,
                event_loop_lag_s=quantiles(lag),stages=m.report(),events=dict(m.events),
                reasons=dict(m.reasons),error_samples=m.error_samples,
                exceptions=m.exceptions,input_states=dict(Counter(r[2] for r in inputs)),
                correctness=dict(wrong_recipient_posts=sum(p['fan']not in w.fans for p in w.posts),
                    duplicate_receipts=len(receipts)-len({(a,f,str(c)) for a,f,st,c,u in receipts}),
                    held_posts=sum(p['fan']==w.fans[-1] for p in w.posts) if args.held else 0,
                    contender_activated=any(c.started_at is not None for c in contenders),
                    takeover_posts_after=sum(t>takeover_at[k] for t,k in wire_calls if k in takeover_at),
                    duplicate_sends=sum(len(msgs)-len({(r['text'],r.get('price',0),tuple(r.get('previews',[]))) for r in msgs})
                                        for msgs in w.sent.values())))
            return result
        finally:
            monitor_stop.set()
            for s in services+contenders:s.stop.set()
            await asyncio.gather(*tasks,return_exceptions=True)
            await async_engine.dispose()
            media_dir.cleanup()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name',default='one-creator-100');p.add_argument('--creators',type=int,default=1)
    p.add_argument('--fans',type=int,default=100);p.add_argument('--mode',choices=['gap','concurrent'],default='concurrent')
    p.add_argument('--prepare',type=int,default=4);p.add_argument('--send',type=int,default=4)
    p.add_argument('--rate',type=float,default=1);p.add_argument('--burst',type=int,default=5)
    p.add_argument('--model-latency',type=float,default=.2);p.add_argument('--provider-latency',type=float,default=.12)
    p.add_argument('--read-latency',type=float,default=.02);p.add_argument('--poll',type=float,default=2)
    p.add_argument('--duration',type=float,default=190);p.add_argument('--paced',action='store_true')
    p.add_argument('--fleet-db-pool',type=int,default=0)
    p.add_argument('--multi',action='store_true');p.add_argument('--held',action='store_true')
    p.add_argument('--media',action='store_true')
    p.add_argument('--fault',choices=['429','401','timeout_lost','timeout_accepted'])
    p.add_argument('--takeover-after',type=float)
    p.add_argument('--competing-runner',action='store_true')
    p.add_argument('--slow-latency',type=float,default=0);p.add_argument('--out',required=True)
    args=p.parse_args()
    if os.environ.get('LOAD_TEST_REUSE_DB'):raise RuntimeError('fresh disposable DB required')
    os.environ.update(PYTHON_DOTENV_DISABLED='1',TWIN_FASTT_CONFIG_ISOLATED='1',
                      TWIN_BRIDGE_ENABLED='0',TWIN_BRIDGE_SEND_ENABLED='0',ONLYFANS_WORKER_TENANT='synthetic-tenant')
    if args.fleet_db_pool:
        if args.creators<2 or not 4<=args.fleet_db_pool<=128:
            raise RuntimeError('fleet DB pool must be bounded 4..128 for a multi-creator simulation')
        # One process aggregates creator services; size only its shared test pool.
    db=H.Db(os.environ['ONLYFANS_TEST_PG_URL']);os.environ['DATABASE_URL']=db.async_url
    logging.disable(logging.CRITICAL)
    db.create()
    try:
        db.bootstrap();result=asyncio.run(exercise(args,db))
        result['environment']=dict(python=sys.version,os=platform.platform(),host_cpus=os.cpu_count(),
            postgres=db.run('SELECT version()')[0][0],
            source_sha256={str(x.relative_to(SERVICE.parent)):hashlib.sha256(x.read_bytes()).hexdigest()
                           for folder in ('native_pilot','onlyfans_runtime','twin_bridge','automations')
                           for x in (SERVICE/folder).rglob('*.py')})
        Path(args.out).write_text(json.dumps(result,indent=2,default=str))
        print(json.dumps({k:result[k] for k in ('name','eligible','completed','outstanding','latency_s','elapsed_s','reasons')}),flush=True)
    finally:db.drop()


if __name__=='__main__':main()
