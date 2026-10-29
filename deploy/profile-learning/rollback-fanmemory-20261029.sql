-- Rollback of the 2026-10-29 fan-memory release trigger. Columns/index are harmless to the old learner and are left in place.
-- Install only after schema.sql. No provider calls, controls or sender counters.
CREATE OR REPLACE FUNCTION of_profile_message_changed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a text; f bigint;
BEGIN
    IF TG_OP='DELETE' THEN a:=OLD.account_id; f:=OLD.fan_id;
    ELSE a:=NEW.account_id; f:=NEW.fan_id; END IF;
    INSERT INTO of_profile_learning(tenant_id,account_id,binding_id,fan_id)
        SELECT b.tenant_id,r.account_id,r.binding_id,f FROM of_runtime r
        JOIN of_creator_bindings b ON b.id=r.binding_id AND b.account_id=r.account_id
        WHERE r.account_id=a AND b.channel='onlyfans' AND b.revoked_at IS NULL
        ON CONFLICT(tenant_id,account_id,binding_id,fan_id) DO UPDATE
        SET next_at=least(of_profile_learning.next_at,CURRENT_TIMESTAMP),
            queued_at=least(of_profile_learning.queued_at,CURRENT_TIMESTAMP);
    UPDATE of_profile_learning SET next_at=least(next_at,CURRENT_TIMESTAMP),queued_at=least(queued_at,CURRENT_TIMESTAMP)
        WHERE account_id=a AND fan_id=0;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS of_profile_message_changed ON messages;
CREATE TRIGGER of_profile_message_changed AFTER INSERT OR DELETE OR UPDATE OF body,direction,is_unsent,created_at,automation_kind
    ON messages FOR EACH ROW EXECUTE FUNCTION of_profile_message_changed();
