"""Add isolated engine state beside canonical messages, intents and receipts."""
from alembic import op
import sqlalchemy as sa

revision = '0113_conversation_engine'
down_revision = '0112_vault_recent_window'
branch_labels = depends_on = None

STATEMENTS = [
    '''CREATE TABLE of_engine_owner (
      account_id VARCHAR NOT NULL, fan_id BIGINT NOT NULL, binding_id VARCHAR NOT NULL,
      mode VARCHAR(16) NOT NULL, epoch BIGINT NOT NULL, revision INTEGER NOT NULL,
      sales_mode VARCHAR(24) NOT NULL, sales_scope_json TEXT NOT NULL,
      sales_source_message_id BIGINT, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_engine_owner PRIMARY KEY (account_id,fan_id),
      CONSTRAINT fk_of_engine_owner_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
      CONSTRAINT fk_of_engine_owner_account_id_fans FOREIGN KEY(account_id,fan_id) REFERENCES fans(account_id,fan_id) ON DELETE RESTRICT,
      CONSTRAINT fk_of_engine_owner_account_id_messages FOREIGN KEY(account_id,fan_id,sales_source_message_id) REFERENCES messages(account_id,fan_id,message_id) ON DELETE RESTRICT,
      CONSTRAINT fk_of_engine_owner_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
      CONSTRAINT ck_of_engine_owner_mode CHECK(mode IN ('active','paused','human')),
      CONSTRAINT ck_of_engine_owner_revision CHECK(epoch > 0 AND revision >= 0),
      CONSTRAINT ck_of_engine_owner_sales_mode CHECK(sales_mode IN ('open','paused','reopened_scoped'))
    )''',
    '''CREATE TABLE of_engine_request (
      account_id VARCHAR NOT NULL, request_id VARCHAR(36) NOT NULL, fan_id BIGINT NOT NULL,
      binding_id VARCHAR NOT NULL, source_message_id BIGINT NOT NULL, status VARCHAR(24) NOT NULL,
      owner VARCHAR(128) NOT NULL, next_step VARCHAR(128) NOT NULL, handover_target VARCHAR(128), accepted_by VARCHAR(128), revision INTEGER NOT NULL,
      created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_engine_request PRIMARY KEY (account_id,request_id),
      CONSTRAINT fk_of_engine_request_account_id_of_engine_owner FOREIGN KEY(account_id,fan_id) REFERENCES of_engine_owner(account_id,fan_id) ON DELETE RESTRICT,
      CONSTRAINT fk_of_engine_request_account_id_messages FOREIGN KEY(account_id,fan_id,source_message_id) REFERENCES messages(account_id,fan_id,message_id) ON DELETE RESTRICT,
      CONSTRAINT fk_of_engine_request_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
      CONSTRAINT uq_engine_request_source UNIQUE(account_id,fan_id,source_message_id),
      CONSTRAINT ck_of_engine_request_status CHECK(status IN ('open','waiting_fan','waiting_provider','handover_pending','handover_accepted','completed','declined','cancelled')),
      CONSTRAINT ck_of_engine_request_revision CHECK(revision >= 0)
    )''',
    'CREATE INDEX ix_engine_request_open ON of_engine_request (account_id,fan_id,status)',
    '''CREATE TABLE of_engine_decision (
      account_id VARCHAR NOT NULL, decision_id VARCHAR(36) NOT NULL, request_id VARCHAR(36) NOT NULL,
      fan_id BIGINT NOT NULL, binding_id VARCHAR NOT NULL, owner_epoch BIGINT NOT NULL, connection_epoch BIGINT,
      input_revision BIGINT NOT NULL, control_revision INTEGER NOT NULL, action VARCHAR(16) NOT NULL,
      offer_grant_id VARCHAR(36), catalog_item_id INTEGER, terms_json TEXT, body TEXT NOT NULL, payload_hash VARCHAR(64) NOT NULL, snapshot_hash VARCHAR(64) NOT NULL,
      intent_id VARCHAR(36), status VARCHAR(16) NOT NULL,
      created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_engine_decision PRIMARY KEY(account_id,decision_id),
      CONSTRAINT fk_of_engine_decision_account_id_of_engine_owner FOREIGN KEY(account_id,fan_id) REFERENCES of_engine_owner(account_id,fan_id) ON DELETE RESTRICT,
      CONSTRAINT fk_of_engine_decision_account_id_of_engine_request FOREIGN KEY(account_id,request_id) REFERENCES of_engine_request(account_id,request_id) ON DELETE RESTRICT,
      CONSTRAINT fk_of_engine_decision_account_id_of_text_intent FOREIGN KEY(account_id,intent_id) REFERENCES of_text_intent(account_id,intent_id) ON DELETE RESTRICT,
      CONSTRAINT fk_of_engine_decision_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
      CONSTRAINT uq_engine_decision_request UNIQUE(account_id,request_id),
      CONSTRAINT uq_engine_decision_intent UNIQUE(account_id,intent_id),
      CONSTRAINT ck_of_engine_decision_status CHECK(status IN ('ready','queued','unknown','completed','cancelled','held')),
      CONSTRAINT ck_of_engine_decision_action CHECK(action IN ('reply','offer'))
    )''',
    'CREATE INDEX ix_engine_decision_status ON of_engine_decision (account_id,fan_id,status)',
    """CREATE TABLE of_engine_offer_grant (
      account_id VARCHAR NOT NULL, grant_id VARCHAR(36) NOT NULL, fan_id BIGINT NOT NULL,
      binding_id VARCHAR NOT NULL, item_id INTEGER NOT NULL, terms_hash VARCHAR(64) NOT NULL,
      coverage_ref VARCHAR(128) NOT NULL, expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      used_intent_id VARCHAR(36), created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_engine_offer_grant PRIMARY KEY(account_id,grant_id),
      CONSTRAINT fk_of_engine_offer_grant_account_id_of_engine_owner FOREIGN KEY(account_id,fan_id) REFERENCES of_engine_owner(account_id,fan_id) ON DELETE RESTRICT,
      CONSTRAINT fk_of_engine_offer_grant_account_id_of_text_intent FOREIGN KEY(account_id,used_intent_id) REFERENCES of_text_intent(account_id,intent_id) ON DELETE RESTRICT,
      CONSTRAINT fk_of_engine_offer_grant_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
      CONSTRAINT fk_of_engine_offer_grant_item_id_catalog_items FOREIGN KEY(item_id) REFERENCES catalog_items(id) ON DELETE RESTRICT,
      CONSTRAINT uq_engine_offer_grant_terms UNIQUE(account_id,fan_id,item_id,terms_hash)
    )""",
    'ALTER TABLE of_engine_decision ADD CONSTRAINT fk_of_engine_decision_account_id_of_engine_offer_grant FOREIGN KEY(account_id,offer_grant_id) REFERENCES of_engine_offer_grant(account_id,grant_id) ON DELETE RESTRICT',
    """CREATE FUNCTION of_engine_offer_grant_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'offer authorization evidence is retained'; END IF;
      IF ROW(NEW.account_id,NEW.grant_id,NEW.fan_id,NEW.binding_id,NEW.item_id,NEW.terms_hash,NEW.coverage_ref,NEW.expires_at,NEW.created_at)
         IS DISTINCT FROM ROW(OLD.account_id,OLD.grant_id,OLD.fan_id,OLD.binding_id,OLD.item_id,OLD.terms_hash,OLD.coverage_ref,OLD.expires_at,OLD.created_at)
         OR (OLD.used_intent_id IS NOT NULL AND NEW.used_intent_id IS DISTINCT FROM OLD.used_intent_id)
      THEN RAISE EXCEPTION 'offer authorization identity is immutable'; END IF;
      RETURN NEW;
    END $$""",
    'CREATE TRIGGER of_engine_offer_grant_guard BEFORE UPDATE OR DELETE ON of_engine_offer_grant FOR EACH ROW EXECUTE FUNCTION of_engine_offer_grant_guard()',
    """CREATE TABLE of_engine_access_evidence (
      account_id VARCHAR NOT NULL, evidence_key VARCHAR(128) NOT NULL, intent_id VARCHAR(36) NOT NULL,
      fan_id BIGINT NOT NULL, message_id BIGINT NOT NULL, media_json TEXT NOT NULL, status VARCHAR(16) NOT NULL, applied BOOLEAN NOT NULL,
      evidence_ref VARCHAR(128) NOT NULL, occurred_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_engine_access_evidence PRIMARY KEY(account_id,evidence_key),
      CONSTRAINT fk_of_engine_access_evidence_account_id_of_text_intent FOREIGN KEY(account_id,intent_id) REFERENCES of_text_intent(account_id,intent_id) ON DELETE RESTRICT,
      CONSTRAINT ck_of_engine_access_evidence_status CHECK(status IN ('confirmed','revoked'))
    )""",
    """CREATE FUNCTION of_engine_access_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'access evidence is append-only'; END $$""",
    'CREATE TRIGGER of_engine_access_immutable BEFORE UPDATE OR DELETE ON of_engine_access_evidence FOR EACH ROW EXECUTE FUNCTION of_engine_access_immutable()',
    """CREATE TABLE of_fans_connection (
      account_id VARCHAR NOT NULL, epoch BIGINT NOT NULL, binding_id VARCHAR NOT NULL,
      provider_account_id VARCHAR(255) NOT NULL, provider_creator_id VARCHAR(255) NOT NULL,
      status VARCHAR(16) NOT NULL, created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_fans_connection PRIMARY KEY(account_id,epoch),
      CONSTRAINT fk_of_fans_connection_account_id_of_runtime FOREIGN KEY(account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT,
      CONSTRAINT fk_of_fans_connection_binding_id_of_creator_bindings FOREIGN KEY(binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT,
      CONSTRAINT ck_of_fans_connection_status CHECK(status IN ('active','retired') AND epoch > 0)
    )""",
    "CREATE UNIQUE INDEX uq_fans_connection_active ON of_fans_connection (account_id) WHERE status = 'active'",
    "CREATE UNIQUE INDEX uq_fans_provider_account_active ON of_fans_connection (provider_account_id) WHERE status = 'active'",
    """CREATE FUNCTION of_fans_connection_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'connection evidence is retained'; END IF;
      IF ROW(NEW.account_id,NEW.epoch,NEW.binding_id,NEW.provider_account_id,NEW.provider_creator_id,NEW.created_at)
         IS DISTINCT FROM ROW(OLD.account_id,OLD.epoch,OLD.binding_id,OLD.provider_account_id,OLD.provider_creator_id,OLD.created_at)
         OR (OLD.status = 'retired' AND NEW.status <> 'retired')
      THEN RAISE EXCEPTION 'connection identity is immutable'; END IF;
      RETURN NEW;
    END $$""",
    'CREATE TRIGGER of_fans_connection_guard BEFORE UPDATE OR DELETE ON of_fans_connection FOR EACH ROW EXECUTE FUNCTION of_fans_connection_guard()',
    """CREATE TABLE of_fans_webhook (
      account_id VARCHAR NOT NULL, epoch BIGINT NOT NULL, event_key VARCHAR(255) NOT NULL,
      event_type VARCHAR(80) NOT NULL, payload_sha256 VARCHAR(64) NOT NULL, raw_body BYTEA NOT NULL,
      status VARCHAR(16) NOT NULL, received_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_fans_webhook PRIMARY KEY(account_id,epoch,event_key),
      CONSTRAINT fk_of_fans_webhook_account_id_of_fans_connection FOREIGN KEY(account_id,epoch) REFERENCES of_fans_connection(account_id,epoch) ON DELETE RESTRICT,
      CONSTRAINT ck_of_fans_webhook_status CHECK(status IN ('accepted','quarantined'))
    )""",
    """CREATE TABLE of_fans_webhook_conflict (
      account_id VARCHAR NOT NULL, epoch BIGINT NOT NULL, event_key VARCHAR(255) NOT NULL,
      payload_sha256 VARCHAR(64) NOT NULL, raw_body BYTEA NOT NULL, received_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_fans_webhook_conflict PRIMARY KEY(account_id,epoch,event_key,payload_sha256),
      CONSTRAINT fk_of_fans_webhook_conflict_account_id_of_fans_webhook FOREIGN KEY(account_id,epoch,event_key) REFERENCES of_fans_webhook(account_id,epoch,event_key) ON DELETE RESTRICT
    )""",
    """CREATE FUNCTION of_fans_webhook_conflict_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'webhook conflict evidence is append-only'; END $$""",
    'CREATE TRIGGER of_fans_webhook_conflict_immutable BEFORE UPDATE OR DELETE ON of_fans_webhook_conflict FOR EACH ROW EXECUTE FUNCTION of_fans_webhook_conflict_immutable()',
    """CREATE TABLE of_engine_attempt (
      account_id VARCHAR NOT NULL, attempt_id VARCHAR(36) NOT NULL, intent_id VARCHAR(36) NOT NULL,
      outcome VARCHAR(16) NOT NULL, payload_hash VARCHAR(64) NOT NULL, evidence_json TEXT,
      started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
      CONSTRAINT pk_of_engine_attempt PRIMARY KEY(account_id,attempt_id),
      CONSTRAINT fk_of_engine_attempt_account_id_of_text_intent FOREIGN KEY(account_id,intent_id) REFERENCES of_text_intent(account_id,intent_id) ON DELETE RESTRICT,
      CONSTRAINT uq_engine_attempt_identity UNIQUE(account_id,intent_id,attempt_id),
      CONSTRAINT ck_of_engine_attempt_outcome CHECK(outcome IN ('dispatching','unknown','accepted','not_accepted'))
    )""",
    'CREATE INDEX ix_engine_attempt_intent ON of_engine_attempt (account_id,intent_id)',
    """CREATE FUNCTION of_fans_webhook_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'webhook evidence is append-only'; END $$""",
    'CREATE TRIGGER of_fans_webhook_immutable BEFORE UPDATE OR DELETE ON of_fans_webhook FOR EACH ROW EXECUTE FUNCTION of_fans_webhook_immutable()',
    """CREATE FUNCTION of_engine_attempt_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'engine attempt evidence is retained'; END IF;
      IF ROW(NEW.account_id,NEW.attempt_id,NEW.intent_id,NEW.payload_hash,NEW.started_at)
          IS DISTINCT FROM ROW(OLD.account_id,OLD.attempt_id,OLD.intent_id,OLD.payload_hash,OLD.started_at)
          OR (OLD.outcome IN ('accepted','not_accepted') AND ROW(NEW.outcome,NEW.evidence_json) IS DISTINCT FROM ROW(OLD.outcome,OLD.evidence_json))
          OR (OLD.outcome = 'unknown' AND NEW.outcome NOT IN ('unknown','accepted','not_accepted'))
      THEN RAISE EXCEPTION 'engine attempt identity is immutable'; END IF;
      RETURN NEW;
    END $$""",
    'CREATE TRIGGER of_engine_attempt_guard BEFORE UPDATE OR DELETE ON of_engine_attempt FOR EACH ROW EXECUTE FUNCTION of_engine_attempt_guard()',
    '''CREATE FUNCTION of_engine_decision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'engine decision evidence is retained'; END IF;
      IF ROW(NEW.account_id,NEW.decision_id,NEW.request_id,NEW.fan_id,NEW.binding_id,NEW.owner_epoch,NEW.connection_epoch,
             NEW.input_revision,NEW.control_revision,NEW.action,NEW.offer_grant_id,NEW.catalog_item_id,NEW.terms_json,NEW.body,NEW.payload_hash,NEW.snapshot_hash,NEW.created_at)
         IS DISTINCT FROM ROW(OLD.account_id,OLD.decision_id,OLD.request_id,OLD.fan_id,OLD.binding_id,OLD.owner_epoch,OLD.connection_epoch,
             OLD.input_revision,OLD.control_revision,OLD.action,OLD.offer_grant_id,OLD.catalog_item_id,OLD.terms_json,OLD.body,OLD.payload_hash,OLD.snapshot_hash,OLD.created_at)
         OR (OLD.intent_id IS NOT NULL AND NEW.intent_id IS DISTINCT FROM OLD.intent_id)
         OR (OLD.status = 'completed' AND NEW.status <> 'completed')
         OR (OLD.status = 'unknown' AND NEW.status NOT IN ('unknown','completed','held'))
      THEN RAISE EXCEPTION 'engine decision identity is immutable'; END IF;
      RETURN NEW;
    END $$''',
    'CREATE TRIGGER of_engine_decision_guard BEFORE UPDATE OR DELETE ON of_engine_decision FOR EACH ROW EXECUTE FUNCTION of_engine_decision_guard()',
]


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)
    for table in ('of_engine_owner', 'of_engine_request', 'of_engine_decision', 'of_fans_connection', 'of_fans_webhook', 'of_fans_webhook_conflict', 'of_engine_attempt', 'of_engine_offer_grant', 'of_engine_access_evidence'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'REVOKE ALL ON {table} FROM PUBLIC')
        for role in ('anon', 'authenticated'):
            op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON {table} FROM {role}; END IF; END $$")


def downgrade():
    conn = op.get_bind()
    for table in ('of_engine_access_evidence', 'of_engine_offer_grant', 'of_engine_attempt', 'of_fans_webhook_conflict', 'of_fans_webhook', 'of_fans_connection', 'of_engine_decision', 'of_engine_request', 'of_engine_owner'):
        if conn.execute(sa.text(f'SELECT 1 FROM {table} LIMIT 1')).first():
            raise RuntimeError('Preserve engine request and decision evidence; use a compatible rollback')
    op.execute('DROP TRIGGER of_engine_access_immutable ON of_engine_access_evidence')
    op.execute('DROP FUNCTION of_engine_access_immutable()')
    op.drop_table('of_engine_access_evidence')
    op.execute('DROP TRIGGER of_engine_attempt_guard ON of_engine_attempt')
    op.execute('DROP FUNCTION of_engine_attempt_guard()')
    op.execute('DROP TRIGGER of_fans_webhook_conflict_immutable ON of_fans_webhook_conflict')
    op.execute('DROP FUNCTION of_fans_webhook_conflict_immutable()')
    op.drop_table('of_fans_webhook_conflict')
    op.execute('DROP TRIGGER of_fans_webhook_immutable ON of_fans_webhook')
    op.execute('DROP FUNCTION of_fans_webhook_immutable()')
    op.drop_table('of_engine_attempt')
    op.drop_table('of_fans_webhook')
    op.execute('DROP TRIGGER of_fans_connection_guard ON of_fans_connection')
    op.execute('DROP FUNCTION of_fans_connection_guard()')
    op.drop_table('of_fans_connection')
    op.execute('DROP TRIGGER of_engine_decision_guard ON of_engine_decision')
    op.execute('DROP FUNCTION of_engine_decision_guard()')
    op.drop_table('of_engine_decision')
    op.execute('DROP TRIGGER of_engine_offer_grant_guard ON of_engine_offer_grant')
    op.execute('DROP FUNCTION of_engine_offer_grant_guard()')
    op.drop_table('of_engine_offer_grant')
    op.drop_table('of_engine_request')
    op.drop_table('of_engine_owner')
