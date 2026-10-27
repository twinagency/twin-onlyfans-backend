"""Immutable launch-operation commands; canonical state keeps its current owners."""
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text, Index
from db.models import Base


class OfLaunchCommand(Base):
    __tablename__ = "of_launch_command"
    __table_args__ = (Index("ix_of_launch_command_kind", "account_id", "kind", "created_at"),
        {"info": {"onlyfans_since": "0091_launch_controls"}})
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    command_key = Column(String(64), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    kind = Column(String(32), nullable=False)
    request_hash = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    reason = Column(String(512), nullable=False)
    before_json = Column(Text, nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)


class OfLedgerResolution(Base):
    """A verified projection decision, never a replacement for original evidence."""
    __tablename__ = "of_ledger_resolution"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "evidence_hash"],
            ["of_ledger_evidence.account_id", "of_ledger_evidence.evidence_hash"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["account_id", "command_key"],
            ["of_launch_command.account_id", "of_launch_command.command_key"], ondelete="RESTRICT"),
        {"info": {"onlyfans_since": "0091_launch_controls"}})
    account_id = Column(String, primary_key=True)
    evidence_hash = Column(String(64), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    command_key = Column(String(64), nullable=False)
    transaction_id = Column(Integer, ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=False)
    fan_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    decision_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)


class OfLaunchWorkerHealth(Base):
    """Current worker observation, never evidence that a send was authorized."""
    __tablename__ = "of_launch_worker_health"
    __table_args__ = {"info": {"onlyfans_since": "0091_launch_controls"}}
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    observed_at = Column(DateTime, nullable=False)
    capabilities_json = Column(Text, nullable=False)
    last_error = Column(String(64))
