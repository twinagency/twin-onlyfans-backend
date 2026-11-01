"""Provider-reported earnings totals per account and reporting period.

One row per (account, period), refreshed in the background from the creator's own OnlyFans earnings
statistics. Read-only reporting cache: it never feeds the payment ledger, offers or sending.
"""
from sqlalchemy import Column, String, BigInteger, Integer, DateTime, ForeignKey, CheckConstraint
from .models import Base

PERIODS = ("7d", "30d", "90d", "all")


class OfEarningsTotal(Base):
    __tablename__ = "of_earnings_total"
    __table_args__ = (
        CheckConstraint("period IN ('7d','30d','90d','all')", name="ck_earnings_total_period"),
        CheckConstraint("gross_cents >= 0 AND net_cents >= 0", name="ck_earnings_total_amounts"),
        {"info": {"onlyfans_since": "0105_earnings_totals"}},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="CASCADE"), primary_key=True)
    period = Column(String(8), primary_key=True)
    binding_id = Column(String, nullable=False)
    connection_revision = Column(Integer, nullable=False)
    gross_cents = Column(BigInteger, nullable=False)
    net_cents = Column(BigInteger, nullable=False)
    window_start = Column(DateTime, nullable=False)
    window_end = Column(DateTime, nullable=False)
    fetched_at = Column(DateTime, nullable=False)
