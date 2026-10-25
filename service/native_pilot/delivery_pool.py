"""Opt-in bounded delivery lanes with a private captured-session client per lane."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import os
from weakref import WeakKeyDictionary
from .fan_tasks import configured_fan_concurrency

_CALLBACK_POOLS = WeakKeyDictionary()

def configured_delivery_concurrency():
    raw = os.environ.get("FASTT_MAX_CONCURRENT_SENDS", "1")
    try:
        value = int(raw)
        if not 1 <= value <= 16:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("FASTT_MAX_CONCURRENT_SENDS must be an integer from 1 to 16") from None
    return value if configured_fan_concurrency() > 1 else 1

def configure_callback_workers(loop):
    lanes = configured_delivery_concurrency()
    if lanes <= 1 or loop in _CALLBACK_POOLS:
        return
    # Dedicated native worker processes own their event loop. Keep enough
    # callbacks for every bounded lane plus background reads; physical I/O
    # uses the separate send_io pool and never waits for this callback pool.
    pool = ThreadPoolExecutor(max_workers=lanes + 4, thread_name_prefix="fastt-native-callback")
    loop.set_default_executor(pool)
    _CALLBACK_POOLS[loop] = pool

class DeliveryClients:
    def __init__(self, limit, factory):
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("invalid delivery client limit")
        self._semaphore = asyncio.Semaphore(limit)
        self._factory = factory
        self._available, self._all = [], []
        self.active, self.closed = 0, False

    @asynccontextmanager
    async def borrow(self):
        async with self._semaphore:
            if self.closed:
                raise RuntimeError("delivery clients are closed")
            # connection.build_client has no network I/O. Each lane owns its
            # own Session; captured credentials/proxy remain account-bound.
            if self._available:
                client = self._available.pop()
            else:
                client = self._factory()
                if any(existing is client or existing.http is client.http for existing in self._all):
                    raise RuntimeError("delivery client factory reused a Session")
                self._all.append(client)
            self.active += 1
            try:
                yield client
            finally:
                self.active -= 1
                self._available.append(client)

    def close(self):
        if self.active:
            raise RuntimeError("delivery clients must drain before closing")
        if self.closed:
            return
        self.closed = True
        failed = False
        for client in self._all:
            try:
                client.http.close()
            except Exception:
                failed = True
        self._available.clear()
        self._all.clear()
        if failed:
            raise RuntimeError("delivery client close failed")
