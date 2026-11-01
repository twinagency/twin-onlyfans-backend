"""Stored fan agreement to a materially different alternative (R4 / F04).

One row per proposal. A proposal records the media ids that WOULD be sent and the
fan message id of the turn on which it was made; agreement records the later fan
message that accepted it. Authority for 'may a substitute be dispatched'."""
from sqlalchemy import Column, String, BigInteger, DateTime, Text, ForeignKey, CheckConstraint
from .models import Base


class OfSubstitutionAgreement(Base):
    __tablename__ = "of_substitution_agreement"
    __table_args__ = (
        CheckConstraint("state IN ('proposed','agreed')", name="ck_substitution_state"),
        {"info": {"onlyfans_since": "0106_read_lane_fairness"}},
    )
    account_id = Column(String, ForeignKey("accounts.id", ondelete="RESTRICT"), primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    proposed_turn_id = Column(BigInteger, primary_key=True)     # fan message id the proposal answered
    proposed_media_json = Column(Text, nullable=False)          # sorted media ids of the proposed alternative
    requested_subject = Column(String(200))
    state = Column(String(16), nullable=False)
    agreed_turn_id = Column(BigInteger)                         # fan message id that accepted it
    created_at = Column(DateTime, nullable=False)
    agreed_at = Column(DateTime)
