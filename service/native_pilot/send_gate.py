"""Per-process, per-account FIFO gate in front of the provider send path.

The account's send RATE is limited in the database (``of_delivery_policy.next_dispatch_at``, shared by every
process and by human/campaign sends). This gate is only the in-process half: it lets concurrently prepared
replies take their turn at the wire in arrival order instead of racing at admission, and it makes every
waiter cancellable (a takeover, a stop, a lost lease) without losing its place for anyone else.

* one holder at a time; waiters are served OLDEST-FIRST by ``priority`` (the age of the fan's first unanswered
  message), arrival order among equals, so a reply prepared late for an old message does not wait behind newer ones;
* a waiter polls ``should_abort`` about once a second and leaves the queue at once when it returns a reason;
* a waiter that waits longer than ``wait_max_s`` leaves with ``send-queue-timeout`` (its reply is released
  and answered later, never silently dropped);
* nothing here touches a database or the network.
"""
from __future__ import annotations

import asyncio
import itertools

from .ledger import Refused


class SendGate:
    def __init__(self, *, poll_s: float = 1.0):
        self._queue: list = []          # [(priority, seq, future)]
        self._seq = itertools.count()
        self._held = False
        self.poll_s = poll_s
        self.max_waiting = 0
        self.acquired = 0

    @property
    def waiting(self) -> int:
        return len(self._queue)

    @property
    def held(self) -> bool:
        return self._held

    async def acquire(self, should_abort, wait_max_s: float, priority=None) -> None:
        if not self._held and not self._queue:
            self._held = True
            self.acquired += 1
            return
        fut = asyncio.get_running_loop().create_future()
        entry = (priority if priority is not None else "\uffff", next(self._seq), fut)
        self._queue.append(entry)
        self.max_waiting = max(self.max_waiting, len(self._queue))
        waited = 0.0
        try:
            while True:
                done, _ = await asyncio.wait({fut}, timeout=self.poll_s)
                if done:
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
                self._handoff()       # we were handed the gate just as we left: pass it on
            else:
                try:
                    self._queue.remove(entry)
                except ValueError:
                    pass
                fut.cancel()
            raise

    def release(self) -> None:
        self._handoff()

    def _handoff(self) -> None:
        while self._queue:
            self._queue.sort(key=lambda e: (e[0], e[1]))
            _, _, fut = self._queue.pop(0)
            if not fut.done():
                fut.set_result(True)    # the gate stays held, now by that waiter
                return
        self._held = False
