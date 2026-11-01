"""Active-selling records (R5): one decision row per evaluated/excluded/blocked turn, and the durable invitation.

``of_sales_decision`` is the observability record (reason for every offer AND every non-offer). It is append-only
evidence, never an authority: nothing reads it to decide a send. ``of_sales_invitation`` is the durable association
(account, fan, item, conversation) that lets a short affirmation accept THAT invitation; an acceptance only
authorises presenting the offer, never a charge.
"""
from sqlalchemy import Column, String, BigInteger, Integer, DateTime, Text, ForeignKey, CheckConstraint, UniqueConstraint, Index
from .models import Base

SINCE = {"onlyfans_since": "0107_active_selling_quote"}


class OfSalesDecision(Base):
    __tablename__ = "of_sales_decision"
    __table_args__ = (
        CheckConstraint("outcome IN ('acted','evaluated','excluded','blocked')", name="ck_sales_decision_outcome"),
        UniqueConstraint("account_id", "fan_id", "source_message_id", "stage", name="uq_sales_decision_turn"),
        Index("ix_sales_decision_account_time", "account_id", "created_at"),
        {"info": SINCE},
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
    fan_id = Column(BigInteger, nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    stage = Column(String(16), nullable=False, default="decide")     # decide | deliver
    mode = Column(String(8), nullable=False)                         # off | shadow | live
    action = Column(String(32), nullable=False)
    approach = Column(String(24))                                    # offer | invite | develop | none
    outcome = Column(String(12), nullable=False)
    reason = Column(String(64), nullable=False)
    settings_json = Column(Text)                                     # controls in force when decided
    evidence_json = Column(Text)                                     # fan-side evidence ids, counts, readiness codes
    grouped_turns = Column(Integer)                                  # creator turns since the last paid card (grouped)
    raw_bubbles = Column(Integer)                                    # raw outgoing bubbles since then (never interchangeable)
    invitation_id = Column(String(36))
    created_at = Column(DateTime, nullable=False)


class OfSalesInvitation(Base):
    __tablename__ = "of_sales_invitation"
    __table_args__ = (
        CheckConstraint("state IN ('open','accepted','declined','ignored','superseded','expired')", name="ck_sales_invitation_state"),
        Index("ix_sales_invitation_fan", "account_id", "fan_id", "state"),
        {"info": SINCE},
    )
    account_id = Column(String, ForeignKey("accounts.id", ondelete="RESTRICT"), primary_key=True)
    invitation_id = Column(String(36), primary_key=True)
    fan_id = Column(BigInteger, nullable=False)
    media_ids_json = Column(Text, nullable=False)                    # sorted media ids of the invited item
    subject = Column(String(200))
    source_message_id = Column(BigInteger, nullable=False)           # fan turn that supported it (fan-side evidence)
    state = Column(String(12), nullable=False)
    accepted_turn_id = Column(BigInteger)
    created_at = Column(DateTime, nullable=False)
    resolved_at = Column(DateTime)
