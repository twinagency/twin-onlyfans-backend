"""Durable retry due time and bounded failure evidence for released model calls.

A released HTTP 408/429/5xx or not_sent model call is a proven zero-cost refusal. The scheduler may ask the same
request again once `retry_not_before` has passed; the runtime keeps its attempt limit. Both columns are additive and
nullable (rows from before this revision keep NULL and fall back to the fixed transport cooldown). The model-call
guard additionally freezes both fields once a call has left reserved/calling.
"""
from alembic import op
from sqlalchemy import text

revision = "0129b_model_retry_due"
down_revision = "0129_reconnect_receipt"
branch_labels = depends_on = None

GUARD = "CREATE OR REPLACE FUNCTION of_engine_model_call_guard() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'engine model call evidence is retained'; END IF;\n  IF ROW(NEW.account_id,NEW.call_id,NEW.request_id,NEW.generation,NEW.seq,NEW.chain_pos,NEW.chain_step,NEW.fan_id,NEW.binding_id,\n         NEW.owner_epoch,NEW.provider,NEW.model,NEW.prefixed,NEW.reserved_microusd,NEW.snapshot_sha256,NEW.prompt_sha256,NEW.cost_day,NEW.created_at,NEW.pilot_scope_revision,NEW.pilot_scope_fingerprint)\n     IS DISTINCT FROM ROW(OLD.account_id,OLD.call_id,OLD.request_id,OLD.generation,OLD.seq,OLD.chain_pos,OLD.chain_step,OLD.fan_id,OLD.binding_id,\n         OLD.owner_epoch,OLD.provider,OLD.model,OLD.prefixed,OLD.reserved_microusd,OLD.snapshot_sha256,OLD.prompt_sha256,OLD.cost_day,OLD.created_at,OLD.pilot_scope_revision,OLD.pilot_scope_fingerprint)\n     OR (OLD.status = 'settled' AND ROW(NEW.status,NEW.actual_microusd,NEW.usage_known,NEW.response_sha256) IS DISTINCT FROM ROW(OLD.status,OLD.actual_microusd,OLD.usage_known,OLD.response_sha256))\n     OR (OLD.status = 'released' AND NEW.status <> 'released')\n     OR (OLD.status = 'unknown' AND NEW.status NOT IN ('unknown','settled'))\n     OR (OLD.status = 'calling' AND NEW.status = 'reserved')\n     OR (OLD.usage_known AND ROW(NEW.actual_microusd,NEW.usage_known) IS DISTINCT FROM ROW(OLD.actual_microusd,OLD.usage_known))\n     OR (OLD.provider_generation_id IS NOT NULL AND ROW(NEW.provider_generation_id,NEW.provider_endpoint_sha256)\n         IS DISTINCT FROM ROW(OLD.provider_generation_id,OLD.provider_endpoint_sha256))\n     OR (OLD.provider_generation_id IS NULL AND NEW.provider_generation_id IS NOT NULL AND OLD.status NOT IN ('calling','unknown'))\n     OR (OLD.status NOT IN ('reserved','calling') AND ROW(NEW.retry_not_before,NEW.failure_detail_json) IS DISTINCT FROM ROW(OLD.retry_not_before,OLD.failure_detail_json))\n  THEN RAISE EXCEPTION 'engine model call identity is immutable'; END IF;\n  RETURN NEW;\nEND $$"
OLD_GUARD = "CREATE OR REPLACE FUNCTION of_engine_model_call_guard() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'engine model call evidence is retained'; END IF;\n  IF ROW(NEW.account_id,NEW.call_id,NEW.request_id,NEW.generation,NEW.seq,NEW.chain_pos,NEW.chain_step,NEW.fan_id,NEW.binding_id,\n         NEW.owner_epoch,NEW.provider,NEW.model,NEW.prefixed,NEW.reserved_microusd,NEW.snapshot_sha256,NEW.prompt_sha256,NEW.cost_day,NEW.created_at,NEW.pilot_scope_revision,NEW.pilot_scope_fingerprint)\n     IS DISTINCT FROM ROW(OLD.account_id,OLD.call_id,OLD.request_id,OLD.generation,OLD.seq,OLD.chain_pos,OLD.chain_step,OLD.fan_id,OLD.binding_id,\n         OLD.owner_epoch,OLD.provider,OLD.model,OLD.prefixed,OLD.reserved_microusd,OLD.snapshot_sha256,OLD.prompt_sha256,OLD.cost_day,OLD.created_at,OLD.pilot_scope_revision,OLD.pilot_scope_fingerprint)\n     OR (OLD.status = 'settled' AND ROW(NEW.status,NEW.actual_microusd,NEW.usage_known,NEW.response_sha256) IS DISTINCT FROM ROW(OLD.status,OLD.actual_microusd,OLD.usage_known,OLD.response_sha256))\n     OR (OLD.status = 'released' AND NEW.status <> 'released')\n     OR (OLD.status = 'unknown' AND NEW.status NOT IN ('unknown','settled'))\n     OR (OLD.status = 'calling' AND NEW.status = 'reserved')\n     OR (OLD.usage_known AND ROW(NEW.actual_microusd,NEW.usage_known) IS DISTINCT FROM ROW(OLD.actual_microusd,OLD.usage_known))\n     OR (OLD.provider_generation_id IS NOT NULL AND ROW(NEW.provider_generation_id,NEW.provider_endpoint_sha256)\n         IS DISTINCT FROM ROW(OLD.provider_generation_id,OLD.provider_endpoint_sha256))\n     OR (OLD.provider_generation_id IS NULL AND NEW.provider_generation_id IS NOT NULL AND OLD.status NOT IN ('calling','unknown'))\n  THEN RAISE EXCEPTION 'engine model call identity is immutable'; END IF;\n  RETURN NEW;\nEND $$"


def upgrade():
    op.execute("ALTER TABLE of_engine_model_call ADD COLUMN retry_not_before TIMESTAMP WITHOUT TIME ZONE")
    op.execute("ALTER TABLE of_engine_model_call ADD COLUMN failure_detail_json TEXT")
    op.execute("""ALTER TABLE of_engine_model_call ADD CONSTRAINT ck_of_engine_model_call_failure_detail
      CHECK (failure_detail_json IS NULL OR octet_length(failure_detail_json) <= 2000)""")
    op.execute("""ALTER TABLE of_engine_model_call ADD CONSTRAINT ck_of_engine_model_call_retry_due
      CHECK (retry_not_before IS NULL OR status IN ('reserved','calling','released'))""")
    op.execute(GUARD)


def downgrade():
    if op.get_bind().execute(text("SELECT 1 FROM of_engine_model_call WHERE retry_not_before IS NOT NULL OR failure_detail_json IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("Preserve model retry evidence; use a compatible rollback")
    op.execute(OLD_GUARD)
    op.execute("ALTER TABLE of_engine_model_call DROP CONSTRAINT ck_of_engine_model_call_retry_due")
    op.execute("ALTER TABLE of_engine_model_call DROP CONSTRAINT ck_of_engine_model_call_failure_detail")
    op.execute("ALTER TABLE of_engine_model_call DROP COLUMN failure_detail_json")
    op.execute("ALTER TABLE of_engine_model_call DROP COLUMN retry_not_before")
