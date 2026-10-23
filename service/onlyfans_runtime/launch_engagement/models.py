"""Proposed private schema. Claude owns migration and schema registration.

Separate metadata deliberately avoids changing the pinned shared schema merely
by importing this package. Production must migrate explicitly; never create_all
from an API or worker. Test databases alone install this metadata directly.
"""
from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Text, UniqueConstraint, CheckConstraint, text
from sqlalchemy.orm import declarative_base

EngagementBase = declarative_base()


class Settings(EngagementBase):
    __tablename__ = "of_engagement_settings"
    account_id = Column(String, primary_key=True)
    binding_id = Column(String, nullable=False)
    revision = Column(Integer, nullable=False)
    settings_json = Column(Text, nullable=False)


class SettingsChange(EngagementBase):
    __tablename__ = "of_engagement_settings_change"
    account_id = Column(String, primary_key=True)
    revision = Column(Integer, primary_key=True)
    binding_id = Column(String, nullable=False)
    command_key = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    settings_json = Column(Text, nullable=False)
    actor = Column(String(128), nullable=False)
    created_at = Column(DateTime, nullable=False)
    __table_args__ = (UniqueConstraint("account_id", "command_key"),)


class Action(EngagementBase):
    __tablename__ = "of_engagement_action"
    account_id = Column(String, primary_key=True)
    action_id = Column(String(36), primary_key=True)
    binding_id = Column(String, nullable=False)
    fan_id = Column(BigInteger, nullable=False)
    kind = Column(String(16), nullable=False)
    anchor_message_id = Column(BigInteger, nullable=False)
    command_key = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    snapshot_json = Column(Text, nullable=False)
    snapshot_hash = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False)
    reason = Column(String(64))
    due_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    intent_id = Column(String(36))
    cancel_requested_at = Column(DateTime)
    cancelled_by = Column(String(128))
    __table_args__ = (
        UniqueConstraint("account_id", "command_key"),
        UniqueConstraint("account_id", "intent_id"),
        UniqueConstraint("account_id", "binding_id", "fan_id", "kind", "anchor_message_id"),
        CheckConstraint("kind IN ('offer','follow_up','reengage')"),
        CheckConstraint("status IN ('scheduled','queued','dispatching','accepted','not_accepted','unknown','cancelled','expired')"),
        CheckConstraint("expires_at > due_at"),
        Index("ix_of_engagement_due", "account_id", "status", "due_at"),
        Index("uq_of_engagement_active_fan", "account_id", "fan_id", unique=True,
              postgresql_where=text("status IN ('scheduled','queued','dispatching','unknown')")),
    )
