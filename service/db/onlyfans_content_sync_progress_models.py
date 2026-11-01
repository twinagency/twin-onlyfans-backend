"""Durable source occurrences and bounded content-sync continuation.

Offer snapshots remain immutable. These rows describe the current occurrence
and outstanding work, so returning to an older snapshot cannot erase a change.
"""
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime,
    ForeignKey, Index, Integer, String, Text, false, text)

from .models import Base

INFO = {"onlyfans_since": "0091_launch_controls"}


class OfContentSyncSource(Base):
    __tablename__ = "of_content_sync_source"
    __table_args__ = (
        CheckConstraint("source_kind IN ('message','post')", name="kind"),
        CheckConstraint("source_id > 0 AND occurrence >= 0", name="bounds"),
        Index("ix_of_content_sync_source_refresh", "account_id", "binding_id", "refresh_requested_at"),
        Index("ix_of_content_sync_source_fold_pending", "account_id", "binding_id", "source_kind", "source_id",
            postgresql_where=text("fold_pending IS TRUE"), info={"onlyfans_since":"0095_content_fold_pending"}),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), primary_key=True)
    source_kind = Column(String(8), primary_key=True)
    source_id = Column(BigInteger, primary_key=True)
    fan_id = Column(BigInteger)
    current_observation_id = Column(String(36))
    current_fingerprint = Column(String(64))
    occurrence = Column(Integer, nullable=False, default=0)
    observed_at = Column(DateTime)
    folded_message_media_json = Column(Text)
    folded_message_price_cents = Column(Integer)
    folded_message_unsent = Column(Boolean)
    folded_at = Column(DateTime)
    fold_pending = Column(Boolean, nullable=False, default=False, server_default=false(),
        info={"onlyfans_since":"0095_content_fold_pending"})
    candidate_job_id = Column(String(36))
    candidate_media_key = Column(String(64))
    refresh_requested_at = Column(DateTime)
    refresh_completed_at = Column(DateTime)
    refresh_job_id = Column(String(36))
    refresh_error = Column(String(64))
    last_checked_at = Column(DateTime)
    source_unavailable = Column(Boolean, nullable=False, default=False)
    unavailable_reason = Column(String(64))


class OfContentPostProgress(Base):
    __tablename__ = "of_content_post_progress"
    __table_args__ = (
        CheckConstraint("(lease_token IS NULL) = (lease_until IS NULL)", name="lease"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    connection_revision = Column(Integer, nullable=False)
    history_marker = Column(String(256))
    history_complete = Column(Boolean, nullable=False, default=False)
    history_checked_at = Column(DateTime)
    revisit_after_id = Column(BigInteger)
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
    next_attempt_at = Column(DateTime)
    last_error = Column(String(64))
    cursor_contract = Column(String(64))
    cursor_evidence_json = Column(Text)


class OfContentSyncOccurrence(Base):
    __tablename__ = "of_content_sync_occurrence"
    __table_args__ = (
        CheckConstraint("source_kind IN ('message','post') AND source_id > 0 AND occurrence > 0", name="bounds"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), primary_key=True)
    source_kind = Column(String(8), primary_key=True)
    source_id = Column(BigInteger, primary_key=True)
    occurrence = Column(Integer, primary_key=True)
    observation_id = Column(String(36), nullable=False)
    observed_at = Column(DateTime, nullable=False)


class OfProviderReadBudget(Base):
    __tablename__ = "of_provider_read_budget"
    __table_args__ = (
        CheckConstraint("tokens >= 0 AND tokens <= 10", name="tokens"),
        CheckConstraint("(lease_token IS NULL) = (lease_until IS NULL)", name="lease"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    connection_revision = Column(Integer, nullable=False)
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
    tokens = Column(Integer, nullable=False, default=10)
    refilled_at = Column(DateTime, nullable=False)
    backoff_until = Column(DateTime)
    last_kind = Column(String(32))
    # R4: oldest background lane denied tokens (bounded-starvation turn); both NULL when nobody is starved.
    starved_kind = Column(String(32), info={"onlyfans_since": "0106_read_lane_fairness"})
    starved_since = Column(DateTime, info={"onlyfans_since": "0106_read_lane_fairness"})
