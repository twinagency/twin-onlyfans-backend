CREATE SCHEMA onlyfans_proxy;
CREATE TABLE onlyfans_proxy.inventory (
    address_hash varchar(64) PRIMARY KEY,
    country varchar(2) NOT NULL,
    order_id bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    sealed_url text NOT NULL,
    state varchar(24) NOT NULL CHECK (state IN ('available','assigned','legacy_reserved','quarantined','retired')),
    owner_key varchar(64) UNIQUE,
    tenant_id varchar(64),
    creator_id varchar(64),
    handle varchar(80),
    account_id varchar(64),
    assigned_at timestamptz,
    observed_at timestamptz NOT NULL,
    CHECK ((owner_key IS NULL AND tenant_id IS NULL AND creator_id IS NULL AND handle IS NULL)
        OR (owner_key IS NOT NULL AND tenant_id IS NOT NULL AND creator_id IS NOT NULL AND handle IS NOT NULL))
);
CREATE INDEX inventory_available_country ON onlyfans_proxy.inventory(country,expires_at,address_hash) WHERE state='available';
CREATE TABLE onlyfans_proxy.audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    address_hash varchar(64) NOT NULL REFERENCES onlyfans_proxy.inventory(address_hash),
    tenant_id varchar(64) NOT NULL,
    creator_id varchar(64) NOT NULL,
    handle varchar(80) NOT NULL,
    actor varchar(128) NOT NULL,
    action varchar(24) NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
REVOKE ALL ON SCHEMA onlyfans_proxy FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA onlyfans_proxy FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA onlyfans_proxy FROM PUBLIC;
