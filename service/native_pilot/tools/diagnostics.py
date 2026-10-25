"""Synthetic benchmark diagnostics: stack locations only, never locals or payloads."""
import asyncio
from collections import Counter
from pathlib import Path
import sys
import threading


def _label(frame):
    return f"{Path(frame.f_code.co_filename).name}:{frame.f_code.co_name}:{frame.f_lineno}"


def capture_waits():
    task_waits = Counter()
    for task in asyncio.all_tasks():
        if task is asyncio.current_task() or task.done():
            continue
        coro, chain = task.get_coro(), []
        seen = set()
        while coro is not None and id(coro) not in seen:
            seen.add(id(coro))
            frame = getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None)
            if frame is not None:
                chain.append(_label(frame))
            coro = getattr(coro, "cr_await", None) or getattr(coro, "gi_yieldfrom", None)
        task_waits[" -> ".join(chain[-8:]) or "waiting"] += 1
    thread_waits = Counter()
    names = {t.ident: t.name for t in threading.enumerate()}
    current = threading.get_ident()
    for ident, frame in sys._current_frames().items():
        if ident == current:
            continue
        chain = []
        while frame is not None and len(chain) < 10:
            chain.append(_label(frame))
            frame = frame.f_back
        thread_waits[names.get(ident, "thread").split("_")[0] + ": " + " <- ".join(chain)] += 1
    return {"tasks": dict(task_waits.most_common(20)), "threads": dict(thread_waits.most_common(20))}
