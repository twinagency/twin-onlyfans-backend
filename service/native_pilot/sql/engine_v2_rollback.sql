-- Rollback of engine_v2_upgrade.sql. Stop every engine process first (the old image does not use
-- these tables, so this is only needed to leave no trace). Dropping loses only the read model and
-- the runner lease; no send authority, receipt, claim or ledger row lives in either table.
DROP TABLE IF EXISTS fastt_runtime.engine_work;
DROP TABLE IF EXISTS fastt_runtime.engine_runner_lease;
