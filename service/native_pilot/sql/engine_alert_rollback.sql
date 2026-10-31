-- Rollback of engine_alert_upgrade.sql. Dropping loses only the alert history; no send authority lives in the table.
DROP TABLE IF EXISTS fastt_runtime.engine_alert;
