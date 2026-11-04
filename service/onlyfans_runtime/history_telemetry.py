"""Durable page and HTTP-attempt telemetry, scoped with contextvars per worker.

A request counter records attempts at dispatch, not provider acceptance. A crash
leaves an unfinished attempt visible. No message bodies, URLs or credentials.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import uuid
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, BigInteger
from sqlalchemy.orm import Session
from db.models import Base
from .store import utcnow
from .history_policy import policy


class OfHistoryReadAttempt(Base):
    __tablename__ = 'of_history_read_attempt'
    __table_args__ = ({'info':{'onlyfans_since':'0110_recent_history'}},)
    id = Column(String(36), primary_key=True)
    account_id = Column(String, ForeignKey('of_runtime.account_id', ondelete='RESTRICT'), nullable=False, index=True)
    binding_id = Column(String, nullable=False)
    fan_id = Column(BigInteger)
    scope = Column(String(16), nullable=False)
    operation = Column(String(16), nullable=False)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime)
    http_attempts = Column(Integer, nullable=False, default=0)
    outcome = Column(String(16), nullable=False, default='unfinished')

_ACTIVE = ContextVar('onlyfans_history_read_attempt', default=None)


def dispatched():
    active = _ACTIVE.get()
    if active is None:
        return
    store, attempt_id = active
    with Session(store.engine) as s, s.begin():
        row = s.get(OfHistoryReadAttempt, attempt_id, with_for_update=True)
        row.http_attempts += 1


@contextmanager
def measure(store, claim, *, discovery):
    with Session(store.engine) as s:
        enabled = policy(s, claim.account_id, claim.binding_id) is not None
    if not enabled:
        yield
        return
    attempt_id = str(uuid.uuid4())
    with Session(store.engine) as s, s.begin():
        s.add(OfHistoryReadAttempt(id=attempt_id, account_id=claim.account_id, binding_id=claim.binding_id,
            fan_id=None if discovery else claim.fan_id, scope=claim.scope,
            operation='discovery' if discovery else 'messages', started_at=utcnow()))
    token = _ACTIVE.set((store, attempt_id))
    outcome = 'succeeded'
    try:
        yield
    except BaseException:
        outcome = 'failed'
        raise
    finally:
        _ACTIVE.reset(token)
        with Session(store.engine) as s, s.begin():
            row = s.get(OfHistoryReadAttempt, attempt_id)
            row.finished_at, row.outcome = utcnow(), outcome
