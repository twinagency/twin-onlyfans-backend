"""Durable operator-authored broadcasts over the shared delivery authority.

The audience is a frozen set of canonical local fans. These rows never stand
in for provider delivery receipts, audience rosters, opens or payment records.
"""
from sqlalchemy import (BigInteger, CheckConstraint, Column, DateTime,
    ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text,
    UniqueConstraint)

from .models import Base

REVISION = "0080_onlyfans_campaigns"
INFO = {"onlyfans_since": REVISION}


class OfCampaign(Base):
    __tablename__ = "of_campaign"
    __table_args__ = (
        UniqueConstraint("account_id", "command_key", name="uq_of_campaign_command"),
        CheckConstraint("status IN ('scheduled','running','paused','cancelled','completed')", name="status"),
        CheckConstraint("audience_kind IN ('explicit','all_known')", name="audience_kind"),
        CheckConstraint("recipient_count > 0 AND recipient_count <= 1000", name="recipient_count"),
        CheckConstraint("revision > 0", name="revision"),
        CheckConstraint("(catalog_item_id IS NULL AND catalog_snapshot_json IS NULL) OR (catalog_item_id IS NOT NULL AND catalog_snapshot_json IS NOT NULL)", name="offer_shape"),
        Index("ix_of_campaign_due", "account_id", "status", "scheduled_at", "campaign_id"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    campaign_id = Column(String(36), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    command_key = Column(String(64), nullable=False)
    command_hash = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    name = Column(String(128), nullable=False)
    body = Column(Text, nullable=False)
    catalog_item_id = Column(Integer, ForeignKey("catalog_items.id", ondelete="RESTRICT"))
    catalog_snapshot_json = Column(Text)
    audience_kind = Column(String(16), nullable=False)
    audience_hash = Column(String(64), nullable=False)
    recipient_count = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False)
    revision = Column(Integer, nullable=False)
    scheduled_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class OfCampaignRecipient(Base):
    __tablename__ = "of_campaign_recipient"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "campaign_id"], ["of_campaign.account_id", "of_campaign.campaign_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["account_id", "fan_id"], ["fans.account_id", "fans.fan_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["account_id", "intent_id"], ["of_text_intent.account_id", "of_text_intent.intent_id"], ondelete="RESTRICT"),
        UniqueConstraint("account_id", "intent_id", name="uq_of_campaign_recipient_intent"),
        CheckConstraint("status IN ('pending','queued','skipped','cancelled')", name="status"),
        Index("ix_of_campaign_recipient_pending", "account_id", "campaign_id", "status", "fan_id"),
        {"info": INFO},
    )
    account_id = Column(String, primary_key=True)
    campaign_id = Column(String(36), primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    status = Column(String(16), nullable=False)
    intent_id = Column(String(36))
    reason = Column(String(128))
    updated_at = Column(DateTime, nullable=False)


class OfCampaignChange(Base):
    __tablename__ = "of_campaign_change"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "campaign_id"], ["of_campaign.account_id", "of_campaign.campaign_id"], ondelete="RESTRICT"),
        UniqueConstraint("account_id", "command_key", name="uq_of_campaign_change_command"),
        CheckConstraint("revision > 0", name="revision"),
        {"info": INFO},
    )
    account_id = Column(String, primary_key=True)
    campaign_id = Column(String(36), primary_key=True)
    revision = Column(Integer, primary_key=True)
    command_key = Column(String(64), nullable=False)
    command_hash = Column(String(64), nullable=False)
    action = Column(String(16), nullable=False)
    actor = Column(String(128), nullable=False)
    before_json = Column(Text, nullable=False)
    after_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)
