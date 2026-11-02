"""Per-process, per-account bounded send gate.

The account's durable provider budget is the authority (token bucket, concurrency
slots and per-conversation ownership in the ledger). This gate is the in-process
half: it bounds how many prepared replies may wait on / occupy the provider wire
without letting one process open an unbounded number of network calls.

* legacy mode uses ``max_concurrency=1`` and thereby preserves the old one-holder
  behaviour exactly;
* concurrent mode uses the account's configured send concurrency. Waiters are
  served oldest-first by ``priority`` (the age of the fan's first unanswered
  message), arrival order among equals;
* one conversation cannot hold two gate slots at once;
* a waiter polls ``should_abort`` about once a second and leaves on takeover,
  stop or a lost lease;
* nothing here touches a database or the network.
"""
from __future__ import annotations

import asyncio
import itertools

from .ledger import Refused


class SendGate:
    def __init__(self, *, max_concurrency: int = 1, poll_s: float = 1.0):
        self._max = max(1, int(max_concurrency))
        self._queue: list = []          # [(priority, seq, key, future)]
        self._active: set = set()       # conversation keys currently holding a slot
        self._seq = itertools.count()
        self._held = 0
        self.poll_s = poll_s
        self.max_waiting = 0
        self.acquired = 0

    @property
    def limit(self) -> int:
        return self._max

    def set_limit(self, max_concurrency: int) -> None:
        """Change the process bound (legacy=1, concurrent=configured)."""
        self._max = max(1, int(max_concurrency))
        while self._held < self._max and self._queue:
            self._handoff()

    @property
    def waiting(self) -> int:
        return len([e for e in self._queue if not e[3].done()])

    @property
    def held(self) -> int:
        return self._held

    @property
    def in_flight(self) -> int:
        return self._held

    async def acquire(self, should_abort, wait_max_s: float, priority=None, key=None) -> None:
        if key is not None and (key in self._active or any(
                e[2] == key and not e[3].done() for e in self._queue)):
            raise Refused("conversation-busy")
        if self._held < self._max and not self._queue:
            self._held += 1
            self.acquired += 1
            if key is not None:
                self._active.add(key)
            return
        fut = asyncio.get_running_loop().create_future()
        entry = (priority if priority is not None else "\uffff", next(self._seq), key, fut)
        self._queue.append(entry)
        self.max_waiting = max(self.max_waiting, len(self._queue))
        waited = 0.0
        try:
            while True:
                done, _ = await asyncio.wait({fut}, timeout=self.poll_s)
                if done:
                    fut.result()  # cancellation is never a granted slot
                    self.acquired += 1
                    return
                reason = should_abort()
                if reason:
                    raise Refused(reason)
                waited += self.poll_s
                if waited >= wait_max_s:
                    raise Refused("send-queue-timeout")
        except BaseException:
            if fut.done() and not fut.cancelled():
                if key is not None:
                    self._active.discard(key)
                self._release_handoff()       # we were handed the gate just as we left: pass it on
            else:
                try:
                    self._queue.remove(entry)
                except ValueError:
                    pass
                fut.cancel()
            raise

    def release(self, key=None) -> None:
        if key is not None:
            self._active.discard(key)
        self._release_handoff()

    def _release_handoff(self) -> None:
        if self._held > 0:
            self._held -= 1
        self._handoff()

    def _handoff(self) -> None:
        while self._held < self._max and self._queue:
            self._queue.sort(key=lambda e: (e[0], e[1]))
            _, _, key, fut = self._queue.pop(0)
            if fut.done():
                continue
            if key is not None and key in self._active:
                fut.cancel()
                continue
            self._held += 1
            if key is not None:
                self._active.add(key)  # reserve before waking the waiter
            fut.set_result(True)
            return
