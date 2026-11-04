"""Durable operator cases (R1): the one owner/status record for a conversation hold.

A case carries reason, evidence, scope, owner, due time, acknowledgement, status, resolution and escalation. The
conversation CONTROL stays in ``of_conversation_state`` (paused / no_contact); this table is what explains it, who
owns it and when it is overdue, and it is the only place hardship and complaint (commercial-scope) holds live.
``status='shadow'`` rows are detector verdicts recorded WITHOUT enforcement: the policy never reads them.
"""
from sqlalchemy import (BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, text)
from .models import Base


class OfOperatorCase(Base):
    __tablename__ = "of_operator_case"
    __table_args__ = (
        CheckConstraint("status IN ('shadow','open','acknowledged','resolved')", name="ck_operator_case_status"),
        CheckConstraint("scope IN ('all','commercial','offer','nickname')", name="ck_operator_case_scope"),
        UniqueConstraint("account_id", "fan_id", "category", "source_message_id", name="uq_operator_case_source"),
        Index("uq_operator_case_active", "account_id", "fan_id", "category", unique=True,
              postgresql_where=text("status IN ('open','acknowledged')"),
              sqlite_where=text("status IN ('open','acknowledged')")),
        Index("ix_operator_case_queue", "account_id", "status", "due_at"),
        {"info": {"onlyfans_since": "0105_operator_cases"}},
    )
    account_id = Column(String, ForeignKey("accounts.id", ondelete="RESTRICT"), primary_key=True)
    case_id = Column(String(36), primary_key=True)
    fan_id = Column(BigInteger, nullable=False)
    category = Column(String(24), nullable=False)
    scope = Column(String(12), nullable=False)
    status = Column(String(12), nullable=False)
    reason = Column(String(64), nullable=False)          # fixed detector / operator code, never message text
    evidence_json = Column(Text, nullable=False)         # bounded: rule code, message ids, short excerpt
    source_message_id = Column(BigInteger)
    origin = Column(String(16), nullable=False)          # detector | operator | external
    owner = Column(String(128))
    due_at = Column(DateTime)
    acknowledged_at = Column(DateTime)
    acknowledged_by = Column(String(128))
    resolution = Column(Text)
    resolved_at = Column(DateTime)
    resolved_by = Column(String(128))
    escalated_at = Column(DateTime)
    escalation_level = Column(Integer, nullable=False, server_default=text("0"), default=0)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
