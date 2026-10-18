"""Twin fact projection and explicit OF overrides, never a second creator owner."""
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, CheckConstraint
from .models import Base


class OfCreatorFactsSource(Base):
    __tablename__ = "of_creator_facts_source"
    __table_args__ = (CheckConstraint("(refresh_token IS NULL) = (refresh_until IS NULL)", name="lease"),
        {"info": {"onlyfans_since":"0085_shared_creator_facts"}})
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    source_json = Column(Text)
    source_hash = Column(String(64))
    source_updated_at = Column(DateTime)
    overrides_json = Column(Text, nullable=False)
    checked_at = Column(DateTime)
    attempted_at = Column(DateTime)
    refresh_token = Column(String(36))
    refresh_until = Column(DateTime)
    last_error = Column(String(64))
