"""Durable foundation records; history remains in messages/fans/chats.

No account, recipient, planner, media catalogue or delivery authority is
duplicated here. The account row serializes short admission transactions;
leases never span a database transaction or hold a lock during external work.
"""
from sqlalchemy import BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, false, text
from .models import Base

INFO = {"onlyfans_foundation": True}


class OfRuntime(Base):
    __tablename__ = "of_runtime"
    __table_args__ = (CheckConstraint("pending_events >= 0 AND pending_events <= 1000", name="ck_of_runtime_pending"), Index("ix_of_runtime_fair", "enabled", "last_claim_at", "account_id"), {"info": INFO})
    account_id = Column(String, ForeignKey("accounts.id", ondelete="RESTRICT"), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    enabled = Column(Boolean, nullable=False, server_default=false())
    activated_at = Column(DateTime, nullable=False)
    last_claim_at = Column(DateTime, nullable=False, server_default=text("'1970-01-01'"))
    recovery_needed = Column(Boolean, nullable=False, server_default=text("true"))
    last_sync_at = Column(DateTime)
    pending_events = Column(Integer, nullable=False, server_default=text("0"))
    configuration_revision = Column(Integer, nullable=False, server_default=text("0"),
        info={"onlyfans_since":"0079_native_configuration"})


class OfConfigurationChange(Base):
    """Immutable operator evidence; current settings remain in AccountAiConfig."""
    __tablename__="of_configuration_change"
    __table_args__=({"info":{"onlyfans_since":"0079_native_configuration"}},)
    account_id=Column(String,ForeignKey("of_runtime.account_id",ondelete="RESTRICT"),primary_key=True)
    command_key=Column(String(64),primary_key=True)
    previous_revision=Column(Integer,nullable=False)
    revision=Column(Integer,nullable=False)
    fingerprint_before=Column(String(64),nullable=False)
    fingerprint_after=Column(String(64),nullable=False)
    request_hash=Column(String(64),nullable=False)
    actor=Column(String(128),nullable=False)
    before_json=Column(Text,nullable=False)
    after_json=Column(Text,nullable=False)
    created_at=Column(DateTime,nullable=False)


class OfIngress(Base):
    __tablename__ = "of_ingress"
    __table_args__ = (
        CheckConstraint("status IN ('pending','applied','quarantined')", name="ck_of_ingress_status"),
        Index("ix_of_ingress_pending", "account_id", "status", "received_at", "event_key"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    event_key = Column(String(64), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    fan_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    source = Column(String(16), nullable=False)
    payload = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, server_default=text("'pending'"))
    reason = Column(String(64))
    received_at = Column(DateTime, nullable=False)
    applied_at = Column(DateTime)


class OfWork(Base):
    __tablename__ = "of_work"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "fan_id"], ["fans.account_id", "fans.fan_id"], ondelete="RESTRICT"),
        CheckConstraint("status IN ('waiting','claimed','blocked','done')", name="ck_of_work_status"),
        Index("ix_of_work_due", "account_id", "status", "due_at", "last_claim_at", "fan_id"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    generation = Column(Integer, nullable=False, server_default=text("0"))
    status = Column(String(16), nullable=False)
    first_input_at = Column(DateTime, nullable=False)
    last_input_at = Column(DateTime, nullable=False)
    due_at = Column(DateTime, nullable=False)
    target_message_id = Column(BigInteger, nullable=False)
    control_revision = Column(Integer, nullable=False)
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
    last_claim_at = Column(DateTime, nullable=False, server_default=text("'1970-01-01'"))


class OfRecovery(Base):
    __tablename__ = "of_recovery"
    __table_args__ = (
        CheckConstraint("status IN ('needed','running','complete')", name="ck_of_recovery_status"),
        Index("ix_of_recovery_due", "account_id", "status", "lease_until", "fan_id"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    high_water = Column(BigInteger, nullable=False, server_default=text("0"))
    upper_bound = Column(BigInteger)
    before_id = Column(BigInteger)
    generation = Column(Integer, nullable=False, server_default=text("0"))
    status = Column(String(16), nullable=False, server_default=text("'needed'"))
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
    last_complete_at = Column(DateTime)


class OfControlAudit(Base):
    __tablename__ = "of_control_audit"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "fan_id"], ["fans.account_id", "fans.fan_id"], ondelete="RESTRICT"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    command_key = Column(String(64), primary_key=True)
    fan_id = Column(BigInteger, nullable=False)
    mode = Column(String(24), nullable=False)
    previous_revision = Column(Integer, nullable=False)
    revision = Column(Integer, nullable=False)
    actor = Column(String(128), nullable=False)
    reason = Column(String(512), nullable=False)
    consent_ref = Column(String(128))
    action = Column(String(32), info={"onlyfans_since": "0078_operator_controls"})
    takeover_from = Column(String(16), info={"onlyfans_since": "0078_operator_controls"})
    takeover_to = Column(String(16), info={"onlyfans_since": "0078_operator_controls"})
    created_at = Column(DateTime, nullable=False)


class OfScan(Base):
    __tablename__ = "of_scan"
    __table_args__ = (CheckConstraint("status IN ('needed','running','complete')", name="ck_of_scan_status"), {"info": INFO})
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    generation = Column(Integer, nullable=False, server_default=text("0"))
    cursor = Column(String(1024))
    status = Column(String(16), nullable=False, server_default=text("'needed'"))
    page_count = Column(Integer, nullable=False, server_default=text("0"))
    known_fan_after = Column(BigInteger, nullable=False, server_default=text("0"))
    known_fans_complete = Column(Boolean, nullable=False, server_default=false())
    repeat_requested = Column(Boolean, nullable=False, server_default=false())
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
