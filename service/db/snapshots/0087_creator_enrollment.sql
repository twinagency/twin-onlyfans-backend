create table of_connection_enrollment (
 id varchar primary key, tenant_id varchar not null, creator_id varchar not null,
 creator_name text not null, handle text not null, owner_id bigint not null,
 actor varchar(128) not null, command_key varchar(64) not null, request_hash varchar(64) not null,
 status varchar(16) not null constraint ck_of_connection_enrollment_status check (status in ('pending','checking','accepted','failed','expired')),
 sealed_config text, expires_at timestamp not null, lease_until timestamp,
 generation integer not null, expected_revision integer, account_id varchar,
 last_error varchar(64), created_at timestamp not null, updated_at timestamp not null,
 constraint uq_of_connection_enrollment_tenant_key unique(tenant_id,command_key)
);
create index ix_of_connection_enrollment_pending on of_connection_enrollment(tenant_id,status,created_at);
create index ix_of_connection_enrollment_creator on of_connection_enrollment(tenant_id,creator_id,created_at);
