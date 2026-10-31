"""Shared offline message delivery and native provenance; no live registration.

Queued and ambiguous attempts live here without fabricated provider message
identities. Accepted provider messages still use the canonical history and
OfMessageDelivery receipt projection. Media terms extend the same intent;
commerce evidence audits the existing canonical financial/ownership owners.
"""
from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey,
    Float, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint,
    false, text,
)

from .models import Base
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.ext.compiler import compiles


class _MediaShape(TextClause):
    """Same media invariant for production PostgreSQL and SQLite test stores."""
    inherit_cache = True


@compiles(_MediaShape, "sqlite")
def _sqlite_media_shape(element, compiler, **kw):
    predicate = "catalog_snapshot_json::jsonb @> \'{\"manual_vault\": true}\'::jsonb"
    return element.text.replace(predicate,
        "CASE WHEN json_valid(catalog_snapshot_json) THEN "
        "json_type(catalog_snapshot_json, \'$.manual_vault\') = \'true\' ELSE 0 END")


REVISION = "0072_onlyfans_text_delivery"
INFO = {"onlyfans_since": REVISION}


class OfNativePlan(Base):
    __tablename__ = "of_native_plan"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "fan_id"], ["fans.account_id", "fans.fan_id"], ondelete="RESTRICT"),
        UniqueConstraint("account_id", "fan_id", "input_revision", name="uq_of_native_plan_input"),
        CheckConstraint("status IN ('generating','ready','queued','stale','unknown','refused')", name="status"),
        Index("ix_of_native_plan_state", "account_id", "status", "created_at"),
        {"info": {"onlyfans_since": "0074_native_text_plans"}},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    plan_id = Column(String(36), primary_key=True)
    fan_id = Column(BigInteger, nullable=False)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    work_generation = Column(Integer, nullable=False)
    input_revision = Column(BigInteger, nullable=False)
    claim_json = Column(Text, nullable=False)
    context_json = Column(Text, nullable=False)
    context_hash = Column(String(64), nullable=False)
    planner_version = Column(String(32), nullable=False)
    model_id = Column(String(64), nullable=False)
    resolved_model_id = Column(String(128), info={"onlyfans_since": "0075_native_model_budget"})
    model_call_id = Column(Integer, ForeignKey("grok_calls.id", ondelete="RESTRICT"), info={"onlyfans_since": "0075_native_model_budget"})
    budget_day = Column(String(10), info={"onlyfans_since": "0075_native_model_budget"})
    billing_provider = Column(String(32), info={"onlyfans_since": "0075_native_model_budget"})
    reserved_millicents = Column(Integer, info={"onlyfans_since": "0075_native_model_budget"})
    charged_millicents = Column(Integer, info={"onlyfans_since": "0075_native_model_budget"})
    cost_settled_at = Column(DateTime, info={"onlyfans_since": "0075_native_model_budget"})
    status = Column(String(16), nullable=False)
    reason = Column(String(64))
    body = Column(Text)
    sale_json = Column(Text, info={"onlyfans_since": "0077_native_media_sales"})
    intent_id = Column(String(36))
    created_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class OfNativeMemoryChange(Base):
    """Append-only provenance; canonical values remain in Fan/FanProfile."""
    __tablename__ = "of_native_memory_change"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "fan_id"], ["fans.account_id", "fans.fan_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["account_id", "plan_id"], ["of_native_plan.account_id", "of_native_plan.plan_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["account_id", "fan_id", "source_message_id"], ["messages.account_id", "messages.fan_id", "messages.message_id"], ondelete="RESTRICT"),
        UniqueConstraint("account_id", "plan_id", "target", "field", name="uq_of_native_memory_plan_field"),
        CheckConstraint("target IN ('fan','profile')", name="target"),
        Index("ix_of_native_memory_fan", "account_id", "fan_id", "created_at"),
        {"info": {"onlyfans_since": "0076_native_memory_evidence"}},
    )
    account_id = Column(String, primary_key=True)
    change_id = Column(String(36), primary_key=True)
    fan_id = Column(BigInteger, nullable=False)
    plan_id = Column(String(36), nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    input_revision = Column(BigInteger, nullable=False)
    target = Column(String(16), nullable=False)
    field = Column(String(32), nullable=False)
    before_json = Column(Text, nullable=False)
    after_json = Column(Text, nullable=False)
    source_quote = Column(Text, nullable=False)
    context_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False)


class OfCommerceEvidence(Base):
    """Append-only intake provenance; Transaction/Message own current payment state."""
    __tablename__ = "of_commerce_evidence"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "intent_id"], ["of_text_intent.account_id", "of_text_intent.intent_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["account_id", "fan_id", "message_id"], ["messages.account_id", "messages.fan_id", "messages.message_id"], ondelete="RESTRICT"),
        CheckConstraint("kind IN ('purchase','refund','chargeback')", name="kind"),
        CheckConstraint("status IN ('applied','stale','conflict')", name="status"),
        CheckConstraint("provider_revision > 0", name="provider_revision"),
        Index("ix_of_commerce_event", "account_id", "event_key"),
        Index("ix_of_commerce_revision", "account_id", "provider_transaction_id", "provider_revision"),
        Index("ix_of_commerce_conflict", "account_id", "status", postgresql_where=text("status = 'conflict'")),
        {"info": {"onlyfans_since": "0077_native_media_sales"}},
    )
    account_id = Column(String, primary_key=True)
    evidence_hash = Column(String(64), primary_key=True)
    event_key = Column(String(64), nullable=False)
    provider_transaction_id = Column(String(64), nullable=False)
    provider_revision = Column(BigInteger, nullable=False)
    fan_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    intent_id = Column(String(36), nullable=False)
    kind = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False)
    transaction_id = Column(Integer, ForeignKey("transactions.id", ondelete="RESTRICT"))
    occurred_at = Column(DateTime, nullable=False)
    recorded_at = Column(DateTime, nullable=False)
    evidence_ref = Column(String(256), nullable=False)
    payload_json = Column(Text, nullable=False)


class OfDeliveryPolicy(Base):
    __tablename__ = "of_delivery_policy"
    __table_args__ = (
        CheckConstraint("transport_kind IN ('synthetic','native')", name="transport_kind"),
        CheckConstraint("limit_units >= 0 AND limit_units <= 10000", name="limit_units"),
        CheckConstraint("used_units >= 0", name="used_units"),
        CheckConstraint("queued_count >= 0 AND queued_count <= 1000", name="queued_count"),
        CheckConstraint("throttle_seconds >= 1 AND throttle_seconds <= 86400", name="throttle_seconds"),
        CheckConstraint("daily_limit_units IS NULL OR (daily_limit_units >= 0 AND daily_limit_units <= 20000)",
                        name="daily_limit_units", info={"onlyfans_since": "0102_daily_send_cap"}),
        CheckConstraint("delivery_mode IN ('gap','concurrent')", name="delivery_mode",
                        info={"onlyfans_since": "0103_concurrent_delivery"}),
        CheckConstraint("provider_requests_per_second > 0 AND provider_requests_per_second <= 100",
                        name="provider_requests_per_second", info={"onlyfans_since": "0103_concurrent_delivery"}),
        CheckConstraint("provider_burst >= 1 AND provider_burst <= 200", name="provider_burst",
                        info={"onlyfans_since": "0103_concurrent_delivery"}),
        CheckConstraint("provider_max_concurrent >= 1 AND provider_max_concurrent <= 64",
                        name="provider_max_concurrent", info={"onlyfans_since": "0103_concurrent_delivery"}),
        CheckConstraint("conversation_min_gap_seconds >= 0 AND conversation_min_gap_seconds <= 3600",
                        name="conversation_min_gap_seconds", info={"onlyfans_since": "0103_concurrent_delivery"}),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    revision = Column(Integer, nullable=False, server_default=text("0"))
    enabled = Column(Boolean, nullable=False, server_default=false())
    transport_kind = Column(String(16), nullable=False, server_default=text("'synthetic'"))
    limit_units = Column(Integer, nullable=False, server_default=text("0"))
    used_units = Column(Integer, nullable=False, server_default=text("0"))
    queued_count = Column(Integer, nullable=False, server_default=text("0"))
    throttle_seconds = Column(Integer, nullable=False, server_default=text("1"))
    next_dispatch_at = Column(DateTime)
    active_intent_id = Column(String(36))
    media_enabled = Column(Boolean, nullable=False, server_default=false(),
                           info={"onlyfans_since": "0077_native_media_sales"})
    # Optional per-account DAILY send cap (rolling 24 hours of the FASTT engine's receipts). NULL = no daily cap, which
    # is exactly the behaviour before this column existed. It sits beside, and never replaces, the lifetime limit_units.
    daily_limit_units = Column(Integer, nullable=True, info={"onlyfans_since": "0102_daily_send_cap"})
    # Concurrent mode is an account-specific opt-in. "gap" keeps the old single-send gate and throttle;
    # "concurrent" uses the provider token bucket, bounded concurrency and per-fan ownership below.
    delivery_mode = Column(String(16), nullable=False, server_default=text("'gap'"),
                           info={"onlyfans_since": "0103_concurrent_delivery"})
    provider_requests_per_second = Column(Float, nullable=False, server_default=text("1.0"),
                                          info={"onlyfans_since": "0103_concurrent_delivery"})
    provider_burst = Column(Integer, nullable=False, server_default=text("5"),
                            info={"onlyfans_since": "0103_concurrent_delivery"})
    provider_max_concurrent = Column(Integer, nullable=False, server_default=text("4"),
                                     info={"onlyfans_since": "0103_concurrent_delivery"})
    conversation_min_gap_seconds = Column(Integer, nullable=False, server_default=text("0"),
                                          info={"onlyfans_since": "0103_concurrent_delivery"})
    provider_cooldown_until = Column(DateTime, nullable=True, info={"onlyfans_since": "0103_concurrent_delivery"})


class OfDeliveryPolicyChange(Base):
    __tablename__ = "of_delivery_policy_change"
    __table_args__ = ({"info": INFO},)
    account_id = Column(String, ForeignKey("of_delivery_policy.account_id", ondelete="RESTRICT"), primary_key=True)
    revision = Column(Integer, primary_key=True)
    actor = Column(String(128), nullable=False)
    reason = Column(String(512), nullable=False)
    before_json = Column(Text, nullable=False)
    after_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)


class OfTextIntent(Base):
    __tablename__ = "of_text_intent"
    __table_args__ = (
        ForeignKeyConstraint(["account_id", "native_plan_id"], ["of_native_plan.account_id", "of_native_plan.plan_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["account_id", "fan_id"], ["fans.account_id", "fans.fan_id"], ondelete="RESTRICT"),
        UniqueConstraint("account_id", "command_key", name="uq_of_text_intent_command"),
        UniqueConstraint("account_id", "attempt_id", name="uq_of_text_intent_attempt"),
        UniqueConstraint("account_id", "provider_message_id", name="uq_of_text_intent_provider_message"),
        CheckConstraint("origin IN ('automated','manual','campaign','engagement')", name="origin"),
        CheckConstraint("status IN ('queued','dispatching','accepted','not_accepted','unknown','cancelled','refused')", name="status"),
        CheckConstraint("provider_message_id IS NULL OR provider_message_id > 0", name="provider_message_id"),
        CheckConstraint(_MediaShape("(catalog_item_id IS NULL AND media_json = '[]' AND preview_json = '[]' AND price_cents = 0 AND catalog_snapshot_json IS NULL AND offer_id IS NULL) OR (catalog_item_id IS NOT NULL AND media_json <> '[]' AND catalog_snapshot_json IS NOT NULL AND price_cents >= 300 AND price_cents <= 20000) OR (origin = 'manual' AND catalog_item_id IS NOT NULL AND media_json <> '[]' AND preview_json = '[]' AND catalog_snapshot_json IS NOT NULL AND price_cents = 0 AND offer_id IS NULL AND catalog_snapshot_json::jsonb @> '{\"manual_vault\": true}'::jsonb)"),
                        name="media_shape", info={"onlyfans_since": "0077_native_media_sales"}),
        Index("ix_of_text_intent_queued", "account_id", "status", "created_at", "intent_id"),
        Index("ix_of_text_intent_unresolved", "account_id", "status", "deadline",
              postgresql_where=text("status IN ('dispatching','unknown')")),
        Index("uq_of_text_intent_offer", "offer_id", unique=True,
              postgresql_where=text("offer_id IS NOT NULL"), info={"onlyfans_since": "0077_native_media_sales"}),
        {"info": INFO},
    )
    account_id = Column(String, ForeignKey("of_runtime.account_id", ondelete="RESTRICT"), primary_key=True)
    intent_id = Column(String(36), primary_key=True)
    fan_id = Column(BigInteger, nullable=False)
    binding_id = Column(String, ForeignKey("of_creator_bindings.id", ondelete="RESTRICT"), nullable=False)
    command_key = Column(String(64), nullable=False)
    actor = Column(String(128), nullable=False)
    origin = Column(String(16), nullable=False)
    body = Column(Text, nullable=False)
    command_hash = Column(String(64), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    control_revision = Column(Integer, nullable=False)
    input_revision = Column(BigInteger, nullable=False)
    policy_revision = Column(Integer, nullable=False)
    work_generation = Column(Integer)
    work_token = Column(String(36))
    native_plan_id = Column(String(36), info={"onlyfans_since": "0074_native_text_plans"})
    media_json = Column(Text, nullable=False, server_default=text("'[]'"), info={"onlyfans_since": "0077_native_media_sales"})
    preview_json = Column(Text, nullable=False, server_default=text("'[]'"), info={"onlyfans_since": "0077_native_media_sales"})
    price_cents = Column(Integer, nullable=False, server_default=text("0"), info={"onlyfans_since": "0077_native_media_sales"})
    catalog_item_id = Column(Integer, ForeignKey("catalog_items.id", ondelete="RESTRICT"), info={"onlyfans_since": "0077_native_media_sales"})
    catalog_snapshot_json = Column(Text, info={"onlyfans_since": "0077_native_media_sales"})
    offer_id = Column(Integer, ForeignKey("content_offers.id", ondelete="RESTRICT"), info={"onlyfans_since": "0077_native_media_sales"})
    status = Column(String(24), nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempt_id = Column(String(36))
    dispatch_token = Column(String(36))
    dispatched_at = Column(DateTime)
    deadline = Column(DateTime)
    wire_version = Column(Integer, nullable=False, server_default=text("0"),
                          info={"onlyfans_since": "0073_one_use_egress"})
    wire_started_at = Column(DateTime, info={"onlyfans_since": "0073_one_use_egress"})
    provider_message_id = Column(BigInteger)
    receipt_json = Column(Text)
    evidence_ref = Column(String(256))
    reason = Column(String(128))
    projected_at = Column(DateTime)
    reservation_held = Column(Boolean, nullable=False, server_default=false())
