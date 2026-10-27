-- Explicit broker schema upgrade; never run from a request.
ALTER TABLE onlyfans_proxy.inventory ADD COLUMN IF NOT EXISTS used_at timestamptz;
ALTER TABLE onlyfans_proxy.audit ADD COLUMN IF NOT EXISTS command_key varchar(64);
ALTER TABLE onlyfans_proxy.audit ADD COLUMN IF NOT EXISTS details jsonb;
CREATE UNIQUE INDEX IF NOT EXISTS proxy_audit_command ON onlyfans_proxy.audit(tenant_id,command_key) WHERE command_key IS NOT NULL;

ALTER TABLE onlyfans_proxy.inventory ADD COLUMN IF NOT EXISTS setup_owner_key varchar(64);
ALTER TABLE onlyfans_proxy.inventory ADD COLUMN IF NOT EXISTS pending_handle boolean NOT NULL DEFAULT false;
CREATE UNIQUE INDEX IF NOT EXISTS proxy_setup_owner ON onlyfans_proxy.inventory(setup_owner_key) WHERE setup_owner_key IS NOT NULL;
