"""Complete 0129b_model_retry_due on databases that skipped it (forward repair, data preserving, idempotent).

The commercial graph inserted 0129b between 0129 and 0130 (R0). A database created by the original lineage that was already
at 0130..0133, or was raw-upgraded to 0136, never ran 0129b: it has no of_engine_model_call.retry_not_before /
failure_detail_json, no failure_detail / retry_due checks and still carries the pre-R0 model-call guard, yet its revision claims
ancestors that include 0129b. This revision adds exactly the missing 0129b objects and nothing else:

* both columns are nullable and additive; existing rows (settled, unknown, released, reserved) keep every value and get NULL;
* each check constraint is added only when absent (every existing row satisfies both: retry_not_before is NULL);
* the R0 guard is installed only over the pre-R0 body or an identical R0 body; any other body is refused, never overwritten.

On a database that did run 0129b every step is a no-op. Downgrade removes nothing: the objects are owned by 0129b and its
downgrade, which still refuses while retry evidence exists.
"""
import hashlib

from alembic import op
from sqlalchemy import text

revision = "0137_model_retry_due_repair"
down_revision = "0136_history_coverage"
branch_labels = depends_on = None

# Byte-for-byte the 0129b guard (asserted against the schema pin by the lineage tests).
GUARD = "CREATE OR REPLACE FUNCTION of_engine_model_call_guard() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'engine model call evidence is retained'; END IF;\n  IF ROW(NEW.account_id,NEW.call_id,NEW.request_id,NEW.generation,NEW.seq,NEW.chain_pos,NEW.chain_step,NEW.fan_id,NEW.binding_id,\n         NEW.owner_epoch,NEW.provider,NEW.model,NEW.prefixed,NEW.reserved_microusd,NEW.snapshot_sha256,NEW.prompt_sha256,NEW.cost_day,NEW.created_at,NEW.pilot_scope_revision,NEW.pilot_scope_fingerprint)\n     IS DISTINCT FROM ROW(OLD.account_id,OLD.call_id,OLD.request_id,OLD.generation,OLD.seq,OLD.chain_pos,OLD.chain_step,OLD.fan_id,OLD.binding_id,\n         OLD.owner_epoch,OLD.provider,OLD.model,OLD.prefixed,OLD.reserved_microusd,OLD.snapshot_sha256,OLD.prompt_sha256,OLD.cost_day,OLD.created_at,OLD.pilot_scope_revision,OLD.pilot_scope_fingerprint)\n     OR (OLD.status = 'settled' AND ROW(NEW.status,NEW.actual_microusd,NEW.usage_known,NEW.response_sha256) IS DISTINCT FROM ROW(OLD.status,OLD.actual_microusd,OLD.usage_known,OLD.response_sha256))\n     OR (OLD.status = 'released' AND NEW.status <> 'released')\n     OR (OLD.status = 'unknown' AND NEW.status NOT IN ('unknown','settled'))\n     OR (OLD.status = 'calling' AND NEW.status = 'reserved')\n     OR (OLD.usage_known AND ROW(NEW.actual_microusd,NEW.usage_known) IS DISTINCT FROM ROW(OLD.actual_microusd,OLD.usage_known))\n     OR (OLD.provider_generation_id IS NOT NULL AND ROW(NEW.provider_generation_id,NEW.provider_endpoint_sha256)\n         IS DISTINCT FROM ROW(OLD.provider_generation_id,OLD.provider_endpoint_sha256))\n     OR (OLD.provider_generation_id IS NULL AND NEW.provider_generation_id IS NOT NULL AND OLD.status NOT IN ('calling','unknown'))\n     OR (OLD.status NOT IN ('reserved','calling') AND ROW(NEW.retry_not_before,NEW.failure_detail_json) IS DISTINCT FROM ROW(OLD.retry_not_before,OLD.failure_detail_json))\n  THEN RAISE EXCEPTION 'engine model call identity is immutable'; END IF;\n  RETURN NEW;\nEND $$"
PRE_R0_GUARD_DIGEST = "5630b811cd298311ff01ddcc0314a176be308b5d56873c50ebd780d0bdd433d8"
R0_GUARD_DIGEST = "ab95da609ffe3d4915fd52aab42c61a99cd56649bac3e345ee34b41b61508cf5"


def _digest(body):
    return hashlib.sha256(" ".join(body.split()).encode()).hexdigest()


def upgrade():
    bind = op.get_bind()
    if not bind.scalar(text("SELECT to_regclass('of_engine_model_call') IS NOT NULL")):
        raise RuntimeError("of_engine_model_call is missing; this is not a supported schema")
    bodies = bind.execute(text("SELECT prosrc FROM pg_proc WHERE proname = 'of_engine_model_call_guard'")).scalars().all()
    if len(bodies) != 1 or _digest(bodies[0]) not in (PRE_R0_GUARD_DIGEST, R0_GUARD_DIGEST):
        raise RuntimeError("Unrecognised model call guard; it is never overwritten by a repair")
    op.execute("ALTER TABLE of_engine_model_call ADD COLUMN IF NOT EXISTS retry_not_before TIMESTAMP WITHOUT TIME ZONE")
    op.execute("ALTER TABLE of_engine_model_call ADD COLUMN IF NOT EXISTS failure_detail_json TEXT")
    existing = set(bind.execute(text("SELECT conname FROM pg_constraint WHERE conrelid = 'of_engine_model_call'::regclass")).scalars())
    if "ck_of_engine_model_call_failure_detail" not in existing:
        op.execute("""ALTER TABLE of_engine_model_call ADD CONSTRAINT ck_of_engine_model_call_failure_detail
          CHECK (failure_detail_json IS NULL OR octet_length(failure_detail_json) <= 2000)""")
    if "ck_of_engine_model_call_retry_due" not in existing:
        op.execute("""ALTER TABLE of_engine_model_call ADD CONSTRAINT ck_of_engine_model_call_retry_due
          CHECK (retry_not_before IS NULL OR status IN ('reserved','calling','released'))""")
    if _digest(bodies[0]) == PRE_R0_GUARD_DIGEST:
        op.execute(GUARD)


def downgrade():
    # Nothing to undo: the repaired objects belong to 0129b (whose downgrade refuses while retry evidence exists).
    pass
