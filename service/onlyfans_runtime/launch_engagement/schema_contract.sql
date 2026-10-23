-- Proposed migration fragment, owned here for review only. Claude integrates
-- into the single migration chain AFTER creating the three tables from models.
-- Never executed by runtime code. Dedicated tests use disposable databases.
ALTER TABLE of_engagement_settings ADD CONSTRAINT of_engagement_settings_runtime
  FOREIGN KEY (account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT;
ALTER TABLE of_engagement_settings ADD CONSTRAINT of_engagement_settings_binding
  FOREIGN KEY (binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT;
ALTER TABLE of_engagement_settings_change ADD CONSTRAINT of_engagement_change_runtime
  FOREIGN KEY (account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT;
ALTER TABLE of_engagement_settings_change ADD CONSTRAINT of_engagement_change_binding
  FOREIGN KEY (binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT;
ALTER TABLE of_engagement_action ADD CONSTRAINT of_engagement_action_runtime
  FOREIGN KEY (account_id) REFERENCES of_runtime(account_id) ON DELETE RESTRICT;
ALTER TABLE of_engagement_action ADD CONSTRAINT of_engagement_action_binding
  FOREIGN KEY (binding_id) REFERENCES of_creator_bindings(id) ON DELETE RESTRICT;
ALTER TABLE of_engagement_action ADD CONSTRAINT of_engagement_action_fan
  FOREIGN KEY (account_id, fan_id) REFERENCES fans(account_id, fan_id) ON DELETE RESTRICT;
ALTER TABLE of_engagement_action ADD CONSTRAINT of_engagement_action_intent
  FOREIGN KEY (account_id, intent_id) REFERENCES of_text_intent(account_id, intent_id) ON DELETE RESTRICT;

CREATE FUNCTION of_engagement_settings_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'engagement settings evidence is append-only'; END;
$$;
CREATE TRIGGER of_engagement_settings_immutable BEFORE UPDATE OR DELETE ON of_engagement_settings_change
  FOR EACH ROW EXECUTE FUNCTION of_engagement_settings_immutable();

CREATE FUNCTION of_engagement_action_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'engagement actions are retained'; END IF;
  IF ROW(NEW.account_id, NEW.action_id, NEW.binding_id, NEW.fan_id, NEW.kind,
         NEW.anchor_message_id, NEW.command_key, NEW.request_hash, NEW.snapshot_json,
         NEW.snapshot_hash, NEW.due_at, NEW.expires_at, NEW.created_at)
     IS DISTINCT FROM
     ROW(OLD.account_id, OLD.action_id, OLD.binding_id, OLD.fan_id, OLD.kind,
         OLD.anchor_message_id, OLD.command_key, OLD.request_hash, OLD.snapshot_json,
         OLD.snapshot_hash, OLD.due_at, OLD.expires_at, OLD.created_at) THEN
    RAISE EXCEPTION 'engagement definition is immutable';
  END IF;
  IF OLD.intent_id IS NOT NULL AND NEW.intent_id IS DISTINCT FROM OLD.intent_id THEN
    RAISE EXCEPTION 'engagement intent link is immutable';
  END IF;
  IF OLD.cancel_requested_at IS NOT NULL AND
    ROW(NEW.cancel_requested_at, NEW.cancelled_by) IS DISTINCT FROM ROW(OLD.cancel_requested_at, OLD.cancelled_by) THEN
    RAISE EXCEPTION 'engagement cancellation is retained';
  END IF;
  IF OLD.status IN ('accepted','not_accepted','cancelled','expired') AND NEW.status IS DISTINCT FROM OLD.status THEN
    RAISE EXCEPTION 'terminal engagement cannot be reopened';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER of_engagement_action_guard BEFORE UPDATE OR DELETE ON of_engagement_action
  FOR EACH ROW EXECUTE FUNCTION of_engagement_action_guard();
