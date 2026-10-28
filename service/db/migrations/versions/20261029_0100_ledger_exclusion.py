"""Audited per-transaction exclusion of unlinked paid purchases from the account-wide commerce hold."""
import sqlalchemy as sa
from alembic import op

revision = "0098_ledger_exclusion"
down_revision = "0097_release_controls"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "of_ledger_exclusion",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("binding_id", sa.String(), nullable=False),
        sa.Column("fan_id", sa.BigInteger(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("blocker", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("command_key", sa.String(64), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["of_runtime.account_id"], name=op.f("fk_of_ledger_exclusion_account_id_of_runtime"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["binding_id"], ["of_creator_bindings.id"], name=op.f("fk_of_ledger_exclusion_binding_id_of_creator_bindings"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], name=op.f("fk_of_ledger_exclusion_transaction_id_transactions"), ondelete="RESTRICT"),
        sa.UniqueConstraint("account_id", "transaction_id", name=op.f("uq_of_ledger_exclusion_account_transaction")),
        sa.UniqueConstraint("command_key", name=op.f("uq_of_ledger_exclusion_command")),
        sa.CheckConstraint("length(trim(reason)) > 0", name=op.f("ck_of_ledger_exclusion_reason")),
        sa.CheckConstraint("length(trim(actor)) > 0", name=op.f("ck_of_ledger_exclusion_actor")),
    )


def downgrade():
    op.drop_table("of_ledger_exclusion")
