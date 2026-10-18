"""Immutable edits to existing native catalogue rows, never a second catalogue."""
from sqlalchemy import (CheckConstraint, Column, DateTime, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from .models import Base

REVISION = "0082_content_management"


class OfContentChange(Base):
    __tablename__ = "of_content_change"
    __table_args__ = (
        UniqueConstraint("account_id", "item_id", "revision", name="uq_of_content_revision"),
        CheckConstraint("previous_revision >= 0 AND revision = previous_revision + 1", name="revision"),
        {"info": {"onlyfans_since": REVISION}},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    item_id = Column(Integer, ForeignKey("catalog_items.id", ondelete="RESTRICT"), primary_key=True)
    command_key = Column(String(64), primary_key=True)
    previous_revision = Column(Integer, nullable=False)
    revision = Column(Integer, nullable=False)
    fingerprint_before = Column(String(64), nullable=False)
    fingerprint_after = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    before_json = Column(Text, nullable=False)
    after_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)
