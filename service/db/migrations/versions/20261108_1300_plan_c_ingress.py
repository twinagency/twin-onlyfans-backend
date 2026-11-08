"""Safely parse immutable pilot ingress metadata without decoding display history."""
from alembic import op

revision = '0126_plan_c_ingress'
down_revision = '0125_plan_c_scope'
branch_labels = depends_on = None

METADATA = 'CREATE FUNCTION of_engine_pilot_ingress_metadata(raw text) RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $$\nDECLARE data jsonb; stamp timestamptz; direction text;\nBEGIN\n  data := raw::jsonb;\n  direction := data->>\'direction\';\n  IF data->>\'created_at\' IS NULL OR data->>\'created_at\' !~ \'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,6})?\\+00:00$\' THEN RETURN NULL; END IF;\n  stamp := (data->>\'created_at\')::timestamptz;\n  IF direction IS NULL OR direction NOT IN (\'in\',\'out\') OR stamp IS NULL THEN RETURN NULL; END IF;\n  RETURN jsonb_build_object(\'direction\',direction,\'created_at\',to_char(stamp AT TIME ZONE \'UTC\',\'YYYY-MM-DD"T"HH24:MI:SS.US\'));\nEXCEPTION WHEN others THEN RETURN NULL;\nEND\n$$'

def upgrade():
    op.execute(METADATA)

def downgrade():
    op.execute('DROP FUNCTION of_engine_pilot_ingress_metadata(text)')
