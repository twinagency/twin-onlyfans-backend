"""Request-local observation of bounded work; never delivery or model authority."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
_callback=ContextVar("twin_bounded_work_progress",default=None)

@contextmanager
def observe(callback):
 token=_callback.set(callback)
 try:yield
 finally:_callback.reset(token)

def started(phase,timeout_seconds):
 callback=_callback.get()
 if callback is None:return
 if phase not in {"model","model_identity","provider"} or type(timeout_seconds) not in (int,float):
  return
 if not math.isfinite(timeout_seconds) or not 0<timeout_seconds<=300:
  return
 # A fresh phase is reported once before a bounded network attempt, never by
 # readiness polling or a heartbeat timer. Local materialization gets five seconds.
 callback(phase,float(timeout_seconds)+5)
