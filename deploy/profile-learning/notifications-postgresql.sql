-- Install only after schema.sql. No provider calls, controls or sender counters.
-- A fan's profile depends on that fan's INCOMING words only: an outgoing (mass) message neither
-- re-queues the fan nor touches the fan row. The creator draft depends on outgoing messages and
-- is touched only while it is not already due (no hot-row update per mass-message recipient).
CREATE OR REPLACE FUNCTION of_profile_message_changed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a text; f bigint; d text; od text;
BEGIN
    IF TG_OP='DELETE' THEN a:=OLD.account_id; f:=OLD.fan_id; d:=OLD.direction; od:=OLD.direction;
    ELSE a:=NEW.account_id; f:=NEW.fan_id; d:=NEW.direction; od:=CASE WHEN TG_OP='UPDATE' THEN OLD.direction ELSE NEW.direction END; END IF;
    IF d='in' OR od='in' THEN
        INSERT INTO of_profile_learning(tenant_id,account_id,binding_id,fan_id,priority)
            SELECT b.tenant_id,r.account_id,r.binding_id,f,0 FROM of_runtime r
            JOIN of_creator_bindings b ON b.id=r.binding_id AND b.account_id=r.account_id
            WHERE r.account_id=a AND b.channel='onlyfans' AND b.revoked_at IS NULL
            ON CONFLICT(tenant_id,account_id,binding_id,fan_id) DO UPDATE
            SET next_at=least(of_profile_learning.next_at,CURRENT_TIMESTAMP),
                queued_at=least(of_profile_learning.queued_at,CURRENT_TIMESTAMP),
                priority=0;
    END IF;
    IF d='out' OR od='out' THEN
        UPDATE of_profile_learning SET next_at=least(next_at,CURRENT_TIMESTAMP),queued_at=least(queued_at,CURRENT_TIMESTAMP)
            WHERE account_id=a AND fan_id=0 AND (next_at>CURRENT_TIMESTAMP OR queued_at>CURRENT_TIMESTAMP);
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS of_profile_message_changed ON messages;
CREATE TRIGGER of_profile_message_changed AFTER INSERT OR DELETE OR UPDATE OF body,direction,is_unsent,created_at,automation_kind
    ON messages FOR EACH ROW EXECUTE FUNCTION of_profile_message_changed();
