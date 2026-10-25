"""Assembled scheduling and engine context isolation, without any real account."""
import argparse,asyncio
from types import SimpleNamespace
import pytest
from native_pilot.service import Service
from native_pilot.engine import NativeEngine,PilotBoundaryError
from telegram_runtime.capabilities import active_telegram_capabilities

def test_engine_contexts_are_isolated_across_concurrent_fans(monkeypatch,tmp_path):
    monkeypatch.setenv("DATABASE_URL","sqlite+aiosqlite:///"+str(tmp_path/"synthetic.db"))
    monkeypatch.setenv("TWIN_FASTT_CONFIG_ISOLATED","1")
    async def run():
        entered=asyncio.Event();release=asyncio.Event()
        async def first():
            with NativeEngine("of-123",1,chat=None,send=None) as engine:
                entered.set();await release.wait()
                assert active_telegram_capabilities() is engine
                with pytest.raises(PilotBoundaryError):
                    with NativeEngine("of-123",2,chat=None,send=None):pass
        async def second():
            await entered.wait()
            with NativeEngine("of-123",2,chat=None,send=None) as engine:
                assert active_telegram_capabilities() is engine
                release.set();await asyncio.sleep(0)
                assert active_telegram_capabilities() is engine
        await asyncio.gather(first(),second())
        assert active_telegram_capabilities() is None
    asyncio.run(run())

def test_service_overlaps_fans_without_reentering_same_fan(monkeypatch):
    monkeypatch.setenv("FASTT_MAX_CONCURRENT_FANS","2")
    async def run():
        args=argparse.Namespace(account="of-123",fan=None,all_fans=True)
        service=Service(args,poll_s=0.001)
        slow=asyncio.Event();started=[];finished=[]
        class Child:
            def __init__(self,fan):self.fan=fan;self.backoff_s=0
            async def serve_fan(self):
                started.append(self.fan)
                if self.fan==1:await slow.wait()
                finished.append(self.fan)
            def note(self,_):pass
        children={fan:Child(fan) for fan in [1,2,3]}
        service.child=lambda fan:children[fan]
        service.already_claimed=lambda fan,mid:fan in finished
        await service._tick_concurrent_fans([(1,1),(2,2),(3,3)])
        assert started==[1,2] and finished==[2]
        await service._tick_concurrent_fans([(1,1),(2,2),(3,3)])
        assert started==[1,2,3] and finished==[2,3]
        slow.set();await service.fan_tasks.drain()
        assert started.count(1)==1 and len(service.fan_tasks)==0
    asyncio.run(run())

def test_children_share_budget_admission_only_within_their_creator(monkeypatch):
    monkeypatch.setenv("FASTT_MAX_CONCURRENT_FANS", "2")
    async def run():
        first = Service(argparse.Namespace(account="of-123", fan=None, all_fans=True))
        other = Service(argparse.Namespace(account="of-124", fan=None, all_fans=True))
        a, b = first.child(1), first.child(2)
        assert a.budget_lock is b.budget_lock is first.budget_lock
        assert other.child(1).budget_lock is other.budget_lock
        assert first.budget_lock is not other.budget_lock
    asyncio.run(run())
