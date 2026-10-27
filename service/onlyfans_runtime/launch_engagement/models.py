"""Engagement tables, registered on the shared schema as 0090_launch.

Production migrates explicitly; never create_all from an API or worker.
Foreign keys and the immutable guards follow schema_contract.sql.
"""
from sqlalchemy import (BigInteger, Column, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer,
                        String, Text, UniqueConstraint, CheckConstraint, text)

from db.models import Base, Fan
from db.onlyfans_delivery_models import OfTextIntent

EngagementBase = Base  # shared registry; create_all is a no-op on a migrated database
INFO = {"onlyfans_since": "0090_launch"}


class Settings(EngagementBase):
    __tablename__ = "of_engagement_settings"
    __table_args__ = {"info": INFO}
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    revision = Column(Integer, nullable=False)
    settings_json = Column(Text, nullable=False)


class SettingsChange(EngagementBase):
    __tablename__ = "of_engagement_settings_change"
    __table_args__ = (UniqueConstraint("account_id", "command_key", name="uq_of_engagement_change_command"), {"info": INFO})
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    revision = Column(Integer, primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    command_key = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    settings_json = Column(Text, nullable=False)
    actor = Column(String(128), nullable=False)
    created_at = Column(DateTime, nullable=False)


class Action(EngagementBase):
    __tablename__ = "of_engagement_action"
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    action_id = Column(String(36), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
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
        ForeignKeyConstraint(["account_id", "fan_id"], [Fan.account_id, Fan.fan_id], ondelete="RESTRICT"),
        ForeignKeyConstraint(["account_id", "intent_id"], [OfTextIntent.account_id, OfTextIntent.intent_id], ondelete="RESTRICT"),
        UniqueConstraint("account_id", "command_key", name="uq_of_engagement_action_command"),
        UniqueConstraint("account_id", "intent_id", name="uq_of_engagement_action_intent"),
        UniqueConstraint("account_id", "binding_id", "fan_id", "kind", "anchor_message_id", name="uq_of_engagement_action_anchor"),
        CheckConstraint("kind IN ('offer','follow_up','reengage')", name="kind"),
        CheckConstraint("status IN ('scheduled','queued','dispatching','accepted','not_accepted','unknown','cancelled','expired')", name="status"),
        CheckConstraint("expires_at > due_at", name="window"),
        Index("ix_of_engagement_due", "account_id", "status", "due_at"),
        Index("uq_of_engagement_active_fan", "account_id", "fan_id", unique=True,
              postgresql_where=text("status IN ('scheduled','queued','dispatching','unknown')")),
        {"info": INFO},
    )
