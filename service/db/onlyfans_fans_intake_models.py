"""Fans API provider link, webhook projection, payment/takeover evidence and backfill progress."""
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey,
                        ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint)
from .models import Base

REVISION = "0120_fans_intake"
INFO = {"onlyfans_since": REVISION}


class OfFansLink(Base):
    """One row per creator identity (stable OnlyFans id). The acct_ id is mutable."""
    __tablename__ = "of_fans_link"
    __table_args__ = (
        CheckConstraint("state IN ('connected','auth_failed','disconnected')", name="state"),
        CheckConstraint("reconnect_count >= 0", name="reconnect_count"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    creator_id = Column(String(255), primary_key=True)
    provider_account_id = Column(String(255), nullable=False)
    state = Column(String(16), nullable=False)
    send_blocked = Column(Boolean, nullable=False)
    block_reason = Column(String(64))
    session_refreshes = Column(Integer, nullable=False)
    reconnect_count = Column(Integer, nullable=False)
    last_event_at = Column(DateTime)
    last_event_type = Column(String(80))
    updated_at = Column(DateTime, nullable=False)


class OfFansProjection(Base):
    """Projection outcome of one raw webhook row; raw evidence is never rewritten."""
    __tablename__ = "of_fans_projection"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "epoch", "event_key"],
            ["of_fans_webhook.account_id", "of_fans_webhook.epoch", "of_fans_webhook.event_key"], ondelete="RESTRICT"),
        CheckConstraint("status IN ('pending','projected','held','ignored')", name="status"),
        {"info": INFO},
    )
    account_id = Column(String, primary_key=True)
    epoch = Column(BigInteger, primary_key=True)
    event_key = Column(String(255), primary_key=True)
    status = Column(String(16), nullable=False)
    detail = Column(String(96))
    attempts = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class OfFansEvidence(Base):
    """Payment, chargeback and human-send observations awaiting or holding a link.

    Missing evidence is never recorded as "not purchased"; a row only moves to
    applied after the canonical commerce owner accepted the exact match.
    """
    __tablename__ = "of_fans_evidence"
    __table_args__ = (
        UniqueConstraint("account_id", "dedupe_key", name="uq_fans_evidence_dedupe"),
        CheckConstraint("kind IN ('unlock','transaction','chargeback','human_sent')", name="kind"),
        CheckConstraint("status IN ('pending','paired','applied','held','dismissed')", name="status"),
        Index("ix_fans_evidence_match", "account_id", "fan_id", "kind", "status"),
        {"info": INFO},
    )
    account_id = Column(String, primary_key=True)
    evidence_id = Column(String(36), primary_key=True)
    dedupe_key = Column(String(128), nullable=False)
    kind = Column(String(16), nullable=False)
    source = Column(String(16), nullable=False)
    event_key = Column(String(255))
    fan_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger)
    provider_transaction_id = Column(String(64))
    amount_cents = Column(BigInteger)
    provider_status = Column(String(24))
    occurred_at = Column(DateTime, nullable=False)
    status = Column(String(16), nullable=False)
    reason = Column(String(96))
    peer_id = Column(String(36))
    intent_id = Column(String(36))
    grant_id = Column(String(36))
    raw_sha256 = Column(String(64), nullable=False)
    resolved_by = Column(String(128))
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class OfFansBackfill(Base):
    """Resumable, budgeted gap repair (transactions by startDate, history by cursor)."""
    __tablename__ = "of_fans_backfill"
    __table_args__ = (
        CheckConstraint("status IN ('running','complete','budget_exhausted','failed')", name="status"),
        CheckConstraint("pages_used >= 0 AND pages_budget > 0", name="pages"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    run_id = Column(String(36), primary_key=True)
    reason = Column(String(32), nullable=False)
    start_date = Column(DateTime, nullable=False)
    status = Column(String(24), nullable=False)
    pages_used = Column(Integer, nullable=False)
    pages_budget = Column(Integer, nullable=False)
    marker = Column(Text)
    rows_seen = Column(Integer, nullable=False)
    rows_new = Column(Integer, nullable=False)
    detail = Column(String(96))
    started_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
