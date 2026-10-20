"""Native admission and recovery evidence; VaultItem and vault_upload own media/runs."""
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey,
                        Index, Integer, String, Text, UniqueConstraint, text)
from .models import Base

INFO = {"onlyfans_since": "0088_native_vault_media"}


class OfVaultMediaJob(Base):
    __tablename__ = "of_vault_media_job"
    __table_args__ = (
        UniqueConstraint("account_id", "command_key", name="uq_of_vault_media_command"),
        CheckConstraint("kind IN ('upload','video')", name="kind"),
        CheckConstraint("status IN ('receiving','pending','running','complete','failed','interrupted','superseded','expired')", name="status"),
        CheckConstraint("connection_revision > 0 AND owner_id > 0", name="scope"),
        Index("ix_of_vault_media_pending", "kind", "status", "requested_at"),
        Index("ix_of_vault_media_binding", "binding_id"),
        Index("ix_of_vault_media_account_recent", "account_id", "kind", "requested_at"),
        Index("ix_of_vault_media_cleanup", "updated_at", postgresql_where=text("cleanup_required")),
        CheckConstraint("(status = 'running') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)", name="lease"),
        Index("uq_of_vault_media_active", "account_id", "kind", unique=True,
              postgresql_where=text("status IN ('receiving','pending','running')")),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    job_id = Column(String(36), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    command_key = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    kind = Column(String(8), nullable=False)
    connection_revision = Column(Integer, nullable=False)
    owner_id = Column(BigInteger, nullable=False)
    request_json = Column(Text, nullable=False)
    status = Column(String(16), nullable=False)
    run_id = Column(String(160))
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
    last_error = Column(String(64))
    cleanup_required = Column(Boolean, nullable=False, server_default=text("false"))
    requested_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime)
