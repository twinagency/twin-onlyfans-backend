"""History-memory tables, registered on the shared schema as 0090_launch.

Canonical messages, Fan and FanProfile remain their existing owners. These
tables contain processing receipts, source evidence and explicit corrections.
Never created at startup; the 0090 migration owns the DDL.
"""
from sqlalchemy import (BigInteger, Column, DateTime, ForeignKeyConstraint,
                        Integer, String, Text, Boolean, Index, CheckConstraint)
from db.models import Base, Fan, Message

MemoryBase = Base  # shared registry; create_all is a no-op on a migrated database
INFO = {"onlyfans_since": "0090_launch"}


def fan_fk():
    return ForeignKeyConstraint(["account_id", "fan_id"],
                                [Fan.account_id, Fan.fan_id], ondelete="RESTRICT")


def source_fk():
    return ForeignKeyConstraint(["account_id", "fan_id", "message_id"],
                                [Message.account_id, Message.fan_id, Message.message_id],
                                ondelete="RESTRICT")


class MemoryState(MemoryBase):
    __tablename__ = "of_history_memory_state"
    __table_args__ = (fan_fk(), {"info": INFO})
    account_id = Column(String, primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    revision = Column(BigInteger, nullable=False, default=0)
    last_processed_at = Column(DateTime)


class MemoryReceipt(MemoryBase):
    __tablename__ = "of_history_memory_receipt"
    __table_args__ = (source_fk(), {"info": INFO})
    account_id = Column(String, primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    message_id = Column(BigInteger, primary_key=True)
    algorithm = Column(String(40), nullable=False)
    source_digest = Column(String(64), nullable=False)
    # MD5 is only an indexed SQL change detector, not evidence authentication.
    body_md5 = Column(String(32), nullable=False)
    source_direction = Column(String(16), nullable=False)
    source_at = Column(DateTime, nullable=False)
    source_unsent = Column(Boolean, nullable=False)
    processed_at = Column(DateTime, nullable=False)


class MemoryEvidence(MemoryBase):
    __tablename__ = "of_history_memory_evidence"
    __table_args__ = (source_fk(),
        Index("ix_history_memory_field", "account_id", "fan_id", "field", "source_at"), {"info": INFO})
    account_id = Column(String, primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    message_id = Column(BigInteger, primary_key=True)
    field = Column(String(32), primary_key=True)
    algorithm = Column(String(40), primary_key=True)
    source_digest = Column(String(64), primary_key=True)
    value = Column(String(80), nullable=False)
    quote = Column(String(500), nullable=False)
    source_at = Column(DateTime, nullable=False)


class MemoryCorrection(MemoryBase):
    __tablename__ = "of_history_memory_correction"
    __table_args__ = (fan_fk(),
        CheckConstraint("operation IN ('set','clear')", name="operation"),
        Index("ix_history_memory_correction", "account_id", "fan_id", "field", "revision"), {"info": INFO})
    account_id = Column(String, primary_key=True)
    command_key = Column(String(64), primary_key=True)
    fan_id = Column(BigInteger, nullable=False)
    field = Column(String(32), nullable=False)
    operation = Column(String(8), nullable=False)
    value = Column(Text)
    before_json = Column(Text, nullable=False)
    request_hash = Column(String(64), nullable=False)
    revision = Column(BigInteger, nullable=False)
    actor = Column(String(128), nullable=False)
    reason = Column(String(512), nullable=False)
    created_at = Column(DateTime, nullable=False)
