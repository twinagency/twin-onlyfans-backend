"""Native FASTT pilot storage: Twin bridge control tables, writer markers, admission ledger.

These nine tables (plus the runner-owned ``native_pilot_followups`` queue) are what ``native_pilot.ledger`` and ``twin_bridge.coordination`` read
and write. They are deliberately NOT part of the application's shared metadata
(``db.models.Base``), its alembic chain or ``db.schema.check_schema``: the live
application schema stays at revision 0091 so the currently deployed api/worker images
(which require exactly 0091 and only complain about MISSING tables) still start and
rollback stays possible.

* PostgreSQL: the tables live in their own schema, ``fastt_runtime``. ``ensure_tables``
  creates the schema and the tables idempotently (called by the pilot runner / control /
  bridge entry points, never by the app's API/worker start-up). Raw SQL in
  ``twin_bridge.store`` reaches them through a per-connection ``search_path`` of
  ``fastt_runtime, public`` so unqualified names keep working and public tables
  (messages, catalog_items, of_* ...) are still found unchanged. ORM models carry the
  schema explicitly.
* SQLite: no schema; the same table names in the same file as before.

Timestamps in the three ``native_pilot_*`` tables and ``twin_bridge_writes`` are TEXT
holding naive-UTC ISO strings the ledger compares lexically (SQLite stores them that
way), so both backends order and hash identically.
"""
from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, Index, Integer, MetaData, String, Text,
    UniqueConstraint, text, true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .models import NAMING_CONVENTION, _ts_now

SCHEMA_NAME = "fastt_runtime"
# Held for the duration of schema/table creation so two starting processes cannot race.
DDL_LOCK_KEY = 7811093


def _is_postgres_env() -> bool:
    """Same rule as ``db.engine``: any DATABASE_URL naming postgres is PostgreSQL."""
    return os.environ.get("DATABASE_URL", "").startswith("postgres")


# Resolved once at import (the process's DATABASE_URL does not change afterwards).
SCHEMA = SCHEMA_NAME if _is_postgres_env() else None

metadata = MetaData(naming_convention=NAMING_CONVENTION, schema=SCHEMA)


class Base(DeclarativeBase):
    metadata = metadata


class NativeReplySchedule(Base):
    """One retained target per unanswered native burst and authority generation."""
    __tablename__ = "native_reply_schedules"
    schedule_id = Column(Text, primary_key=True)
    account_id = Column(Text, nullable=False)
    fan_id = Column(BigInteger, nullable=False)
    generation = Column(Text, nullable=False)
    anchor_id = Column(BigInteger, nullable=False)
    first_received_at = Column(Text, nullable=False)
    latest_received_at = Column(Text, nullable=False)
    provider_created_at = Column(Text, nullable=False)
    delay_seconds = Column(Float, nullable=False)
    target_send_at = Column(Text, nullable=False)
    pipeline_allowance_s = Column(Float, nullable=False)
    admissions = Column(Integer, nullable=False, server_default=text("0"))
    state = Column(Text, nullable=False)
    input_key = Column(Text)
    created_at = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)
    __table_args__ = (Index("native_reply_one_pending", "account_id", "fan_id", "generation", unique=True,
        postgresql_where=text("state IN ('waiting','generating','held')"),
        sqlite_where=text("state IN ('waiting','generating','held')")),)


def ensure_tables(bind) -> None:
    """Create the schema (PostgreSQL) and every table, idempotently.

    ``bind`` is a synchronous Engine. Never drops or alters anything; an existing
    table is left untouched (``checkfirst``). On PostgreSQL the whole step runs
    under one advisory lock so concurrent starters serialise.
    """
    with bind.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": DDL_LOCK_KEY})
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA_NAME}"'))
            if SCHEMA is None:  # imported under a non-PostgreSQL DATABASE_URL: still target fastt_runtime
                conn = conn.execution_options(schema_translate_map={None: SCHEMA_NAME})
        metadata.create_all(conn, checkfirst=True)


# ── Twin bridge intake (Stage 1C part 1) ──────────────────────────────

class TwinBridgeInput(Base):
    """One durable, account-scoped intake row for the Twin reply bridge.

    A SOURCE-message input, not a final conversation turn: it records that an
    eligible inbound fan DM was already persisted by Fastt and should later be
    considered for a Twin draft. Grouping (7-second merge), generation
    orchestration, human takeover and send-attempt tracking are later stages and
    deliberately have no representation here.

    Identity is `(account_id, fan_id, provider_message_id)` — the same tuple as
    the canonical `messages` row, so the intake row and the message it came from
    can never disagree about which DM this is. The composite primary key is the
    database unique constraint the ingestion hook and the reconciliation both
    rely on for concurrent-safe deduplication (a preliminary SELECT is not
    enough).

    `event_json` is an IMMUTABLE versioned snapshot of the normalized Twin
    inbound event. Later edits to the canonical row must never rewrite it. The
    row also records the creator slug and activation cutoff that were in force
    when it was admitted, so a later configuration change cannot silently
    reassign existing work to a different creator or boundary.

    The bridge bearer token is never stored here (or anywhere in this table).
    """
    __tablename__ = "twin_bridge_inputs"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Trusted scope recorded at admission time.
    creator_slug: Mapped[str] = mapped_column(String, nullable=False)
    activation_cutoff: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    bridge_scope_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Frozen normalized event (Twin's version-1 bridge event, serialized JSON).
    event_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    conversation_id: Mapped[str] = mapped_column(String, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    event_json: Mapped[str] = mapped_column(Text, nullable=False)

    # Provider time (converted to UTC) vs Fastt's FIRST receipt of the row. Both
    # are needed: a freshly scraped/replayed OLD message has an old provider time
    # but may have a new ingested_at, and neither alone is trustworthy.
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Pending for the (not yet built) worker. Terminal states arrive later.
    state: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    admitted_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        # Dedupe/conflict uniqueness IS the composite primary key.
        # Backs reconciliation paging: enabled account, pending state, in
        # provider-time order so equal timestamps still advance deterministically.
        Index(
            "ix_twin_bridge_inputs_account_state_time",
            "account_id", "state", "occurred_at", "provider_message_id",
        ),
    )


# ── Twin bridge turns, conversation control and delivery (Stage 1C part 2) ───
#
# Four additive tables carry the text-delivery path. They are deliberately
# separate from `twin_bridge_inputs`: an input row is an immutable SOURCE-message
# snapshot, while these record what was decided about it (which burst it joined),
# the conversation's ownership/pause state, and each provider send attempt.
#
# No bridge token, session cookie or provider credential is stored here, and
# state is always persisted BEFORE the external side effect it describes.


class TwinBridgeTurn(Base):
    """One frozen burst: the exact ordered membership and event sent to Twin.

    `turn_id` is derived from the frozen identity (account, creator, fan, scope
    fingerprint and ordered source message ids), so the SAME burst always yields
    the SAME id and the SAME event bytes. The primary key therefore makes a
    re-freeze after a crash a no-op instead of a second model call.
    """
    __tablename__ = "twin_bridge_turns"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    turn_id: Mapped[str] = mapped_column(String, primary_key=True)

    creator_slug: Mapped[str] = mapped_column(String, nullable=False)
    conversation_id: Mapped[str] = mapped_column(String, nullable=False)
    scope_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    member_ids_json: Mapped[str] = mapped_column(Text, nullable=False)

    text: Mapped[str] = mapped_column(Text, nullable=False)
    event_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # frozen -> submitted -> draft_ready -> dispatching -> sent -> delivered,
    # plus delivery_unknown / cancelled / attention / parked. See delivery.py.
    state: Mapped[str] = mapped_column(String, nullable=False, default="frozen")
    reason: Mapped[str | None] = mapped_column(String)

    # Fencing: exactly one owner may act, and the revision advances on every
    # ownership change so a stale worker's conditional write matches nothing.
    owner_token: Mapped[str | None] = mapped_column(String)
    owner_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    request_id: Mapped[str | None] = mapped_column(String)
    draft_text: Mapped[str | None] = mapped_column(Text)
    epoch: Mapped[int | None] = mapped_column(Integer)
    # The conversation revision observed when the draft was accepted. The
    # dispatch gate re-reads the conversation and refuses if it has moved (a
    # takeover, pause or reset invalidates the draft), and it survives a restart
    # so recovery reaches the same decision.
    dispatch_revision: Mapped[int | None] = mapped_column(Integer)

    generation_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_polls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ack_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Confirmed provider delivery evidence. A provider id is stored as the
    # decimal STRING the provider returned, never coerced through a float.
    provider_message_id: Mapped[str | None] = mapped_column(String)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = _ts_now()
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_twin_bridge_turns_state_next", "account_id", "state", "next_action_at"),
        Index("ix_twin_bridge_turns_conversation", "account_id", "fan_id", "created_at"),
    )


class TwinBridgeTurnMember(Base):
    """Which source input belongs to which turn - at most ONE turn, ever.

    The primary key is the source input's own identity tuple, so the database -
    not a preliminary SELECT - is what stops two workers assigning the same
    input to different turns.
    """
    __tablename__ = "twin_bridge_turn_members"

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    turn_id: Mapped[str] = mapped_column(String, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        Index("ix_twin_bridge_turn_members_turn", "account_id", "fan_id", "turn_id", "position"),
    )


class TwinBridgeConversation(Base):
    """Durable per-conversation control, ownership and send budget.

    `revision` is the fence: takeover, reset, pause and every ownership change
    advance it, so a worker holding the previous revision can no longer write or
    dispatch. `owner_token` is the current bridge reservation - a paused or
    unknown conversation keeps its row (and its evidence) rather than falling
    back to the older automation writers.
    """
    __tablename__ = "twin_bridge_conversations"
    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    fan_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    creator_slug: Mapped[str] = mapped_column(String, nullable=False)
    conversation_id: Mapped[str] = mapped_column(String, nullable=False)
    scope_fingerprint: Mapped[str] = mapped_column(String, nullable=False)

    # active | paused | attention | disabled
    state: Mapped[str] = mapped_column(String, nullable=False, default="active")
    reason: Mapped[str | None] = mapped_column(String)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    owner_token: Mapped[str | None] = mapped_column(String)
    owner_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    takeover_at: Mapped[datetime | None] = mapped_column(DateTime)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)

    # The DURABLE conversation reservation, deliberately separate from the
    # expiring `owner_token` worker claim. It stays held while the bridge owns
    # the conversation - including while paused / attention / delivery-unknown
    # and across a restart - so a competing local writer (an older automation,
    # a broadcast, or the manual composer without a takeover) is refused at the
    # transport boundary even when the per-attempt claim has expired or the
    # process registry is empty. It is cleared only by an EXPLICIT release: the
    # conversation resolving with no unresolved turn, or a recorded human
    # takeover. Releasing it never deletes the row or its delivery evidence.
    reserved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )

    # Positive pilot send budget. Reserved durably at dispatch, and unknown or
    # in-flight attempts consume it, so a restart cannot raise the cap.
    send_budget_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    send_budget_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = _ts_now()
    updated_at: Mapped[datetime] = _ts_now()


class TwinBridgeSendAttempt(Base):
    """One checked platform-send attempt, written BEFORE the provider POST.

    `dispatching` is the crash marker: a row left in that state is recovered as
    `delivery_unknown`, because a crash around the POST boundary must never be
    read as "nothing was sent".
    """
    __tablename__ = "twin_bridge_send_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    fan_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turn_id: Mapped[str] = mapped_column(String, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # dispatching | sent | delivery_unknown | refused
    state: Mapped[str] = mapped_column(String, nullable=False, default="dispatching")
    reason: Mapped[str | None] = mapped_column(String)
    owner_token: Mapped[str] = mapped_column(String, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    provider_message_id: Mapped[str | None] = mapped_column(String)
    evidence_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts_now()
    updated_at: Mapped[datetime] = _ts_now()

    __table_args__ = (
        UniqueConstraint(
            "account_id", "fan_id", "turn_id", "attempt_no",
            name="uq_twin_bridge_send_attempt",
        ),
        Index("ix_twin_bridge_send_attempts_state", "account_id", "state", "created_at"),
    )


class TwinBridgeWrite(Base):
    """In-flight / unresolved local provider writer marker (the send boundary)."""
    __tablename__ = "twin_bridge_writes"
    account_id = Column(String, primary_key=True)
    fan_id = Column(BigInteger, primary_key=True)
    holder = Column(String, primary_key=True)
    expires_at = Column(String, nullable=False)
    state = Column(String, nullable=False, server_default="held")


class NativePilot(Base):
    __tablename__ = "native_pilots"
    pilot_id = Column(String, primary_key=True)
    account_id = Column(String, nullable=False)
    fan_id = Column(BigInteger, nullable=False)
    owner_token = Column(String, nullable=False)
    cutoff = Column(String, nullable=False)
    deadline = Column(String, nullable=False)
    grant_limit = Column(Integer, nullable=False)
    baseline_used = Column(Integer, nullable=False)
    end_limit = Column(Integer, nullable=False)
    revision = Column(Integer, nullable=False)
    state = Column(String, nullable=False)
    reason = Column(String)
    model_calls = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class NativePilotInput(Base):
    __tablename__ = "native_pilot_inputs"
    __table_args__ = (UniqueConstraint("pilot_id", "message_id", name="uq_native_pilot_inputs_message"),)
    input_key = Column(String, primary_key=True)
    pilot_id = Column(String, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    source_hash = Column(String, nullable=False)
    created_at = Column(String, nullable=False)
    ingested_at = Column(String, nullable=False)
    member_ids_json = Column(Text, nullable=False)
    earliest_created_at = Column(String, nullable=False)
    state = Column(String, nullable=False)
    claimed_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class NativePilotFollowup(Base):
    """Runner-owned durable queue of FASTT's own delayed follow-ups (the re-engage nudge).

    Deliberately NOT ``scheduled_jobs``: the legacy worker/executor never sees these rows, so it can
    never run one. Only ``native_pilot.service`` (``--followups``) reads and settles them; every
    send still goes through the ledger's admission, so the account's delivery allowance caps it.
    """
    __tablename__ = "native_pilot_followups"
    __table_args__ = (Index("ix_native_pilot_followups_due", "account_id", "state", "run_at"),)
    job_id = Column(String, primary_key=True)
    account_id = Column(String, nullable=False)
    fan_id = Column(BigInteger, nullable=False)
    kind = Column(String, nullable=False)
    payload_json = Column(Text, nullable=False)
    run_at = Column(String, nullable=False)
    last_in_id = Column(BigInteger)
    last_in_at = Column(String)
    state = Column(String, nullable=False)
    reason = Column(String)
    input_key = Column(String)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class NativePilotReceipt(Base):
    """One row per admitted provider POST; written BEFORE the POST."""
    __tablename__ = "native_pilot_receipts"
    __table_args__ = (UniqueConstraint("input_key", "bubble_no", name="uq_native_pilot_receipts_bubble"),)
    attempt_id = Column(Integer, primary_key=True, autoincrement=False)
    pilot_id = Column(String, nullable=False)
    input_key = Column(String, nullable=False)
    bubble_no = Column(Integer, nullable=False)
    state = Column(String, nullable=False)
    provider_message_id = Column(String)
    response_json = Column(Text)
    evidence_json = Column(Text)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)
