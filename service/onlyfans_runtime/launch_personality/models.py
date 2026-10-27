"""Owned workflow tables. Claude registers/migrates these during integration.

Importing this module registers metadata only; it never creates schema.
Revision rows are append-only; the integration migration must install the
same immutable evidence trigger used for of_configuration_change.
"""
from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from db.models import Base


class PersonalityHead(Base):
    __tablename__ = "of_personality_head"
    __table_args__ = (CheckConstraint("revision > 0", name="revision"),
        {"info": {"onlyfans_since": "0090_launch"}})
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    revision = Column(Integer, nullable=False)


class PersonalityRevision(Base):
    __tablename__ = "of_personality_revision"
    __table_args__ = (
        CheckConstraint("revision > 0 AND previous_revision >= 0 AND revision = previous_revision + 1", name="sequence"),
        CheckConstraint("state IN ('draft','approved')", name="state"),
        UniqueConstraint("account_id", "command_key", name="uq_of_personality_command"),
        {"info": {"onlyfans_since": "0090_launch"}},
    )
    account_id = Column(String, ForeignKey("of_personality_head.account_id", ondelete="RESTRICT"), primary_key=True)
    revision = Column(Integer, primary_key=True)
    previous_revision = Column(Integer, nullable=False)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    state = Column(String(16), nullable=False)
    command_key = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    document_json = Column(Text, nullable=False)
    configuration_revision = Column(Integer, nullable=False)
    configuration_fingerprint = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False)
