"""History and ownership coverage for account preparation (0136).

Every row is keyed by ``account_id`` and nothing here is ever deleted. State is read from cursors, retained
gaps and counters, never from elapsed time. A missing record is never "not purchased": ownership is derived
by ``conversation_engine/history_ownership.py`` and only that module may say ``clear_as_of``.
"""
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer,
                        String, Text, text)
from .models import Base

REVISION = "0136_history_coverage"
INFO = {"onlyfans_since": REVISION}

SOURCES = ("vault_window", "chats", "transactions", "chargebacks")


class OfFansImportSource(Base):
    """One row per (account, source): state, counters, explicit sub-budget and the durable cursor."""
    __tablename__ = "of_fans_import_source"
    __table_args__ = (
        CheckConstraint("source IN ('vault_window','chats','transactions','chargebacks')", name="source"),
        CheckConstraint("state IN ('not_started','running','partial','complete','failed')", name="state"),
        CheckConstraint("rows_seen >= 0 AND rows_new >= 0 AND rows_duplicate >= 0 AND rows_conflict >= 0 "
                        "AND rows_ignored >= 0 AND rows_quarantined >= 0 AND rows_late >= 0 AND pages_ok >= 0 "
                        "AND pages_failed >= 0 AND retry_attempts >= 0 AND revision > 0", name="counters"),
        CheckConstraint("rows_seen = rows_new + rows_duplicate + rows_conflict + rows_ignored + rows_quarantined",
                        name="reconcile"),
        CheckConstraint("rows_late <= rows_seen", name="late"),
        CheckConstraint("requests_budget >= 0 AND credits_budget >= 0 AND requests_used >= 0 AND credits_reserved >= 0 "
                        "AND requests_used <= requests_budget AND credits_reserved <= credits_budget", name="budget"),
        CheckConstraint("min_interval_seconds >= 0 AND min_interval_seconds <= 86400", name="interval"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    source = Column(String(16), primary_key=True)
    state = Column(String(16), nullable=False)
    revision = Column(Integer, nullable=False)
    boundary_at = Column(DateTime, nullable=False)
    cursor_json = Column(Text, nullable=False)
    rows_seen = Column(Integer, nullable=False, default=0, server_default=text("0"))
    rows_new = Column(Integer, nullable=False, default=0, server_default=text("0"))
    rows_duplicate = Column(Integer, nullable=False, default=0, server_default=text("0"))
    rows_conflict = Column(Integer, nullable=False, default=0, server_default=text("0"))
    rows_ignored = Column(Integer, nullable=False, default=0, server_default=text("0"))
    rows_quarantined = Column(Integer, nullable=False, default=0, server_default=text("0"))
    rows_late = Column(Integer, nullable=False, default=0, server_default=text("0"))
    pages_ok = Column(Integer, nullable=False, default=0, server_default=text("0"))
    pages_failed = Column(Integer, nullable=False, default=0, server_default=text("0"))
    requests_budget = Column(BigInteger, nullable=False)
    credits_budget = Column(BigInteger, nullable=False)
    requests_used = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    credits_reserved = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    min_interval_seconds = Column(Integer, nullable=False)
    retry_attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    next_read_at = Column(DateTime)
    last_read_at = Column(DateTime)
    completed_at = Column(DateTime)
    last_error = Column(String(64))
    evidence_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class OfFansHistoryCursor(Base):
    """Per-fan conversation-history cursor. A fan without a row is simply not covered."""
    __tablename__ = "of_fans_history_cursor"
    __table_args__ = (
        CheckConstraint("status IN ('needed','running','complete','gapped')", name="status"),
        CheckConstraint("priority BETWEEN 0 AND 3", name="priority"),
        CheckConstraint("pages >= 0 AND messages_seen >= 0 AND retry_attempts >= 0", name="counters"),
        CheckConstraint("(status = 'complete') = (completed_at IS NOT NULL)", name="completed"),
        Index("ix_of_fans_history_cursor_work", "account_id", "status", "priority", "fan_id"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    status = Column(String(12), nullable=False)
    priority = Column(Integer, nullable=False)
    next_page = Column(Text)
    first_page_at = Column(DateTime)
    completed_at = Column(DateTime)
    newest_message_id = Column(BigInteger)
    oldest_message_at = Column(DateTime)
    pages = Column(Integer, nullable=False, default=0, server_default=text("0"))
    messages_seen = Column(Integer, nullable=False, default=0, server_default=text("0"))
    retry_attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    retry_not_before = Column(DateTime)
    last_error = Column(String(64))
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class OfFansImportGap(Base):
    """A retained failed page or quarantined row. Resolution is recorded, the row is never removed."""
    __tablename__ = "of_fans_import_gap"
    __table_args__ = (
        CheckConstraint("source IN ('chats','transactions','chargebacks')", name="source"),
        CheckConstraint("kind IN ('page','row')", name="kind"),
        CheckConstraint("attempts >= 0", name="attempts"),
        CheckConstraint("(resolved_at IS NULL) = (resolved_by IS NULL)", name="resolution"),
        Index("ix_of_fans_import_gap_open", "account_id", "source", "fan_id", "resolved_at"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    gap_id = Column(String(40), primary_key=True)
    source = Column(String(16), nullable=False)
    fan_id = Column(BigInteger)
    kind = Column(String(8), nullable=False)
    cursor_ref = Column(Text)
    reason_code = Column(String(64), nullable=False)
    detail = Column(String(96))
    http_status = Column(Integer)
    attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    exhausted = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    raw_sha256 = Column(String(64))
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    resolved_at = Column(DateTime)
    resolved_by = Column(String(128))
    resolution = Column(String(24))


class OfFansMessageFact(Base):
    """First-seen provider facts of one creator message that carried a price. NULL means unknown, not false."""
    __tablename__ = "of_fans_message_fact"
    __table_args__ = (
        CheckConstraint("price_cents > 0 AND via IN ('history','webhook')", name="shape"),
        Index("ix_of_fans_message_fact_fan", "account_id", "fan_id"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    message_id = Column(BigInteger, primary_key=True)
    price_cents = Column(Integer, nullable=False)
    media_ids_json = Column(Text, nullable=False)
    preview_ids_json = Column(Text)
    locked_text = Column(Boolean)
    from_queue = Column(Boolean)
    is_opened = Column(Boolean)
    opened_at = Column(DateTime)
    is_tip = Column(Boolean)
    conflicted = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    via = Column(String(8), nullable=False)
    raw_sha256 = Column(String(64), nullable=False)
    sent_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False)


class OfFansReadMeter(Base):
    """Hourly request, credit and outcome record per account and source; a measurement, never a limit."""
    __tablename__ = "of_fans_read_meter"
    __table_args__ = (
        CheckConstraint("requests >= 0 AND ok >= 0 AND rate_limited >= 0 AND failed >= 0 AND credits_reserved >= 0 "
                        "AND rows_read >= 0 AND ok + rate_limited + failed <= requests", name="counters"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    source = Column(String(16), primary_key=True)
    hour = Column(DateTime, primary_key=True)
    requests = Column(Integer, nullable=False, default=0, server_default=text("0"))
    ok = Column(Integer, nullable=False, default=0, server_default=text("0"))
    rate_limited = Column(Integer, nullable=False, default=0, server_default=text("0"))
    failed = Column(Integer, nullable=False, default=0, server_default=text("0"))
    credits_reserved = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    rows_read = Column(Integer, nullable=False, default=0, server_default=text("0"))
    balance_first = Column(BigInteger)
    balance_last = Column(BigInteger)
    updated_at = Column(DateTime, nullable=False)
