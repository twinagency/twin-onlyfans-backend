"""Which runtime owns a fan's automated replies (0092).

A row means the dedicated FASTT runner process is the ONLY automated sender for
that conversation: the native reactive planner, proactive engagement and
campaigns must not act on it. Releasing sets released_at; rows are never reset.
"""
from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, ForeignKeyConstraint, String
from .models import Base


class OfRuntimeOwner(Base):
    __tablename__ = "of_runtime_owner"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "fan_id"], ["fans.account_id", "fans.fan_id"], ondelete="RESTRICT"),
        CheckConstraint("owner IN ('fastt')", name="owner"),
        {"info": {"onlyfans_since": "0092_fastt_runner"}},
    )
    account_id = Column(String, primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    owner = Column(String(16), nullable=False)
    actor = Column(String(128), nullable=False)
    claimed_at = Column(DateTime, nullable=False)
    released_at = Column(DateTime)
    released_by = Column(String(128))
