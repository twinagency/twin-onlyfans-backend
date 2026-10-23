"""Durable dashboard retry commands for a held ledger scan (append-only evidence).

A row records who asked, the exact request and the acknowledged result; it is
written in the same transaction that makes the held scan due, never afterwards.
Registered by db.onlyfans_launch_models; never create_all at runtime.
"""
from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String
from db.models import Base


class LedgerRetryCommand(Base):
    __tablename__ = "of_ledger_retry_command"
    __table_args__ = (
        CheckConstraint("result IN ('queued')", name="result"),
        CheckConstraint("expected_connection_revision >= 0", name="revision"),
        {"info": {"onlyfans_since": "0090_launch"}},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    command_key = Column(String(64), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    request_hash = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    expected_connection_revision = Column(Integer, nullable=False)
    expected_scan_fingerprint = Column(String(64), nullable=False)
    result = Column(String(16), nullable=False)
    created_at = Column(DateTime, nullable=False)
