"""Agency category single prices stay on existing internal folders."""
from alembic import op
import sqlalchemy as sa
revision="0131_manual_category_prices"
down_revision="0130_commercial_vault_provenance"
branch_labels=depends_on=None
def upgrade():
    for column in (
        sa.Column("single_price_cents",sa.Integer(),nullable=True),
        sa.Column("price_revision",sa.Integer(),nullable=False,server_default="0"),
        sa.Column("pricing_binding_id",sa.String(),nullable=True),
        sa.Column("pricing_actor",sa.String(128),nullable=True),
        sa.Column("pricing_updated_at",sa.DateTime(),nullable=True)):
        op.add_column("vault_folders",column)
    op.create_check_constraint(op.f("ck_vault_folders_manual_price"),"vault_folders",
        "(single_price_cents IS NULL AND price_revision=0 AND pricing_binding_id IS NULL AND pricing_actor IS NULL AND pricing_updated_at IS NULL) OR "
        "(single_price_cents IS NOT NULL AND single_price_cents BETWEEN 300 AND 20000 AND price_revision>0 AND pricing_binding_id IS NOT NULL AND pricing_actor IS NOT NULL AND pricing_updated_at IS NOT NULL)")
def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM vault_folders WHERE price_revision>0 LIMIT 1")).first():
        raise RuntimeError("Preserve agency prices; use compatible code rollback")
    op.drop_constraint(op.f("ck_vault_folders_manual_price"),"vault_folders")
    for name in ("single_price_cents","price_revision","pricing_binding_id","pricing_actor","pricing_updated_at"):
        op.drop_column("vault_folders",name)
