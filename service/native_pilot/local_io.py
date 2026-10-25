"""Bounded database callbacks that cannot block the creator event loop.

The synchronous native sender uses the default executor and awaits the event
loop. Local bookkeeping therefore has its own pool. Cancellation drains an
already-started callback before ownership or transports can be released.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fastt-local-io")


async def run_blocking(function, /, *args, **kwargs):
    call = partial(copy_context().run, function, *args, **kwargs)
    future = asyncio.get_running_loop().run_in_executor(_POOL, call)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not future.cancelled():
            try:
                future.result()
            except BaseException:
                pass
        raise
