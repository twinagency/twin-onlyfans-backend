"""Rate-limited private worker diagnostics without exception values or user data."""
import logging
import time
from pathlib import PurePath
_LAST = float("-inf")
_LOG = logging.getLogger(__name__)

def record_history_failure(error):
    global _LAST
    now = time.monotonic()
    if now - _LAST < 60:
        return
    _LAST = now
    frames = []
    seen = set()
    current = error
    while current is not None and id(current) not in seen and len(seen) < 4:
        seen.add(id(current))
        tb = current.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            filename = PurePath(code.co_filename).name
            if filename in {"native_protocol.py", "native_connector.py", "provider_connector.py", "worker.py", "store.py"}:
                frames.append(filename + ":" + str(tb.tb_lineno))
            tb = tb.tb_next
        current = current.__cause__
    # Never log str(error), repr(error), locals, provider bodies, IDs or tokens.
    _LOG.warning("onlyfans_history_read_failed locations=%s", ",".join(frames[-12:]) or "unclassified")
