"""Virtual-time asyncio loop for the engine tests and the load harness.

Time only moves when the loop is idle: with nothing ready and no thread work outstanding, the clock jumps to the
next timer instead of sleeping. Blocking calls (the ledger's database transactions run on the loop thread; sends
and reads run in executor threads) take REAL time but no virtual time, so latencies the harness reports (model
latency, quiet windows, the 20 s send interval, queueing) are virtual and exact, while real wall-clock cost is
measured separately.

Every timer the engine uses (asyncio.sleep / wait_for / wait with timeout, ``time.monotonic`` and
``runner.now``) is patched to this clock by ``install``.
"""
from __future__ import annotations

import asyncio
import selectors
import time as _real_time
from datetime import datetime, timedelta, timezone


UNCOUNTED = object()


async def to_thread_blocking(func, *args):
    """``asyncio.to_thread`` for a function that waits on the loop (e.g. ``run_coroutine_threadsafe(...).result()``)."""
    return await asyncio.get_running_loop().run_in_executor(UNCOUNTED, func, *args)


class _VSelector:
    """Proxy selector: replaces a blocking wait with a clock jump when no thread work is pending."""

    def __init__(self, real, loop):
        self._real, self._loop = real, loop

    def select(self, timeout=None):
        loop = self._loop
        if timeout == 0:
            return self._real.select(0)           # something is ready to run: just poll
        if loop._outstanding > 0:
            # Thread work in flight: block until a thread finishes (it wakes the loop through the self-pipe);
            # virtual time stays frozen. A thread that is stuck would show here as a hang, not a silent skip.
            return self._real.select(5.0)
        if timeout is None:
            events = self._real.select(0.0005)
            return events
        if timeout > 0:
            events = self._real.select(0)
            if events:
                return events
            loop._vt += timeout
            return []
        return self._real.select(0)

    def __getattr__(self, name):
        return getattr(self._real, name)


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__()
        self._vt = 0.0
        self._outstanding = 0
        self._selector = _VSelector(self._selector, self)

    def time(self):
        return self._vt

    def run_in_executor(self, executor, func, *args):
        if executor is UNCOUNTED:
            # A thread that blocks on the loop itself (the engine's synchronous send waiting for the async send
            # path): virtual time must keep moving while it waits, so it is not counted as real work.
            return super().run_in_executor(None, func, *args)
        fut = super().run_in_executor(executor, func, *args)
        self._outstanding += 1
        fut.add_done_callback(self._thread_done)
        return fut

    def _thread_done(self, _fut):
        self._outstanding -= 1


class Clock:
    """Wall-clock facade over the loop's virtual time (naive UTC like ``runner.now``)."""

    def __init__(self, loop: VirtualLoop, start: datetime | None = None):
        self.loop = loop
        self.start = start or datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.loop._vt)

    __call__ = now

    def monotonic(self) -> float:
        return 1000.0 + self.loop._vt


def run(coro_factory):
    """Run ``coro_factory(loop, clock)`` on a fresh virtual loop and return its result."""
    loop = VirtualLoop()
    clock = Clock(loop)
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro_factory(loop, clock))
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            asyncio.set_event_loop(None)
            loop.close()
