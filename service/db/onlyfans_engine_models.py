"""Durable decision records around the existing message and delivery owners."""
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey,
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
        UniqueConstraint('account_id', 'request_id', name='uq_engine_decision_request'),
        UniqueConstraint('account_id', 'intent_id', name='uq_engine_decision_intent'),
        CheckConstraint("status IN ('ready','queued','unknown','completed','cancelled','held')", name='status'),
        CheckConstraint("action IN ('reply','offer')", name='action'),
        Index('ix_engine_decision_status', 'account_id', 'fan_id', 'status'),
        {'info': INFO},
    )
    account_id = Column(String, primary_key=True)
    decision_id = Column(String(36), primary_key=True)
    request_id = Column(String(36), nullable=False)
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
