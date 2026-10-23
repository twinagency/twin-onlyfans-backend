"""Paid-offer import evidence. CatalogItem stays the only catalogue owner.

Observations record exactly what one provider source showed; candidates are
review aids only. Decisions are immutable and are the only path from an exact
observation to a disabled fixed-price catalogue draft.
"""
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey,
    ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, text)
from .models import Base

INFO = {"onlyfans_since": "0089_offer_import"}


class OfOfferObservation(Base):
    __tablename__ = "of_offer_observation"
    __table_args__ = (
        UniqueConstraint("account_id", "binding_id", "source_kind", "source_id", "fingerprint",
                         name="uq_of_offer_observation_source"),
        CheckConstraint("source_kind IN ('message','post')", name="kind"),
        CheckConstraint("source_context IN ('direct','queue','unknown')", name="context"),
        CheckConstraint("source_id > 0 AND price_cents > 0 AND currency = 'USD'", name="terms"),
        Index("ix_of_offer_observation_media", "account_id", "media_key"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    observation_id = Column(String(36), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    source_kind = Column(String(8), nullable=False)
    source_id = Column(BigInteger, nullable=False)
    fan_id = Column(BigInteger)
    source_context = Column(String(8), nullable=False)
    source_at = Column(DateTime, nullable=False)
    observed_at = Column(DateTime, nullable=False)
    # Ordered exactly as the source listed them. NULL previews/locked_text mean
    # unknown; an empty list and False are explicit provider facts.
    media_json = Column(Text, nullable=False)
    preview_json = Column(Text)
    locked_text = Column(Boolean)
    price_cents = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False)
    parser_version = Column(String(16), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    media_key = Column(String(64), nullable=False)
    reasons_json = Column(Text, nullable=False)


class OfOfferImportJob(Base):
    __tablename__ = "of_offer_import_job"
    __table_args__ = (
        UniqueConstraint("account_id", "command_key", name="uq_of_offer_import_job_command"),
        CheckConstraint("mode IN ('inventory','refresh')", name="mode"),
        CheckConstraint("status IN ('pending','running','complete','partial','failed','cancelled','superseded')", name="status"),
        CheckConstraint("(status = 'running') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)", name="lease"),
        CheckConstraint("revision > 0 AND rows_scanned >= 0 AND pages_read >= 0 AND pages_read <= page_budget"
                        " AND targets_done >= 0 AND targets_done <= targets_total AND targets_total <= 50", name="bounds"),
        Index("uq_of_offer_import_job_active", "account_id", "mode", unique=True,
              postgresql_where=text("status IN ('pending','running')")),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    job_id = Column(String(36), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    command_key = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    mode = Column(String(12), nullable=False)
    status = Column(String(12), nullable=False)
    revision = Column(Integer, nullable=False)
    connection_revision = Column(Integer)
    upper_message_id = Column(BigInteger)
    after_fan_id = Column(BigInteger)
    after_message_id = Column(BigInteger)
    rows_scanned = Column(Integer, nullable=False)
    pages_read = Column(Integer, nullable=False)
    page_budget = Column(Integer, nullable=False)
    targets_total = Column(Integer, nullable=False)
    targets_done = Column(Integer, nullable=False)
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
    next_attempt_at = Column(DateTime)
    last_error = Column(String(64))
    requested_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime)


class OfOfferImportCursor(Base):
    """One chat's bounded refresh position; targets come only from known sources."""
    __tablename__ = "of_offer_import_cursor"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "job_id"], ["of_offer_import_job.account_id", "of_offer_import_job.job_id"], ondelete="RESTRICT"),
        CheckConstraint("status IN ('pending','complete','unresolved')", name="status"),
        CheckConstraint("fan_id > 0 AND pages_read >= 0 AND pages_read <= 200 AND attempts >= 0", name="bounds"),
        {"info": INFO},
    )
    account_id = Column(String, primary_key=True)
    job_id = Column(String(36), primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    targets_json = Column(Text, nullable=False)
    found_json = Column(Text, nullable=False)
    before_id = Column(BigInteger)
    pages_read = Column(Integer, nullable=False)
    attempts = Column(Integer, nullable=False)
    status = Column(String(12), nullable=False)
    outcome = Column(String(32))
    updated_at = Column(DateTime, nullable=False)


class OfOfferImportCandidate(Base):
    """Provisional unordered grouping for navigation. Never commercial authority."""
    __tablename__ = "of_offer_import_candidate"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "job_id"], ["of_offer_import_job.account_id", "of_offer_import_job.job_id"], ondelete="RESTRICT"),
        CheckConstraint("source_count > 0 AND revision > 0", name="bounds"),
        {"info": INFO},
    )
    account_id = Column(String, primary_key=True)
    job_id = Column(String(36), primary_key=True)
    media_key = Column(String(64), primary_key=True)
    media_json = Column(Text, nullable=False)
    source_count = Column(Integer, nullable=False)
    variants_json = Column(Text, nullable=False)
    samples_json = Column(Text, nullable=False)
    revision = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class OfOfferImportDecision(Base):
    __tablename__ = "of_offer_import_decision"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "observation_id"], ["of_offer_observation.account_id", "of_offer_observation.observation_id"], ondelete="RESTRICT"),
        CheckConstraint("action IN ('create','withdraw')", name="action"),
        CheckConstraint("action <> 'create' OR (reuse_confirmed AND description_approved)", name="approval"),
        Index("uq_of_offer_import_decision_bundle", "account_id", "binding_id", "media_key", unique=True,
              postgresql_where=text("action = 'create'")),
        Index("uq_of_offer_import_decision_withdraw", "account_id", "item_id", unique=True,
              postgresql_where=text("action = 'withdraw'")),
        Index("ix_of_offer_import_decision_item", "account_id", "item_id"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    command_key = Column(String(64), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    action = Column(String(12), nullable=False)
    request_hash = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    observation_id = Column(String(36), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    media_key = Column(String(64), nullable=False)
    reuse_confirmed = Column(Boolean, nullable=False)
    description_approved = Column(Boolean, nullable=False)
    item_id = Column(Integer, ForeignKey("catalog_items.id", ondelete="RESTRICT"), nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)


# ── Continuous import and two-way content sync (handoff section 15) ─────────
# Same unpublished 0089 revision: one native schema head.

SYNC_STATES = ("'queued','uploading','verifying','synced','retrying','connection_required',"
               "'needs_review','outcome_unconfirmed'")


class OfContentSyncAccount(Base):
    """Per-account automatic read sync: settings revision plus durable scheduler state."""
    __tablename__ = "of_content_sync_account"
    __table_args__ = (
        CheckConstraint("status IN ('off','active','paused_auth','blocked')", name="status"),
        CheckConstraint("revision > 0 AND backlog >= 0 AND failures >= 0", name="bounds"),
        CheckConstraint("(lease_token IS NULL) = (lease_until IS NULL)", name="lease"),
        Index("ix_of_content_sync_account_due", "status", "incremental_due_at"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    enabled = Column(Boolean, nullable=False)
    status = Column(String(12), nullable=False)
    revision = Column(Integer, nullable=False)
    actor = Column(String(128), nullable=False)
    command_key = Column(String(64), nullable=False)
    connection_revision = Column(Integer)
    initial_job_id = Column(String(36))
    incremental_due_at = Column(DateTime)
    reconcile_due_at = Column(DateTime)
    last_incremental_at = Column(DateTime)
    last_reconcile_at = Column(DateTime)
    last_full_reconcile_at = Column(DateTime)
    # Highest outgoing priced Message.message_id already folded into candidates.
    covered_through_message_id = Column(BigInteger)
    # Rotating daily reconciliation position over catalogue items.
    reconcile_after_item_id = Column(Integer)
    reconcile_cycle_started_at = Column(DateTime)
    backlog = Column(Integer, nullable=False)
    failures = Column(Integer, nullable=False)
    blocked_reason = Column(String(64))
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class OfContentSyncOperation(Base):
    """One scheduled pass, sync-now, upload or retry with its idempotent identity and outcome."""
    __tablename__ = "of_content_sync_operation"
    __table_args__ = (
        UniqueConstraint("account_id", "command_key", name="uq_of_content_sync_operation_command"),
        CheckConstraint("kind IN ('initial','incremental','reconcile','sync_now','upload','retry')", name="kind"),
        CheckConstraint("status IN ('queued','running','complete','partial','failed','cancelled','outcome_unconfirmed')", name="status"),
        CheckConstraint("(status = 'running') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)", name="lease"),
        Index("ix_of_content_sync_operation_recent", "account_id", "created_at"),
        Index("uq_of_content_sync_operation_active", "account_id", "kind", unique=True,
              postgresql_where=text("status IN ('queued','running') AND kind IN ('incremental','reconcile','sync_now')")),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    operation_id = Column(String(36), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    command_key = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    kind = Column(String(12), nullable=False)
    status = Column(String(20), nullable=False)
    connection_revision = Column(Integer)
    draft_id = Column(String(36))
    asset_id = Column(String(36))
    coverage_json = Column(Text, nullable=False)
    result_json = Column(Text, nullable=False)
    attempts = Column(Integer, nullable=False)
    last_error = Column(String(64))
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
    next_attempt_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime)


class OfContentSyncAsset(Base):
    """Local creator media -> provider media id on exactly one account binding."""
    __tablename__ = "of_content_sync_asset"
    __table_args__ = (
        UniqueConstraint("account_id", "binding_id", "content_hash", name="uq_of_content_sync_asset_hash"),
        UniqueConstraint("account_id", "upload_token", name="uq_of_content_sync_asset_token"),
        CheckConstraint("kind IN ('photo','video')", name="kind"),
        CheckConstraint(f"state IN ({SYNC_STATES})", name="state"),
        CheckConstraint("size_bytes > 0 AND attempts >= 0", name="bounds"),
        CheckConstraint("provider_media_id IS NULL OR provider_media_id > 0", name="provider"),
        CheckConstraint("state <> 'synced' OR (provider_media_id IS NOT NULL AND verified_at IS NOT NULL)", name="synced"),
        CheckConstraint("(lease_token IS NULL) = (lease_until IS NULL)", name="lease"),
        Index("uq_of_content_sync_asset_provider", "account_id", "binding_id", "provider_media_id", unique=True,
              postgresql_where=text("provider_media_id IS NOT NULL")),
        Index("ix_of_content_sync_asset_due", "state", "next_attempt_at"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    asset_id = Column(String(36), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    kind = Column(String(8), nullable=False)
    name = Column(String(240), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    content_hash = Column(String(64), nullable=False)
    # Where the staged bytes live (existing native media spool reference).
    staged_ref = Column(String(160))
    # Durable identity recorded before any provider write, so a lost
    # acknowledgement is reconciled by lookup instead of a blind re-upload.
    upload_token = Column(String(36), nullable=False)
    provider_media_id = Column(BigInteger)
    state = Column(String(20), nullable=False)
    attempts = Column(Integer, nullable=False)
    last_error = Column(String(64))
    next_attempt_at = Column(DateTime)
    lease_token = Column(String(36))
    lease_until = Column(DateTime)
    upload_started_at = Column(DateTime)
    verified_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class OfContentSyncDraft(Base):
    """Desired CRM fixed-price draft; becomes a disabled CatalogItem only when every asset is synced."""
    __tablename__ = "of_content_sync_draft"
    __table_args__ = (
        UniqueConstraint("account_id", "command_key", name="uq_of_content_sync_draft_command"),
        CheckConstraint(f"state IN ('draft',{SYNC_STATES})", name="state"),
        CheckConstraint("revision > 0 AND price_cents BETWEEN 300 AND 20000 AND currency = 'USD'", name="terms"),
        Index("ix_of_content_sync_draft_item", "account_id", "item_id"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    draft_id = Column(String(36), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    command_key = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    revision = Column(Integer, nullable=False)
    label = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    price_cents = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False)
    # Ordered local asset ids and the free-preview subset (asset ids).
    assets_json = Column(Text, nullable=False)
    preview_json = Column(Text, nullable=False)
    state = Column(String(20), nullable=False)
    reason = Column(String(64))
    item_id = Column(Integer, ForeignKey("catalog_items.id", ondelete="RESTRICT"))
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class OfContentSyncConflict(Base):
    """A verified change to an item's selected source; held for one explicit decision."""
    __tablename__ = "of_content_sync_conflict"
    __table_args__ = (
        UniqueConstraint("account_id", "item_id", "observation_id", name="uq_of_content_sync_conflict_source"),
        ForeignKeyConstraint(["account_id", "observation_id"], ["of_offer_observation.account_id", "of_offer_observation.observation_id"], ondelete="RESTRICT"),
        CheckConstraint("status IN ('open','resolved')", name="status"),
        CheckConstraint("resolution IS NULL OR resolution IN ('accept_source','keep_crm','disable')", name="resolution"),
        CheckConstraint("(status = 'resolved') = (resolution IS NOT NULL AND resolved_at IS NOT NULL)", name="resolved"),
        CheckConstraint("revision > 0", name="bounds"),
        Index("ix_of_content_sync_conflict_open", "account_id", "item_id", postgresql_where=text("status = 'open'")),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    conflict_id = Column(String(36), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    item_id = Column(Integer, ForeignKey("catalog_items.id", ondelete="RESTRICT"), nullable=False)
    kind = Column(String(16), nullable=False)
    prior_fingerprint = Column(String(64), nullable=False)
    observation_id = Column(String(36), nullable=False)
    status = Column(String(12), nullable=False)
    revision = Column(Integer, nullable=False)
    resolution = Column(String(16))
    resolved_by = Column(String(128))
    resolved_command_key = Column(String(64))
    resolved_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class OfContentSyncDecision(Base):
    """Append-only audit of sync settings, conflict resolutions and commercial edits."""
    __tablename__ = "of_content_sync_decision"
    __table_args__ = (
        CheckConstraint("action IN ('settings','resolve','save','retry')", name="action"),
        Index("ix_of_content_sync_decision_recent", "account_id", "created_at"),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    command_key = Column(String(64), primary_key=True)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    action = Column(String(12), nullable=False)
    request_hash = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    subject_id = Column(String(64))
    before_json = Column(Text, nullable=False)
    after_json = Column(Text, nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)
