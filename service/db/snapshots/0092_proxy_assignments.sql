CREATE TABLE of_proxy_assignment (
    tenant_id varchar NOT NULL,
    creator_id varchar NOT NULL,
    pool_id varchar(64) NOT NULL,
    proxy_fingerprint varchar(64) NOT NULL,
    sealed_proxy text NOT NULL,
    created_at timestamp without time zone NOT NULL,
    PRIMARY KEY (tenant_id, creator_id),
    CONSTRAINT uq_of_proxy_assignment_pool UNIQUE (pool_id),
    CONSTRAINT uq_of_proxy_assignment_network UNIQUE (proxy_fingerprint)
);
-- Preserve current limits, usage, queued work and cooldowns; permit explicit production settings.
ALTER TABLE of_delivery_policy ALTER COLUMN throttle_seconds TYPE double precision USING throttle_seconds::double precision;
ALTER TABLE of_delivery_policy DROP CONSTRAINT ck_of_delivery_policy_throttle_seconds;
ALTER TABLE of_delivery_policy ADD CONSTRAINT ck_of_delivery_policy_throttle_seconds CHECK (throttle_seconds >= 0.1 AND throttle_seconds <= 86400);
ALTER TABLE of_delivery_policy DROP CONSTRAINT ck_of_delivery_policy_limit_units;
ALTER TABLE of_delivery_policy ADD CONSTRAINT ck_of_delivery_policy_limit_units CHECK (limit_units >= 0 AND limit_units <= 1000000);
