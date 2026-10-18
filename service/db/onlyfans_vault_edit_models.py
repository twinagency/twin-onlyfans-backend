"""Command evidence only; metadata and folders keep their existing owners."""
from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from .models import Base

class OfVaultEdit(Base):
    __tablename__ = "of_vault_edit"
    __table_args__ = ({"info":{"onlyfans_since":"0086_vault_edits"}},)
    account_id = Column(String, ForeignKey("of_runtime.account_id",ondelete="RESTRICT"), primary_key=True)
    command_key = Column(String(64), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id",ondelete="RESTRICT"),nullable=False)
    request_hash = Column(String(64),nullable=False)
    actor = Column(String(128),nullable=False)
    operation = Column(String(24),nullable=False)
    target_id = Column(String(24),nullable=False)
    before_json = Column(Text,nullable=False)
    after_json = Column(Text,nullable=False)
    result_json = Column(Text,nullable=False)
    created_at = Column(DateTime,nullable=False)
