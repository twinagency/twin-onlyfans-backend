"""Compatible commercial scope contract: promote a preparation row IN PLACE, never recreate it.

Additive. The preparation row (scope_id > 1, contract preparation.v1) may move once, by one audited command, to
contract commercial.v1. The same physical row keeps its scope_id, account, binding, created_at, provider counters and
last attempt, so no fresh budget exists. Promotion never activates anything: the row stays configured and only a later
audited operator command may set it active. A commercial row can never return to preparation, be deleted, or lose its
lineage. Legacy scope_id 1 never carries a contract and is unchanged. Delivery code from 0133 refuses every scope_id > 1
row, so an older release cannot send for a promoted account.
"""
from alembic import op
import sqlalchemy as sa

revision = "0134_commercial_scope_contract"
down_revision = "0133_preparation_retry_state"
branch_labels = depends_on = None

GUARD = """CREATE OR REPLACE FUNCTION of_engine_pilot_scope_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  new_cfg jsonb; old_cfg jsonb; new_ver text; old_ver text;
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'pilot scope is retained'; END IF;
  new_cfg := NEW.config_json::jsonb; new_ver := new_cfg ->> 'contract_version';
  IF NEW.scope_id = 1 AND (new_ver IS NOT NULL OR NEW.transition_command_key IS NOT NULL OR NEW.commercial_since IS NOT NULL) THEN
    RAISE EXCEPTION 'legacy pilot scope has no contract'; END IF;
  IF NEW.scope_id > 1 AND new_ver IS DISTINCT FROM 'preparation.v1' AND new_ver IS DISTINCT FROM 'commercial.v1' THEN
    RAISE EXCEPTION 'preparation scope cannot authorize delivery'; END IF;
  IF NEW.scope_id > 1 AND new_ver = 'preparation.v1' AND (
       NEW.status = 'active' OR NEW.active_since IS NOT NULL
       OR NEW.transition_command_key IS NOT NULL OR NEW.commercial_since IS NOT NULL) THEN
    RAISE EXCEPTION 'preparation scope cannot authorize delivery'; END IF;
  IF NEW.scope_id > 1 AND new_ver = 'commercial.v1' THEN
    IF NEW.transition_command_key IS NULL OR NEW.commercial_since IS NULL
       OR (new_cfg #>> '{transition,command_key}') IS DISTINCT FROM NEW.transition_command_key THEN
      RAISE EXCEPTION 'commercial scope needs its audited promotion'; END IF;
    IF (NEW.status = 'active' OR NEW.active_since IS NOT NULL) AND (new_cfg #>> '{operator,actor}') IS NULL THEN
      RAISE EXCEPTION 'commercial scope cannot activate without a named operator'; END IF;
    IF COALESCE((new_cfg #>> '{limits,provider_requests}')::bigint, -1) < NEW.provider_requests_used
       OR COALESCE((new_cfg #>> '{limits,provider_credit_budget}')::bigint, -1) < NEW.provider_credits_reserved THEN
      RAISE EXCEPTION 'commercial limits cannot fall below existing reservations'; END IF;
    IF jsonb_array_length(new_cfg -> 'fans') > 100 THEN
      RAISE EXCEPTION 'commercial audience is capped'; END IF;
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.scope_id < 1 OR NEW.revision <> 1 OR NEW.status <> 'configured'
       OR NEW.provider_requests_used <> 0 OR NEW.provider_credits_reserved <> 0
       OR (NEW.scope_id > 1 AND new_ver <> 'preparation.v1') THEN
      RAISE EXCEPTION 'pilot scope must start configured and unused'; END IF;
    RETURN NEW;
  END IF;
  old_cfg := OLD.config_json::jsonb; old_ver := old_cfg ->> 'contract_version';
  IF NEW.scope_id > 1 AND
     ROW(new_cfg ->> 'provider_account',new_cfg ->> 'creator_id',new_cfg #>> '{qualification,mode}')
     IS DISTINCT FROM ROW(old_cfg ->> 'provider_account',old_cfg ->> 'creator_id',old_cfg #>> '{qualification,mode}') THEN
    RAISE EXCEPTION 'preparation provider identity is retained'; END IF;
  IF ROW(NEW.scope_id,NEW.account_id,NEW.binding_id,NEW.created_at)
     IS DISTINCT FROM ROW(OLD.scope_id,OLD.account_id,OLD.binding_id,OLD.created_at)
     OR NEW.provider_requests_used < OLD.provider_requests_used
     OR NEW.provider_credits_reserved < OLD.provider_credits_reserved THEN
    RAISE EXCEPTION 'pilot identity and reservations are retained'; END IF;
  IF old_ver IS DISTINCT FROM new_ver THEN
    IF NOT (NEW.scope_id > 1 AND old_ver = 'preparation.v1' AND new_ver = 'commercial.v1'
            AND OLD.transition_command_key IS NULL AND OLD.commercial_since IS NULL
            AND NEW.revision = OLD.revision + 1 AND NEW.status = 'configured' AND NEW.active_since IS NULL
            AND (new_cfg #>> '{transition,from}') = 'preparation.v1'
            AND (new_cfg #>> '{transition,from_fingerprint}') = OLD.fingerprint
            AND (new_cfg #>> '{transition,from_revision}') = OLD.revision::text
            AND (new_cfg #>> '{qualification,reference}') IS NOT DISTINCT FROM (old_cfg #>> '{qualification,reference}')) THEN
      RAISE EXCEPTION 'scope contract transition is invalid'; END IF;
  ELSIF new_ver = 'commercial.v1' THEN
    IF ROW(NEW.transition_command_key,NEW.commercial_since,new_cfg -> 'transition',new_cfg #>> '{qualification,reference}')
       IS DISTINCT FROM ROW(OLD.transition_command_key,OLD.commercial_since,old_cfg -> 'transition',old_cfg #>> '{qualification,reference}') THEN
      RAISE EXCEPTION 'commercial lineage is retained'; END IF;
  END IF;
  IF ROW(NEW.status,NEW.active_since,NEW.config_json,NEW.fingerprint) IS DISTINCT FROM ROW(OLD.status,OLD.active_since,OLD.config_json,OLD.fingerprint) THEN
    IF NEW.revision <> OLD.revision + 1 OR
       (OLD.status = 'active' AND ROW(NEW.config_json,NEW.fingerprint) IS DISTINCT FROM ROW(OLD.config_json,OLD.fingerprint)) THEN
      RAISE EXCEPTION 'pilot configuration transition is invalid'; END IF;
  ELSIF NEW.revision <> OLD.revision THEN
    RAISE EXCEPTION 'pilot revision changed without a configuration transition';
  END IF;
  RETURN NEW;
END
$$"""

# The 0132 guard, restored by downgrade (only reachable while no row has left preparation).
OLD_GUARD = "CREATE OR REPLACE FUNCTION of_engine_pilot_scope_guard() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'pilot scope is retained'; END IF;\n  IF NEW.scope_id > 1 AND (\n       (NEW.config_json::jsonb ->> 'contract_version') IS DISTINCT FROM 'preparation.v1'\n       OR NEW.status = 'active' OR NEW.active_since IS NOT NULL) THEN\n    RAISE EXCEPTION 'preparation scope cannot authorize delivery'; END IF;\n  IF TG_OP = 'UPDATE' AND NEW.scope_id > 1 AND\n     ROW(NEW.config_json::jsonb ->> 'provider_account',NEW.config_json::jsonb ->> 'creator_id',\n         NEW.config_json::jsonb #>> '{qualification,mode}')\n     IS DISTINCT FROM ROW(OLD.config_json::jsonb ->> 'provider_account',OLD.config_json::jsonb ->> 'creator_id',\n         OLD.config_json::jsonb #>> '{qualification,mode}') THEN\n    RAISE EXCEPTION 'preparation provider identity is retained'; END IF;\n  IF TG_OP = 'INSERT' THEN\n    IF NEW.scope_id < 1 OR NEW.revision <> 1 OR NEW.status <> 'configured'\n       OR NEW.provider_requests_used <> 0 OR NEW.provider_credits_reserved <> 0 THEN\n      RAISE EXCEPTION 'pilot scope must start configured and unused'; END IF;\n    RETURN NEW;\n  END IF;\n  IF ROW(NEW.scope_id,NEW.account_id,NEW.binding_id,NEW.created_at)\n     IS DISTINCT FROM ROW(OLD.scope_id,OLD.account_id,OLD.binding_id,OLD.created_at)\n     OR NEW.provider_requests_used < OLD.provider_requests_used\n     OR NEW.provider_credits_reserved < OLD.provider_credits_reserved THEN\n    RAISE EXCEPTION 'pilot identity and reservations are retained'; END IF;\n  IF ROW(NEW.status,NEW.active_since,NEW.config_json,NEW.fingerprint) IS DISTINCT FROM ROW(OLD.status,OLD.active_since,OLD.config_json,OLD.fingerprint) THEN\n    IF NEW.revision <> OLD.revision + 1 OR\n       (OLD.status = 'active' AND ROW(NEW.config_json,NEW.fingerprint) IS DISTINCT FROM ROW(OLD.config_json,OLD.fingerprint)) THEN\n      RAISE EXCEPTION 'pilot configuration transition is invalid'; END IF;\n  ELSIF NEW.revision <> OLD.revision THEN\n    RAISE EXCEPTION 'pilot revision changed without a configuration transition';\n  END IF;\n  RETURN NEW;\nEND\n$$"

FK = "fk_of_engine_pilot_scope_account_id_of_launch_command"


def upgrade():
    # Plain nullable columns: no data rewrite, no new row, and existing rows keep every value they have.
    op.add_column("of_engine_pilot_scope", sa.Column("transition_command_key", sa.String(64), nullable=True))
    op.add_column("of_engine_pilot_scope", sa.Column("commercial_since", sa.DateTime(), nullable=True))
    # The audit row (append-only) must exist before a scope row can name it.
    op.execute(f"ALTER TABLE of_engine_pilot_scope ADD CONSTRAINT {FK} FOREIGN KEY (account_id, transition_command_key) "
               "REFERENCES of_launch_command (account_id, command_key) ON DELETE RESTRICT")
    op.execute(GUARD)


def downgrade():
    # Evidence is never destroyed: a promoted row has no preparation-only representation.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM of_engine_pilot_scope WHERE transition_command_key IS NOT NULL "
                            "OR commercial_since IS NOT NULL OR (config_json::jsonb ->> 'contract_version') = 'commercial.v1' LIMIT 1")).first():
        raise RuntimeError("Preserve commercial scope authority and its lineage; use compatible code rollback")
    op.execute(OLD_GUARD)
    op.execute(f"ALTER TABLE of_engine_pilot_scope DROP CONSTRAINT {FK}")
    op.drop_column("of_engine_pilot_scope", "commercial_since")
    op.drop_column("of_engine_pilot_scope", "transition_command_key")
