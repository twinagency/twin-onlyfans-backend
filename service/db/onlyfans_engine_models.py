"""Durable decision records around the existing message and delivery owners."""
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey,
                        ForeignKeyConstraint, Index, Integer, LargeBinary, String, Text, UniqueConstraint, text)
from .models import Base

REVISION = '0113_conversation_engine'
INFO = {'onlyfans_since': REVISION}


class OfEngineOwner(Base):
    __tablename__ = 'of_engine_owner'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'fan_id'], ['fans.account_id', 'fans.fan_id'], ondelete='RESTRICT'),
        ForeignKeyConstraint(['account_id', 'fan_id', 'sales_source_message_id'], ['messages.account_id', 'messages.fan_id', 'messages.message_id'], ondelete='RESTRICT'),
        CheckConstraint("mode IN ('active','paused','human')", name='mode'),
        CheckConstraint('epoch > 0 AND revision >= 0', name='revision'),
        CheckConstraint("sales_mode IN ('open','paused','reopened_scoped')", name='sales_mode'),
        {'info': INFO},
    )
    account_id = Column(String, ForeignKey('of_runtime.account_id', ondelete='RESTRICT'), primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    binding_id = Column(String, ForeignKey('of_creator_bindings.id', ondelete='RESTRICT'), nullable=False)
    mode = Column(String(16), nullable=False)
    epoch = Column(BigInteger, nullable=False)
    revision = Column(Integer, nullable=False)
    sales_mode = Column(String(24), nullable=False)
    sales_scope_json = Column(Text, nullable=False)
    sales_source_message_id = Column(BigInteger)
    updated_at = Column(DateTime, nullable=False)


class OfEngineRequest(Base):
    __tablename__ = 'of_engine_request'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'fan_id'], ['of_engine_owner.account_id', 'of_engine_owner.fan_id'], ondelete='RESTRICT'),
        ForeignKeyConstraint(['account_id', 'fan_id', 'source_message_id'], ['messages.account_id', 'messages.fan_id', 'messages.message_id'], ondelete='RESTRICT'),
        UniqueConstraint('account_id', 'fan_id', 'source_message_id', name='uq_engine_request_source'),
        CheckConstraint("status IN ('open','waiting_fan','waiting_provider','handover_pending','handover_accepted','completed','declined','cancelled')", name='status'),
        CheckConstraint('revision >= 0', name='revision'),
        Index('ix_engine_request_open', 'account_id', 'fan_id', 'status'),
        {'info': INFO},
    )
    account_id = Column(String, primary_key=True)
    request_id = Column(String(36), primary_key=True)
    fan_id = Column(BigInteger, nullable=False)
    binding_id = Column(String, ForeignKey('of_creator_bindings.id', ondelete='RESTRICT'), nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    status = Column(String(24), nullable=False)
    owner = Column(String(128), nullable=False)
    next_step = Column(String(128), nullable=False)
    handover_target = Column(String(128))
    accepted_by = Column(String(128))
    revision = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class OfEngineDecision(Base):
    __tablename__ = 'of_engine_decision'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'fan_id'], ['of_engine_owner.account_id', 'of_engine_owner.fan_id'], ondelete='RESTRICT'),
        ForeignKeyConstraint(['account_id', 'request_id'], ['of_engine_request.account_id', 'of_engine_request.request_id'], ondelete='RESTRICT'),
        ForeignKeyConstraint(['account_id', 'intent_id'], ['of_text_intent.account_id', 'of_text_intent.intent_id'], ondelete='RESTRICT'),
        ForeignKeyConstraint(['account_id', 'offer_grant_id'], ['of_engine_offer_grant.account_id', 'of_engine_offer_grant.grant_id'], ondelete='RESTRICT'),
        UniqueConstraint('account_id', 'request_id', 'generation', 'part_index', name='uq_engine_decision_request_generation_part',
                         info={'onlyfans_since': '0121_engine_generations'}),
        CheckConstraint('generation >= 0', name='generation', info={'onlyfans_since': '0121_engine_generations'}),
        UniqueConstraint('account_id', 'intent_id', name='uq_engine_decision_intent'),
        CheckConstraint("status IN ('ready','queued','unknown','completed','cancelled','held')", name='status'),
        CheckConstraint("action IN ('reply','offer')", name='action'),
        CheckConstraint('part_index BETWEEN 0 AND 3', name='part_index', info={'onlyfans_since': '0117_engine_message_parts'}),
        Index('ix_engine_decision_status', 'account_id', 'fan_id', 'status'),
        {'info': INFO},
    )
    account_id = Column(String, primary_key=True)
    decision_id = Column(String(36), primary_key=True)
    request_id = Column(String(36), nullable=False)
    part_index = Column(Integer, nullable=False, server_default=text('0'), info={'onlyfans_since': '0117_engine_message_parts'})
    generation = Column(Integer, nullable=False, server_default=text('0'), info={'onlyfans_since': '0121_engine_generations'})
    fan_id = Column(BigInteger, nullable=False)
    binding_id = Column(String, ForeignKey('of_creator_bindings.id', ondelete='RESTRICT'), nullable=False)
    owner_epoch = Column(BigInteger, nullable=False)
    connection_epoch = Column(BigInteger)
    input_revision = Column(BigInteger, nullable=False)
    control_revision = Column(Integer, nullable=False)
    action = Column(String(16), nullable=False)
    offer_grant_id = Column(String(36))
    catalog_item_id = Column(Integer)
    terms_json = Column(Text)
    body = Column(Text, nullable=False)
    payload_hash = Column(String(64), nullable=False)
    snapshot_hash = Column(String(64), nullable=False)
    intent_id = Column(String(36))
    status = Column(String(16), nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class OfFansConnection(Base):
    __tablename__ = 'of_fans_connection'
    __table_args__ = (
        Index('uq_fans_provider_account_active', 'provider_account_id', unique=True,
              postgresql_where=text("status = 'active'"), sqlite_where=text("status = 'active'")),
        CheckConstraint("status IN ('active','retired') AND epoch > 0", name='status'),
        Index('uq_fans_connection_active', 'account_id', unique=True,
              postgresql_where=text("status = 'active'")),
        {'info': INFO},
    )
    account_id = Column(String, ForeignKey('of_runtime.account_id', ondelete='RESTRICT'), primary_key=True)
    epoch = Column(BigInteger, primary_key=True)
    binding_id = Column(String, ForeignKey('of_creator_bindings.id', ondelete='RESTRICT'), nullable=False)
    provider_account_id = Column(String(255), nullable=False)
    provider_creator_id = Column(String(255), nullable=False)
    status = Column(String(16), nullable=False)
    created_at = Column(DateTime, nullable=False)


class OfFansWebhook(Base):
    __tablename__ = 'of_fans_webhook'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'epoch'], ['of_fans_connection.account_id', 'of_fans_connection.epoch'], ondelete='RESTRICT'),
        CheckConstraint("status IN ('accepted','quarantined')", name='status'),
        {'info': INFO},
    )
    account_id = Column(String, primary_key=True)
    epoch = Column(BigInteger, primary_key=True)
    event_key = Column(String(255), primary_key=True)
    event_type = Column(String(80), nullable=False)
    payload_sha256 = Column(String(64), nullable=False)
    raw_body = Column(LargeBinary, nullable=False)
    status = Column(String(16), nullable=False)
    received_at = Column(DateTime, nullable=False)


class OfEnginePilotWebhookDigest(Base):
    """Hash-only evidence for authenticated events outside the active pilot scope."""
    __tablename__ = 'of_engine_pilot_webhook_digest'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'epoch'], ['of_fans_connection.account_id', 'of_fans_connection.epoch'], ondelete='RESTRICT'),
        {'info': {'onlyfans_since': '0125_plan_c_scope'}},
    )
    account_id = Column(String, primary_key=True)
    epoch = Column(BigInteger, primary_key=True)
    event_key = Column(String(255), primary_key=True)
    payload_sha256 = Column(String(64), primary_key=True)
    received_at = Column(DateTime, nullable=False)


class OfEngineAttempt(Base):
    __tablename__ = 'of_engine_attempt'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'intent_id'], ['of_text_intent.account_id', 'of_text_intent.intent_id'], ondelete='RESTRICT'),
        UniqueConstraint('account_id', 'intent_id', 'attempt_id', name='uq_engine_attempt_identity'),
        CheckConstraint("outcome IN ('dispatching','unknown','accepted','not_accepted')", name='outcome'),
        Index('ix_engine_attempt_intent', 'account_id', 'intent_id'),
        {'info': INFO},
    )
    account_id = Column(String, primary_key=True)
    attempt_id = Column(String(36), primary_key=True)
    intent_id = Column(String(36), nullable=False)
    outcome = Column(String(16), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    evidence_json = Column(Text)
    started_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class OfFansWebhookConflict(Base):
    __tablename__ = 'of_fans_webhook_conflict'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'epoch', 'event_key'],
            ['of_fans_webhook.account_id', 'of_fans_webhook.epoch', 'of_fans_webhook.event_key'], ondelete='RESTRICT'),
        {'info': INFO},
    )
    account_id = Column(String, primary_key=True)
    epoch = Column(BigInteger, primary_key=True)
    event_key = Column(String(255), primary_key=True)
    payload_sha256 = Column(String(64), primary_key=True)
    raw_body = Column(LargeBinary, nullable=False)
    received_at = Column(DateTime, nullable=False)


class OfEngineOfferGrant(Base):
    __tablename__ = 'of_engine_offer_grant'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'fan_id'], ['of_engine_owner.account_id', 'of_engine_owner.fan_id'], ondelete='RESTRICT'),
        ForeignKeyConstraint(['account_id', 'used_intent_id'], ['of_text_intent.account_id', 'of_text_intent.intent_id'], ondelete='RESTRICT'),
        UniqueConstraint('account_id', 'fan_id', 'item_id', 'terms_hash', name='uq_engine_offer_grant_terms'),
        {'info': INFO},
    )
    account_id = Column(String, primary_key=True)
    grant_id = Column(String(36), primary_key=True)
    fan_id = Column(BigInteger, nullable=False)
    binding_id = Column(String, ForeignKey('of_creator_bindings.id', ondelete='RESTRICT'), nullable=False)
    item_id = Column(Integer, ForeignKey('catalog_items.id', ondelete='RESTRICT'), nullable=False)
    terms_hash = Column(String(64), nullable=False)
    coverage_ref = Column(String(128), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_intent_id = Column(String(36))
    created_at = Column(DateTime, nullable=False)


class OfEngineAccessEvidence(Base):
    __tablename__ = 'of_engine_access_evidence'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'intent_id'], ['of_text_intent.account_id', 'of_text_intent.intent_id'], ondelete='RESTRICT'),
        CheckConstraint("status IN ('confirmed','revoked')", name='status'),
        {'info': INFO},
    )
    account_id = Column(String, primary_key=True)
    evidence_key = Column(String(128), primary_key=True)
    intent_id = Column(String(36), nullable=False)
    fan_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    media_json = Column(Text, nullable=False)
    status = Column(String(16), nullable=False)
    applied = Column(Boolean, nullable=False)
    evidence_ref = Column(String(128), nullable=False)
    occurred_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False)


# Revision 0114: a proof is derived by trusted code from a canonical source;
# the model cannot assert that its own answer is independent.
DEPENDENCY_INFO = {'onlyfans_since': '0114_independent_replies'}


class OfEngineIndependenceProof(Base):
    __tablename__ = 'of_engine_independence_proof'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'decision_id'], ['of_engine_decision.account_id', 'of_engine_decision.decision_id'], ondelete='RESTRICT'),
        ForeignKeyConstraint(['account_id', 'anchor_intent_id'], ['of_text_intent.account_id', 'of_text_intent.intent_id'], ondelete='RESTRICT'),
        ForeignKeyConstraint(['account_id', 'fan_id', 'source_message_id'], ['messages.account_id', 'messages.fan_id', 'messages.message_id'], ondelete='RESTRICT'),
        CheckConstraint("kind IN ('arithmetic.v1','identity.v1')", name='kind'),
        {'info': DEPENDENCY_INFO},
    )
    account_id = Column(String, primary_key=True)
    decision_id = Column(String(36), primary_key=True)
    fan_id = Column(BigInteger, nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    anchor_intent_id = Column(String(36), nullable=False)
    kind = Column(String(32), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    answer_sha256 = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False)


class OfEngineIndependentAdmission(Base):
    __tablename__ = 'of_engine_independent_admission'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'intent_id'], ['of_text_intent.account_id', 'of_text_intent.intent_id'], ondelete='RESTRICT'),
        ForeignKeyConstraint(['account_id', 'anchor_intent_id'], ['of_text_intent.account_id', 'of_text_intent.intent_id'], ondelete='RESTRICT'),
        CheckConstraint("status IN ('dispatching','unknown','settled')", name='status'),
        Index('uq_engine_independent_active', 'account_id', unique=True,
              postgresql_where=text("status IN ('dispatching','unknown')"),
              sqlite_where=text("status IN ('dispatching','unknown')")),
        {'info': DEPENDENCY_INFO},
    )
    account_id = Column(String, primary_key=True)
    attempt_id = Column(String(36), primary_key=True)
    intent_id = Column(String(36), nullable=False)
    anchor_intent_id = Column(String(36), nullable=False)
    fan_id = Column(BigInteger, nullable=False)
    status = Column(String(16), nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


MEMORY_INFO = {'onlyfans_since': '0115_engine_memory'}


class OfEngineMemoryBinding(Base):
    __tablename__ = 'of_engine_memory_binding'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'decision_id'], ['of_engine_decision.account_id', 'of_engine_decision.decision_id'], ondelete='RESTRICT'),
        ForeignKeyConstraint(['account_id', 'fan_id', 'source_message_id'], ['messages.account_id', 'messages.fan_id', 'messages.message_id'], ondelete='RESTRICT'),
        CheckConstraint("status IN ('current','unavailable')", name='status'),
        {'info': MEMORY_INFO},
    )
    account_id = Column(String, primary_key=True)
    decision_id = Column(String(36), primary_key=True)
    fan_id = Column(BigInteger, nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    binding_id = Column(String, ForeignKey('of_creator_bindings.id', ondelete='RESTRICT'), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    offer_fingerprint = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False)


CATALOGUE_INFO = {'onlyfans_since': '0116_engine_catalogue'}


class OfEngineCatalogueAlias(Base):
    __tablename__ = 'of_engine_catalogue_alias'
    __table_args__ = (
        UniqueConstraint('account_id', 'binding_id', 'item_id', 'normalized', 'content_hash', name='uq_engine_catalogue_alias_version'),
        Index('ix_engine_catalogue_alias_lookup', 'account_id', 'binding_id', 'normalized'),
        {'info': CATALOGUE_INFO},
    )
    account_id = Column(String, primary_key=True)
    alias_id = Column(String(36), primary_key=True)
    binding_id = Column(String, ForeignKey('of_creator_bindings.id', ondelete='RESTRICT'), nullable=False)
    item_id = Column(Integer, ForeignKey('catalog_items.id', ondelete='RESTRICT'), nullable=False)
    alias = Column(String(128), nullable=False)
    normalized = Column(String(128), nullable=False)
    content_hash = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    created_at = Column(DateTime, nullable=False)


OPERATOR_INFO = {'onlyfans_since': '0118_engine_operator'}


class OfEngineOperatorCommand(Base):
    __tablename__ = 'of_engine_operator_command'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'request_id'], ['of_engine_request.account_id', 'of_engine_request.request_id'], ondelete='RESTRICT'),
        CheckConstraint("action IN ('request_handover','accept_handover','complete','decline')", name='action'),
        {'info': OPERATOR_INFO},
    )
    account_id = Column(String, primary_key=True)
    command_key = Column(String(64), primary_key=True)
    request_id = Column(String(36), nullable=False)
    action = Column(String(24), nullable=False)
    actor = Column(String(128), nullable=False)
    expected_revision = Column(Integer, nullable=False)
    target = Column(String(128))
    evidence_ref = Column(String(128))
    request_hash = Column(String(64), nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)


MODEL_CALL_INFO = {'onlyfans_since': '0123_engine_model_calls'}


class OfEngineModelCall(Base):
    """One model invocation attempt: reservation, lease, settlement (see conversation_engine/model_ledger.py)."""
    __tablename__ = 'of_engine_model_call'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'request_id'], ['of_engine_request.account_id', 'of_engine_request.request_id'], ondelete='RESTRICT'),
        UniqueConstraint('account_id', 'request_id', 'generation', 'seq', name='uq_engine_model_call_request_seq'),
        CheckConstraint("status IN ('reserved','calling','settled','released','unknown')", name='status'),
        CheckConstraint('reserved_microusd >= 0 AND (actual_microusd IS NULL OR actual_microusd >= 0) AND generation >= 0 AND seq >= 0 AND chain_pos >= 0 AND attempt >= 1', name='amounts'),
        CheckConstraint("status <> 'settled' OR (usage_known AND actual_microusd IS NOT NULL)", name='settled'),
        Index('ix_engine_model_call_day', 'cost_day', 'account_id'),
        Index('ix_engine_model_call_open', 'account_id', 'request_id', 'generation', 'status'),
        {'info': MODEL_CALL_INFO},
    )
    account_id = Column(String, primary_key=True)
    call_id = Column(String(36), primary_key=True)
    request_id = Column(String(36), nullable=False)
    generation = Column(Integer, nullable=False)
    seq = Column(Integer, nullable=False)
    chain_pos = Column(Integer, nullable=False)
    chain_step = Column(String(16), nullable=False)
    fan_id = Column(BigInteger, nullable=False)
    binding_id = Column(String, ForeignKey('of_creator_bindings.id', ondelete='RESTRICT'), nullable=False)
    owner_epoch = Column(BigInteger, nullable=False)
    provider = Column(String(32), nullable=False)
    model = Column(String(128), nullable=False)
    prefixed = Column(Boolean, nullable=False)
    status = Column(String(16), nullable=False)
    outcome = Column(String(24))
    reserved_microusd = Column(BigInteger, nullable=False)
    actual_microusd = Column(BigInteger)
    usage_known = Column(Boolean, nullable=False, server_default=text('false'))
    prompt_tokens = Column(Integer)
    completion_tokens = Column(Integer)
    lease_owner = Column(String(64), nullable=False)
    lease_expires_at = Column(DateTime, nullable=False)
    attempt = Column(Integer, nullable=False, server_default=text('1'))
    snapshot_sha256 = Column(String(64), nullable=False)
    prompt_sha256 = Column(String(64), nullable=False)
    pilot_scope_revision = Column(Integer)
    pilot_scope_fingerprint = Column(String(64))
    response_text = Column(Text)
    response_sha256 = Column(String(64))
    error_class = Column(String(64))
    cost_day = Column(Date, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    wire_started_at = Column(DateTime)
    settled_at = Column(DateTime)


APPROVAL_INFO = {'onlyfans_since': '0124_engine_approval'}


class OfEngineApproval(Base):
    """One immutable approval version of a drafted reply/offer sequence (see conversation_engine/approval.py).

    Content (parts_json, draft_hash and the bound state) never changes after insert; an edit inserts the next
    version and invalidates the prior one. Only status, decision fields and expiry move forward.
    """
    __tablename__ = 'of_engine_approval'
    __table_args__ = (
        ForeignKeyConstraint(['account_id', 'request_id'], ['of_engine_request.account_id', 'of_engine_request.request_id'], ondelete='RESTRICT'),
        CheckConstraint("status IN ('awaiting','approved','rejected','invalidated','expired','consumed')", name='status'),
        CheckConstraint('version >= 1 AND generation >= 0 AND owner_epoch > 0 AND control_revision >= 0', name='numbers'),
        CheckConstraint("status NOT IN ('approved','consumed') OR (actor IS NOT NULL AND decided_at IS NOT NULL)", name='decided'),
        CheckConstraint("status NOT IN ('rejected','invalidated','expired') OR invalidation_reason IS NOT NULL", name='reason'),
        Index('uq_engine_approval_live', 'account_id', 'request_id', 'generation', unique=True,
              postgresql_where=text("status IN ('awaiting','approved')")),
        Index('ix_engine_approval_fan', 'account_id', 'fan_id', 'status'),
        {'info': APPROVAL_INFO},
    )
    account_id = Column(String, primary_key=True)
    request_id = Column(String(36), primary_key=True)
    generation = Column(Integer, primary_key=True)
    version = Column(Integer, primary_key=True)
    fan_id = Column(BigInteger, nullable=False)
    binding_id = Column(String, ForeignKey('of_creator_bindings.id', ondelete='RESTRICT'), nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    owner_epoch = Column(BigInteger, nullable=False)
    control_revision = Column(Integer, nullable=False)
    input_revision = Column(BigInteger, nullable=False)
    connection_epoch = Column(BigInteger)
    status = Column(String(16), nullable=False)
    parts_json = Column(Text, nullable=False)
    draft_hash = Column(String(64), nullable=False)
    authorization_mode = Column(String(16), nullable=False, server_default=text("'supervised'"))
    created_by = Column(String(128), nullable=False)
    actor = Column(String(128))
    decided_at = Column(DateTime)
    expires_at = Column(DateTime, nullable=False)
    invalidation_reason = Column(String(64))
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


PILOT_SCOPE_INFO = {'onlyfans_since': '0125_plan_c_scope'}


class OfEnginePilotScope(Base):
    """One durable pilot configuration and conservative provider-request counters.

    Canonical sends and financial evidence remain in their existing owners.
    """
    __tablename__ = 'of_engine_pilot_scope'
    __table_args__ = (
        CheckConstraint('scope_id = 1', name='singleton'),
        CheckConstraint("status IN ('configured','active','stopped')", name='status'),
        CheckConstraint("(status = 'active') = (active_since IS NOT NULL)", name='active_since'),
        CheckConstraint('revision > 0 AND provider_requests_used >= 0 AND provider_credits_reserved >= 0', name='numbers'),
        UniqueConstraint('account_id', name='uq_engine_pilot_scope_account'),
        {'info': PILOT_SCOPE_INFO},
    )
    scope_id = Column(Integer, primary_key=True)
    account_id = Column(String, ForeignKey('of_runtime.account_id', ondelete='RESTRICT'), nullable=False)
    binding_id = Column(String, ForeignKey('of_creator_bindings.id', ondelete='RESTRICT'), nullable=False)
    revision = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False)
    active_since = Column(DateTime)
    config_json = Column(Text, nullable=False)
    fingerprint = Column(String(64), nullable=False)
    provider_requests_used = Column(BigInteger, nullable=False)
    provider_credits_reserved = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
