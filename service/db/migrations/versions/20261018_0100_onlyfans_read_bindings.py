"""onlyfans read bindings — the Twin dashboard's read authority and projections

Three tables for milestone OF-B1, which is the read-only foundation behind the
dashboard's OnlyFans adapter:

  • `of_creator_bindings` — which tenant and creator own one OnlyFans account.
    This is the authorization half: the service reads it on every request and
    refuses an account that is not bound to the calling credential's tenant.
    At most ONE binding per account may be live, enforced by a PARTIAL unique
    index on `account_id` where `revoked_at IS NULL`. A plain `UNIQUE(account_id)`
    would count revoked rows too, so an account that is unlinked and later
    re-linked — possibly back to the same creator — could never be bound again,
    which is the opposite of what revoking is for. Rows are revoked, never
    deleted, so "was this ever ours" stays answerable.

  • `of_conversation_state` — conversation control as a read projection: reply
    mode, monotonic `revision`, takeover state and the unresolved-send hold.
    Nothing in the read service writes it; the delivery authority and the
    operator commands do, in later milestones. Until then every conversation
    reads as automated/none/0, which is true — no control has been recorded.

  • `of_message_delivery` — per-message receipts, with `unknown` as a
    first-class outcome. A timeout or a restart is not proof of failure, and
    recording one as `failed` would invite the resend the plan forbids.

WHAT THIS MIGRATION DELIBERATELY DOES NOT DO:

  • It seeds nothing. There is no default binding, no placeholder creator and no
    implicit tenant. A deployment with no bindings serves an empty account list
    (truthfully: it has no accounts), and an account that is not bound is
    refused. Seeding a binding from `user_accounts` would make the dashboard
    reachable for every existing account the moment the token was set, which is
    a migration decision for the cutover milestone, not this one.
  • It does not touch or backfill `accounts`, `fans`, `chats` or `messages`.
    The read service projects those as they are.

⚠️ NOT APPLIED. Migrations in this repository run as an explicit release
operation; this file was authored and reviewed, and no database — live, test or
local — has been upgraded with it. `init_db()` does NOT run `alembic upgrade`
(it creates missing tables and adds missing nullable columns), so applying this
is `alembic upgrade head` by hand, on a database whose backup policy has been
agreed.

⚠️ REVISION SEQUENCING. This descends from `0064_user_llm_keys`, which is the
head of the committed tree this branch was cut from (base e72316f). A parallel
uncommitted chain in the working checkout (`twin_bridge_intake`,
`twin_bridge_turns`, `twin_bridge_reserved`) also descends from 0064 and uses
the 0065–0067 ids, so this migration takes 0070 to leave that range free.
Whoever lands the two chains together re-parents this one onto the bridge head
instead of merging two branches that both claim 0064 as their parent.

Revision ID: 0070_onlyfans_read_bindings
Revises: 0064_user_llm_keys
Create Date: 2026-10-18
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0070_onlyfans_read_bindings"
down_revision: str | Sequence[str] | None = "0064_user_llm_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent guards, matching this repository's house convention (0022,
    # 0026, …). It is not defensive padding: `0001_initial` builds the whole
    # schema from `Base.metadata.create_all()`, which already includes these
    # three tables, so an unguarded `create_table` here would abort the very
    # first clean `upgrade heads` with "relation already exists". The guard
    # serves both real paths — a database that predates this migration gets the
    # tables from here; one built by create_all gets them from the models, and
    # the two definitions are kept identical.
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "of_creator_bindings" not in existing:
        op.create_table(
            "of_creator_bindings",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("tenant_id", sa.String(), nullable=False),
            sa.Column("creator_id", sa.String(), nullable=False),
            sa.Column("creator_name", sa.String(), nullable=False, server_default=sa.text("''")),
            sa.Column("account_id", sa.String(), nullable=False),
            sa.Column("handle", sa.String(), nullable=False, server_default=sa.text("''")),
            sa.Column("channel", sa.String(), nullable=False, server_default=sa.text("'onlyfans'")),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id", name="pk_of_creator_bindings"),
        )
        op.create_index(
            "ix_of_creator_bindings_tenant", "of_creator_bindings", ["tenant_id", "creator_id"]
        )
        # The lookup every read performs: one live binding by account id.
        op.create_index(
            "ix_of_creator_bindings_live",
            "of_creator_bindings",
            ["account_id", "revoked_at"],
        )
        # THE authority: one ACTIVE binding per account, history unrestricted.
        # A partial unique index rather than `UNIQUE(account_id)`, so a revoked
        # binding does not block the account being bound again. Both dialect
        # predicates are spelled out, because a dialect that silently ignored
        # the WHERE clause would turn this back into the full constraint it
        # replaces — and would refuse the re-link in production that this
        # rehearsal is here to prove.
        op.create_index(
            "uq_of_creator_bindings_active_account",
            "of_creator_bindings",
            ["account_id"],
            unique=True,
            sqlite_where=sa.text("revoked_at IS NULL"),
            postgresql_where=sa.text("revoked_at IS NULL"),
        )

    if "of_conversation_state" not in existing:
        op.create_table(
            "of_conversation_state",
            sa.Column("account_id", sa.String(), nullable=False),
            sa.Column("fan_id", sa.BigInteger(), nullable=False),
            sa.Column("mode", sa.String(), nullable=False, server_default=sa.text("'automated'")),
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column(
                "takeover_state", sa.String(), nullable=False, server_default=sa.text("'none'")
            ),
            sa.Column("unresolved_attempt_id", sa.String(), nullable=True),
            # `sa.false()`, not `sa.text("0")`. PostgreSQL rejects an INTEGER
            # default on a BOOLEAN column outright — "column ... is of type
            # boolean but default expression is of type integer" — so the
            # literal 0 only ever worked on SQLite. `sa.false()` renders as
            # `false` on PostgreSQL and as `0` on SQLite.
            sa.Column(
                "requires_attention", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.ForeignKeyConstraint(
                ["account_id", "fan_id"],
                ["fans.account_id", "fans.fan_id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("account_id", "fan_id", name="pk_of_conversation_state"),
        )
        op.create_index("ix_of_conversation_state_updated", "of_conversation_state", ["updated_at"])

    if "of_message_delivery" not in existing:
        op.create_table(
            "of_message_delivery",
            sa.Column("account_id", sa.String(), nullable=False),
            sa.Column("fan_id", sa.BigInteger(), nullable=False),
            sa.Column("message_id", sa.BigInteger(), nullable=False),
            sa.Column("outcome", sa.String(), nullable=False, server_default=sa.text("'none'")),
            sa.Column("attempt_id", sa.String(), nullable=True),
            sa.Column("evidence_ref", sa.String(), nullable=True),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column(
                "recorded_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.ForeignKeyConstraint(
                ["account_id", "fan_id", "message_id"],
                ["messages.account_id", "messages.fan_id", "messages.message_id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint(
                "account_id", "fan_id", "message_id", name="pk_of_message_delivery"
            ),
        )
        op.create_index("ix_of_message_delivery_attempt", "of_message_delivery", ["attempt_id"])


def downgrade() -> None:
    """Drops the three tables. Written so a rehearsal can be backed out.

    Each table's indexes and constraints disappear with the table, so there is
    no separate index drop that could get out of step with the guarded upgrade,
    and each drop is guarded for the same reason the creates are: over a
    database whose schema came from `create_all`, "downgrade" must still be a
    reversible operation rather than a failure. No other table is touched, and
    nothing here restores state that this migration was the only record of —
    which is why it seeds nothing.
    """
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "of_message_delivery" in existing:
        op.drop_table("of_message_delivery")
    if "of_conversation_state" in existing:
        op.drop_table("of_conversation_state")
    if "of_creator_bindings" in existing:
        op.drop_table("of_creator_bindings")
