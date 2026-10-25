"""Bounded I/O workers for sends called back from synchronous native workers.

A native sender can occupy the default executor while Gateway.send waits for
its loop coroutine. That coroutine must not request the same executor for its
history, pause checks or physical POST: a full pool would wait on itself.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="fastt-send-io")


async def run_blocking(function, /, *args, **kwargs):
    context = copy_context()
    call = partial(context.run, function, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(_POOL, call)
