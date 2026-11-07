"""Keep creator-approved catalogue aliases tied to content versions."""
from alembic import op
import sqlalchemy as sa

revision = '0116_engine_catalogue'
down_revision = '0115_engine_memory'
branch_labels = depends_on = None

STATEMENTS = [
    """CREATE TABLE of_engine_catalogue_alias (
      account_id VARCHAR NOT NULL, alias_id VARCHAR(36) NOT NULL, binding_id VARCHAR NOT NULL,
      item_id INTEGER NOT NULL, alias VARCHAR(128) NOT NULL, normalized VARCHAR(128) NOT NULL,
      content_hash VARCHAR(64) NOT NULL, actor VARCHAR(128) NOT NULL, created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_engine_catalogue_alias PRIMARY KEY(account_id,alias_id),
      CONSTRAINT fk_engine_alias_binding FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
      CONSTRAINT fk_engine_alias_item FOREIGN KEY(item_id) REFERENCES catalog_items(id) ON DELETE RESTRICT,
      CONSTRAINT uq_engine_catalogue_alias_version UNIQUE(account_id,binding_id,item_id,normalized,content_hash)
    )""",
    'CREATE INDEX ix_engine_catalogue_alias_lookup ON of_engine_catalogue_alias(account_id,binding_id,normalized)',
    """CREATE FUNCTION of_engine_catalogue_alias_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'catalogue alias evidence is append-only'; END $$""",
    'CREATE TRIGGER of_engine_catalogue_alias_immutable BEFORE UPDATE OR DELETE ON of_engine_catalogue_alias FOR EACH ROW EXECUTE FUNCTION of_engine_catalogue_alias_immutable()',
]


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)
    op.execute('ALTER TABLE of_engine_catalogue_alias ENABLE ROW LEVEL SECURITY')
    op.execute('REVOKE ALL ON of_engine_catalogue_alias FROM PUBLIC')
    for role in ('anon', 'authenticated'):
        op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON of_engine_catalogue_alias FROM {role}; END IF; END $$")


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM of_engine_catalogue_alias LIMIT 1')).first():
        raise RuntimeError('Preserve catalogue alias approval evidence; use a compatible rollback')
    op.execute('DROP TRIGGER of_engine_catalogue_alias_immutable ON of_engine_catalogue_alias')
    op.execute('DROP FUNCTION of_engine_catalogue_alias_immutable()')
    op.drop_table('of_engine_catalogue_alias')
