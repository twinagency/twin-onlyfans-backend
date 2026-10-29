-- Fan-memory throughput release, 2026-10-29. Additive and compatible with the currently running
-- learner (it ignores the new columns). Apply in this order, BEFORE swapping the learner image.
-- Review in a deployment window. Nothing here touches sender, control, allowance or sales tables.

-- 1. Columns (metadata-only on PostgreSQL 11+; fail fast instead of queueing behind live traffic).
SET lock_timeout='3s';
ALTER TABLE of_profile_learning ADD COLUMN IF NOT EXISTS priority smallint NOT NULL DEFAULT 2;
ALTER TABLE of_profile_accounts ADD COLUMN IF NOT EXISTS hold_until timestamp;
ALTER TABLE of_profile_accounts ADD COLUMN IF NOT EXISTS hold_code text;

-- 2. Claim index. Run this single statement OUTSIDE a transaction (psql -c ...), it is CONCURRENTLY.
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_of_profile_learning_claim
--     ON of_profile_learning(tenant_id,account_id,binding_id,priority,next_at,fan_id);

-- 3. Tier backfill: fans that have any incoming message are tier 1 (about 1,000 of 74,000 fans).
--    One statement per account keeps each transaction short; idempotent.
UPDATE of_profile_learning p SET priority=1
 WHERE p.fan_id>0 AND p.priority=2
   AND EXISTS (SELECT 1 FROM messages m WHERE m.account_id=p.account_id AND m.fan_id=p.fan_id AND m.direction='in');

-- 4. Replace the message trigger function with notifications-postgresql.sql from this release
--    (incoming-only fan queueing, throttled creator-draft touch). Rollback: rollback-fanmemory-20261029.sql.
