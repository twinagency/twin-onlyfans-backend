"""Conversation isolation and scheduling regressions found during qualification."""
import argparse
import asyncio
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from native_pilot.content import VaultPlan
from native_pilot.engine import NativeEngine
from native_pilot.ledger import Refused
from native_pilot.send_gate import SendGate
from native_pilot.service import Service, LIVE_TURNS_PER_SLOT, LIVE_TURNS_CAP_PER_SLOT
from native_pilot.runner import Gateway


def parent():
    return Service(argparse.Namespace(account='of-900000001', fan=None, all_fans=True), poll_s=.01)


def test_a_second_fan_cannot_clear_the_first_fans_quote_or_commercial_hold():
    svc=parent();svc.content=VaultPlan(svc.db,'*')
    a,b=svc.child(1001),svc.child(1002)
    NativeEngine(svc.db,1001,chat=None,send=None,content=a.content)
    quote=SimpleNamespace(price_cents=300,assets=((71,'v1'),))
    a.content.bind_quote(quote);a.content.commercial_text=True
    a.content.accepted_inventory_revision='first-fan-inventory'
    NativeEngine(svc.db,1002,chat=None,send=None,content=b.content)
    assert a.content.canonical_quote is quote
    assert a.content.commercial_text is True
    assert a.content.accepted_inventory_revision=='first-fan-inventory'
    assert b.content.canonical_quote is None
    assert b.content.fan==1002 and a.content.fan==1001


def test_a_scheduled_future_reply_yields_its_slot_until_it_is_due():
    async def run():
        svc=parent();child=svc.child(1001)
        now=datetime.utcnow();due=now+timedelta(seconds=60)
        child.pilot=SimpleNamespace(pilot_id='synthetic',owner_token='owner')
        snap=SimpleNamespace(message_id=1)
        child.ledger=SimpleNamespace(snapshot=lambda **kw:snap,input_claimed=lambda **kw:False)
        child.path='unused'
        async def serve():await child.turn()
        child.serve_fan=serve
        with patch('native_pilot.pacing.settings',return_value={'pacing':True}), \
             patch('native_pilot.pacing.prepare',return_value={'schedule':{'synthetic':True},'eligible_at':due}), \
             patch('native_pilot.runner.now',return_value=now):
            await svc._run_fan(1001)
        assert svc.fan_holds[1001]==(due,'reply-target-wait')
        assert child.turns_started==0
    asyncio.run(run())


def test_send_gate_cannot_queue_two_owners_for_the_same_conversation():
    async def run():
        gate=SendGate(poll_s=.001)
        await gate.acquire(lambda:None,1,key='blocker')
        waiting=asyncio.create_task(gate.acquire(lambda:None,1,key='fan'))
        await asyncio.sleep(.01)
        try:
            with pytest.raises(Refused,match='conversation-busy'):
                await gate.acquire(lambda:None,.02,key='fan')
        finally:
            gate.release('blocker');await waiting;gate.release('fan')
        assert gate.held==0 and not gate._active
    asyncio.run(run())


def test_gate_handoff_reserves_the_key_before_the_waiter_wakes():
    async def run():
        gate=SendGate(poll_s=.001)
        await gate.acquire(lambda:None,1,key='blocker')
        waiting=asyncio.create_task(gate.acquire(lambda:None,1,key='fan'))
        await asyncio.sleep(.01)
        gate.release('blocker')
        gate.set_limit(2)
        try:
            with pytest.raises(Refused,match='conversation-busy'):
                await gate.acquire(lambda:None,.02,key='fan')
        finally:
            await waiting;gate.release('fan')
        assert gate.held==0 and not gate._active
    asyncio.run(run())


def test_contended_admission_leaves_the_event_loop_free_to_release_its_lock():
    async def run():
        release=threading.Event()
        def begin(**kwargs):
            assert release.wait(.5), 'the async lock owner could not run'
            return SimpleNamespace(id=7)
        gateway=object.__new__(Gateway)
        gateway.ledger=SimpleNamespace(pg=True,begin_attempt=begin)
        async def owner():
            await asyncio.sleep(.02);release.set()
        freeing=asyncio.create_task(owner())
        assert (await gateway._begin_attempt()).id==7
        await freeing
    asyncio.run(run())


def test_cancelled_admission_records_a_definite_pre_wire_non_send():
    async def run():
        started=threading.Event();release=threading.Event();recorded=[]
        def begin(**kwargs):
            started.set();assert release.wait(.5)
            return SimpleNamespace(id=7)
        gateway=object.__new__(Gateway)
        gateway.pilot=SimpleNamespace(pilot_id='synthetic',owner_token='owner')
        gateway.ledger=SimpleNamespace(pg=True,begin_attempt=begin,record_not_sent=lambda **kw:recorded.append(kw))
        task=asyncio.create_task(gateway._begin_attempt())
        while not started.is_set():await asyncio.sleep(.001)
        task.cancel();release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert len(recorded)==1 and recorded[0]['attempt_id']==7
        assert recorded[0]['owner_token']=='owner'
    asyncio.run(run())


def test_cancelled_offloaded_ownership_does_not_leave_local_acquisition_armed():
    from twin_bridge import coordination as co
    async def run():
        started=threading.Event();release=threading.Event()
        def reserve(*a):
            started.set();assert release.wait(.5);return True
        account,fan='of-900000001',1000999
        with patch.object(co,'_durable_write_fresh',return_value=False),patch.object(co,'_reserve_atomic',reserve):
            task=asyncio.create_task(co.reserve_ownership(account_id=account,fan_id=fan,
                owner_token='synthetic-cancelled-owner',now=datetime.utcnow(),io_offload=True))
            while not started.is_set():await asyncio.sleep(.001)
            task.cancel();release.set()
            with pytest.raises(asyncio.CancelledError):await task
        assert co._key(account,fan) not in co._ACQUIRING
    asyncio.run(run())


def test_reordered_admissions_cannot_refill_the_same_elapsed_time_twice():
    from native_pilot.admission import refill_state
    start=datetime.utcnow()
    tokens,stamp=refill_state(0,start,start+timedelta(seconds=10),1,5)
    assert tokens==5
    tokens,stamp=refill_state(tokens-1,stamp,start+timedelta(seconds=7),1,5)
    assert tokens==4 and stamp==start+timedelta(seconds=10)
    tokens,stamp=refill_state(tokens-1,stamp,start+timedelta(seconds=11),1,5)
    assert tokens==4


def test_fans_share_one_schema_validated_control_pool():
    from concurrent.futures import ThreadPoolExecutor
    svc=parent();a,b=svc.child(1001),svc.child(1002)
    shared=SimpleNamespace(engine='same validated pool')
    with patch('onlyfans_runtime.store.Store',return_value=shared) as factory:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda child:child.control_store('postgresql://synthetic'),[a,b]*8))
        assert all(result is shared for result in results)
        factory.assert_called_once_with('postgresql://synthetic')
        with pytest.raises(Refused,match='control-store-target-changed'):
            a.control_store('postgresql://different')


def test_a_fans_first_bubble_goes_before_other_fans_later_bubbles():
    async def run():
        gate=SendGate(poll_s=.001);order=[]
        await gate.acquire(lambda:None,1,key='blocker')
        async def send(key,priority,later):
            await gate.acquire(lambda:None,1,priority=priority,key=key,later=later)
            order.append(key);gate.release(key)
        # fan-a is the oldest need and already answered once; its next bubble must not pass fan-c's first bubble
        tasks=[asyncio.create_task(send('a2','2026-01-01T00:00:00',True)),
               asyncio.create_task(send('b','2026-01-01T00:00:30',False)),
               asyncio.create_task(send('c','2026-01-01T00:01:00',False))]
        await asyncio.sleep(.02)
        gate.release('blocker')
        await asyncio.gather(*tasks)
        assert order==['b','c','a2']
        assert gate.held==0 and not gate._active
    asyncio.run(run())


def test_a_fan_with_its_first_bubble_sent_frees_a_preparation_slot_within_a_bound():
    async def run():
        svc=parent();svc.tuning=SimpleNamespace(concurrency=2)
        waiter=asyncio.get_running_loop().create_future()
        for fan in (1,2):
            svc.child(fan);svc.tasks[fan]=waiter
        assert not svc.has_preparation_slot()
        svc.children[1].first_bubble_sent=True
        assert svc.has_preparation_slot()                # fan 1 now only waits to send its remaining bubbles
        svc.child(3);svc.tasks[3]=waiter
        assert not svc.has_preparation_slot()            # two still preparing again
        cap=2*LIVE_TURNS_CAP_PER_SLOT
        for fan in range(3,cap+1):
            svc.child(fan);svc.tasks[fan]=waiter;svc.children[fan].first_bubble_sent=True
        svc.children[2].first_bubble_sent=True
        assert len(svc.tasks)==cap and not svc.has_preparation_slot()   # hard cap on live turns reached
        del svc.tasks[cap];assert svc.has_preparation_slot()            # parked turns below the cap do not starve a new fan
        svc.child(99);svc.tasks[99]=waiter;svc.child(98);svc.tasks[98]=waiter
        assert not svc.has_preparation_slot()                           # two preparing again
    asyncio.run(run())


def test_a_throttled_continuation_gives_the_slot_to_a_waiting_first_bubble():
    async def run():
        gate=SendGate(max_concurrency=1,poll_s=.001);order=[]
        gateway=object.__new__(Gateway)
        gateway.send_gate=gate;gateway.pilot=SimpleNamespace(fan_id=1);gateway.send_priority='a'
        gateway.abort_reason=lambda:None;gateway.send_wait_max_s=2;gateway.throttled_retries=5
        gateway.stopped_reason=None;gateway.turn_sends=1      # one bubble already out: this is the continuation
        tries=[]
        async def inner(text,terms=None):
            tries.append(1)
            if len(tries)<=2:raise Refused("allowance-throttled")
            order.append('continuation');return 'sent'
        gateway._send_inner=inner
        async def first_bubble():
            await gate.acquire(lambda:None,2,priority='b',key=2)
            order.append('first');gate.release(2)
        # the continuation holds the only slot and is throttled; a first bubble arrives and queues behind it
        sending=asyncio.create_task(gateway._send('x'))
        await asyncio.sleep(.0)
        waiting=asyncio.create_task(first_bubble())
        await asyncio.sleep(.05)
        await asyncio.wait_for(asyncio.gather(sending,waiting),5)
        assert order==['first','continuation']
        assert gate.held==0 and not gate._active
    asyncio.run(run())


def test_a_reply_waiting_at_the_send_gate_holds_no_worker_thread():
    async def run():
        from concurrent.futures import ThreadPoolExecutor
        loop=asyncio.get_running_loop()
        pool=ThreadPoolExecutor(max_workers=1);loop.set_default_executor(pool)
        release=threading.Event();loop.run_in_executor(None,release.wait)   # the only worker is busy elsewhere
        gate=SendGate(poll_s=.001)
        gateway=object.__new__(Gateway)
        gateway.send_gate=gate;gateway.pilot=SimpleNamespace(fan_id=1);gateway.send_priority='a';gateway.content=None
        gateway.abort_reason=lambda:None;gateway.send_wait_max_s=5;gateway.throttled_retries=3
        gateway.stopped_reason=None;gateway.turn_sends=0
        async def inner(text,terms=None):return 'sent:'+text
        gateway._send_inner=inner
        await gate.acquire(lambda:None,1,key='blocker')
        waiting=asyncio.create_task(gateway.send_async(1,'hi',{}))
        await asyncio.sleep(.05)
        assert not waiting.done()                       # queued at the gate while the sole worker thread is still busy
        gate.release('blocker')
        assert await asyncio.wait_for(waiting,2)=='sent:hi'
        release.set();pool.shutdown()
    asyncio.run(run())


def test_the_text_client_sends_through_the_coroutine_path_and_marks_a_card():
    async def run():
        sent=[]
        async def send_async(fan,text,kwargs):sent.append((fan,text,kwargs));return 'ok'
        svc=parent();svc.content=VaultPlan(svc.db,'*')
        native=NativeEngine(svc.db,1001,chat=None,send=None,send_async=send_async,content=None)
        client=native.make_client(svc.db)
        assert await client.send_message_async(1001,'hello there')=='ok'
        assert sent==[(1001,'hello there',{})] and not native._card_sent
        with pytest.raises(Exception):
            await client.send_message_async(1002,'wrong fan')   # same scope refusal as the blocking call
    asyncio.run(run())


def test_a_bubble_to_a_client_without_a_coroutine_send_keeps_the_threaded_call():
    from automations.ai_chatter import _send_bubble
    async def run():
        calls=[]
        class Plain:
            def send_message(self,fan,text,**kw):calls.append((fan,text,kw,threading.current_thread() is not threading.main_thread()));return 'plain'
        class Native:
            async def send_message_async(self,fan,text,**kw):calls.append((fan,text,kw,'async'));return 'native'
        assert await _send_bubble(Plain(),7,'a',price=3)=='plain'
        assert await _send_bubble(Native(),7,'b')=='native'
        assert calls==[(7,'a',{'price':3},True),(7,'b',{},'async')]
    asyncio.run(run())



def test_thread_pool_and_live_turn_factor_are_the_original_values():
    assert LIVE_TURNS_PER_SLOT==3 and 8*LIVE_TURNS_PER_SLOT+12==36 and LIVE_TURNS_CAP_PER_SLOT==8
