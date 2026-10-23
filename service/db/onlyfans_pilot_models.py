"""Native FASTT pilot storage (0092): durable admission ledger and writer markers.

These tables are what ``native_pilot.ledger`` and ``twin_bridge.coordination``
read and write through raw SQL. Timestamps are deliberately TEXT holding the
naive-UTC ISO strings the ledger already compares lexically (SQLite stores them
that way), so both backends order and hash identically. No runtime DDL is run on
PostgreSQL; the tables come from migration 0092 only.
"""
from sqlalchemy import BigInteger, Column, Integer, String, Text, UniqueConstraint, text

from .models import Base

INFO = {"onlyfans_since": "0092_native_pilot_storage"}


class TwinBridgeWrite(Base):
    """In-flight / unresolved local provider writer marker (the send boundary)."""
    __tablename__ = "twin_bridge_writes"
    __table_args__ = {"info": INFO}
    account_id = Column(String, primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    holder = Column(String, primary_key=True)
    expires_at = Column(String, nullable=False)
    state = Column(String, nullable=False, server_default="held")


class NativePilot(Base):
    __tablename__ = "native_pilots"
    __table_args__ = {"info": INFO}
    pilot_id = Column(String, primary_key=True)
    account_id = Column(String, nullable=False)
    fan_id = Column(BigInteger, nullable=False)
    owner_token = Column(String, nullable=False)
    cutoff = Column(String, nullable=False)
    deadline = Column(String, nullable=False)
    grant_limit = Column(Integer, nullable=False)
    baseline_used = Column(Integer, nullable=False)
    end_limit = Column(Integer, nullable=False)
    revision = Column(Integer, nullable=False)
    state = Column(String, nullable=False)
    reason = Column(String)
    model_calls = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class NativePilotInput(Base):
    __tablename__ = "native_pilot_inputs"
    __table_args__ = (UniqueConstraint("pilot_id", "message_id", name="uq_native_pilot_inputs_message"),
                      {"info": INFO})
    input_key = Column(String, primary_key=True)
    pilot_id = Column(String, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    source_hash = Column(String, nullable=False)
    created_at = Column(String, nullable=False)
    ingested_at = Column(String, nullable=False)
    member_ids_json = Column(Text, nullable=False)
    earliest_created_at = Column(String, nullable=False)
    state = Column(String, nullable=False)
    claimed_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class NativePilotReceipt(Base):
    """One row per admitted provider POST; written BEFORE the POST."""
    __tablename__ = "native_pilot_receipts"
    __table_args__ = (UniqueConstraint("input_key", "bubble_no", name="uq_native_pilot_receipts_bubble"),
                      {"info": INFO})
    attempt_id = Column(Integer, primary_key=True, autoincrement=False)
    pilot_id = Column(String, nullable=False)
    input_key = Column(String, nullable=False)
    bubble_no = Column(Integer, nullable=False)
    state = Column(String, nullable=False)
    provider_message_id = Column(String)
    response_json = Column(Text)
    evidence_json = Column(Text)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)
