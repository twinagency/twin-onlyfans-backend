"""Durable bounded vault reads and reference evidence; native assets stay canonical."""
from sqlalchemy import (BigInteger, CheckConstraint, Column, DateTime, ForeignKey,
    ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, text)
from .models import Base

INFO = {"onlyfans_since": "0083_vault_management"}


class OfVaultSyncJob(Base):
    __tablename__ = "of_vault_sync_job"
    __table_args__ = (
        UniqueConstraint("account_id", "command_key", name="uq_of_vault_sync_command"),
        CheckConstraint("(provider_kind='native' AND provider_account IS NULL) OR (provider_kind='fansapi' AND provider_account IS NOT NULL)",
            name="provider",info={"onlyfans_since":"0130_commercial_vault_provenance"}),
        CheckConstraint("status IN ('pending','running','complete','failed','superseded')", name="status"),
        CheckConstraint("connection_revision > 0 AND owner_id > 0 AND pages_read >= 0 AND max_pages = 200 AND items_seen >= 0 AND items_seen <= pages_read * 24 AND page_offset = items_seen", name="bounds"),
        CheckConstraint("(status = 'running') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)", name="lease"),
        Index("uq_of_vault_sync_active", "account_id", unique=True,
              postgresql_where=text("status IN ('pending','running')")),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    job_id = Column(String(36), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    command_key = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    provider_kind = Column(String(16), nullable=False, default="native", server_default="native",
        info={"onlyfans_since":"0130_commercial_vault_provenance"})
    provider_account = Column(String(255), info={"onlyfans_since":"0130_commercial_vault_provenance"})
    connection_revision = Column(Integer, nullable=False)
    owner_id = Column(BigInteger, nullable=False)
    max_pages = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False)
    page_offset = Column(Integer, nullable=False)
    pages_read = Column(Integer, nullable=False)
    items_seen = Column(Integer, nullable=False)
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
    window_json = Column(Text, info={"onlyfans_since": "0112_vault_recent_window"})
    last_page_hash = Column(String(64))
    last_error = Column(String(64))
    requested_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime)


class OfVaultObservation(Base):
    """Ownership/readiness provenance, not a copy of vault metadata or files."""
    __tablename__ = "of_vault_observation"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "media_id"], ["vault_items.account_id", "vault_items.media_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["account_id", "job_id"], ["of_vault_sync_job.account_id", "of_vault_sync_job.job_id"], ondelete="RESTRICT"),
        {"info": INFO},
    )
    account_id = Column(String, primary_key=True)
    media_id = Column(BigInteger, primary_key=True)
    job_id = Column(String(36), nullable=False)
    raw_hash = Column(String(64), nullable=False)
    observed_at = Column(DateTime, nullable=False)


class OfVaultProductCreate(Base):
    __tablename__ = "of_vault_product_create"
    __table_args__ = (UniqueConstraint("account_id", "media_hash", name="uq_of_vault_product_media"), {"info": INFO})
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    command_key = Column(String(64), primary_key=True)
    item_id = Column(Integer, ForeignKey("catalog_items.id", ondelete="RESTRICT"), nullable=False)
    request_hash = Column(String(64), nullable=False)
    media_hash = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    product_json = Column(Text, nullable=False)
    acknowledgement_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)
