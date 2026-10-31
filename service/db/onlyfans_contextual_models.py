"""One contextual origin record, independent of price/caption and delivery retries."""
from sqlalchemy import Column, String, BigInteger, DateTime, Text, ForeignKey, CheckConstraint, UniqueConstraint, Index, text
from .models import Base


class OfContextualOffer(Base):
    __tablename__ = "of_contextual_offer"
    __table_args__ = (
        CheckConstraint("state IN ('reserved','sent','unknown','purchased','cancelled')", name="ck_contextual_state"),
        UniqueConstraint("account_id", "fan_id", "source_message_id", name="uq_contextual_turn"),
        Index("uq_contextual_unresolved", "account_id", "fan_id", unique=True,
              postgresql_where=text("state IN ('reserved','sent','unknown')"),
              sqlite_where=text("state IN ('reserved','sent','unknown')")),
        {"info":{"onlyfans_since":"0101_contextual_offers"}},
    )
    account_id = Column(String, ForeignKey("accounts.id", ondelete="RESTRICT"), primary_key=True)
    decision_key = Column(String(64), primary_key=True)
    fan_id = Column(BigInteger, nullable=False)
    binding_id = Column(String, nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    content_key = Column(String(64), nullable=False)
    snapshot_json = Column(Text, nullable=False)
    attempt_id = Column(BigInteger)
    message_id = Column(BigInteger)
    state = Column(String(16), nullable=False)
    created_at = Column(DateTime, nullable=False)
