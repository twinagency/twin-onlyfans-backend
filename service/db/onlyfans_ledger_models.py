"""Ledger scan leases and immutable observations; Transaction remains the money owner."""
from sqlalchemy import Column, String, Integer, BigInteger, DateTime, ForeignKey, Text, CheckConstraint, Index, UniqueConstraint
from .models import Base

INFO = {"onlyfans_since": "0084_native_ledger"}
MARKERS = {"onlyfans_since": "0093_ledger_markers"}
EXCLUSION = {"onlyfans_since": "0098_ledger_exclusion"}


class OfLedgerScan(Base):
    __tablename__ = "of_ledger_scan"
    __table_args__ = (
        CheckConstraint("page_offset >= 0 AND page_offset <= 10000", name="offset"),
        CheckConstraint("status IN ('idle','running','held')", name="status"),
        CheckConstraint("(lease_token IS NULL) = (lease_until IS NULL)", name="lease"),
        CheckConstraint("continuation_json IS NULL OR length(continuation_json) <= 8192", name="continuation", info=MARKERS),
        CheckConstraint("coverage_json IS NULL OR length(coverage_json) <= 2048", name="coverage", info=MARKERS),
        Index("ix_of_ledger_due", "next_poll_at", "account_id"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    connection_revision = Column(Integer, nullable=False)
    owner_id = Column(BigInteger, nullable=False)
    window_start = Column(DateTime, nullable=False)
    window_end = Column(DateTime, nullable=False)
    page_offset = Column(Integer, nullable=False)
    continuation_json = Column(Text, info=MARKERS)
    coverage_json = Column(Text, info=MARKERS)
    status = Column(String(16), nullable=False)
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
    next_poll_at = Column(DateTime, nullable=False)
    last_page_hash = Column(String(64))
    last_complete_at = Column(DateTime)
    last_full_at = Column(DateTime)
    last_error = Column(String(64))
    updated_at = Column(DateTime, nullable=False)


class OfLedgerEvidence(Base):
    __tablename__ = "of_ledger_evidence"
    __table_args__ = (
        CheckConstraint("outcome IN ('applied','stale','conflict')", name="outcome"),
        Index("ix_of_ledger_evidence_provider", "account_id", "provider_transaction_id"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    evidence_hash = Column(String(64), primary_key=True)
    provider_transaction_id = Column(String(64), nullable=False)
    fan_id = Column(BigInteger)
    transaction_id = Column(Integer, ForeignKey("transactions.id", ondelete="RESTRICT"))
    outcome = Column(String(16), nullable=False)
    observation_json = Column(Text, nullable=False)
    recorded_at = Column(DateTime, nullable=False)


class OfLedgerHistory(Base):
    __tablename__ = "of_ledger_history"
    __table_args__ = (
        CheckConstraint("page_offset >= 0 AND page_offset <= 1000000000", name="offset"),
        CheckConstraint("status IN ('idle','running','held')", name="status"),
        CheckConstraint("(lease_token IS NULL) = (lease_until IS NULL)", name="lease"),
        CheckConstraint("continuation_json IS NULL OR length(continuation_json) <= 8192", name="continuation", info=MARKERS),
        CheckConstraint("coverage_json IS NULL OR length(coverage_json) <= 2048", name="coverage", info=MARKERS),
        Index("ix_of_ledger_history_due", "next_poll_at", "account_id"),
        {"info": {"onlyfans_since": "0092_ledger_history"}},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    connection_revision = Column(Integer, nullable=False)
    owner_id = Column(BigInteger, nullable=False)
    window_start = Column(DateTime, nullable=False)
    window_end = Column(DateTime, nullable=False)
    page_offset = Column(Integer, nullable=False)
    continuation_json = Column(Text, info=MARKERS)
    coverage_json = Column(Text, info=MARKERS)
    status = Column(String(16), nullable=False)
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
    next_poll_at = Column(DateTime, nullable=False)
    last_page_hash = Column(String(64))
    last_complete_at = Column(DateTime)
    last_full_at = Column(DateTime)
    last_error = Column(String(64))
    updated_at = Column(DateTime, nullable=False)


class OfLedgerExclusion(Base):
    """Operator decision that one unlinked paid purchase no longer holds the whole account.

    It never links, clears or rewrites the Transaction: the purchase stays unlinked and keeps
    blocking paid offers to its own fan. Only the account-wide hold ignores it.
    """
    __tablename__ = "of_ledger_exclusion"
    __table_args__ = (
        UniqueConstraint("account_id", "transaction_id", name="account_transaction"),
        UniqueConstraint("command_key", name="command"),
        CheckConstraint("length(trim(reason)) > 0", name="reason"),
        CheckConstraint("length(trim(actor)) > 0", name="actor"),
        {"info": EXCLUSION},
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), nullable=False)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    fan_id = Column(BigInteger, nullable=False)
    transaction_id = Column(Integer, ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=False)
    blocker = Column(String(64), nullable=False)
    reason = Column(String(512), nullable=False)
    actor = Column(String(128), nullable=False)
    command_key = Column(String(64), nullable=False)
    evidence_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False)
