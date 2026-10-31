-- OPTIONAL, additive, no schema revision change (check_schema compares tables and columns, not indexes).
-- Speeds the "asked for a person" lookups on busy accounts. Safe to skip: the lookups are correct without it.
-- Run outside a transaction (CONCURRENTLY), as the database owner.
-- UPGRADE
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_of_control_audit_fan_action
    ON of_control_audit (account_id, fan_id, action);
-- ROLLBACK
-- DROP INDEX CONCURRENTLY IF EXISTS ix_of_control_audit_fan_action;
