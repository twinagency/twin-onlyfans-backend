-- Add evidence links only. Existing budgets, unknowns and controls are retained.
ALTER TABLE of_native_plan ADD COLUMN resolved_model_id VARCHAR(128);
ALTER TABLE of_native_plan ADD COLUMN model_call_id INTEGER REFERENCES grok_calls(id) ON DELETE RESTRICT;
ALTER TABLE of_native_plan ADD COLUMN budget_day VARCHAR(10);
ALTER TABLE of_native_plan ADD COLUMN billing_provider VARCHAR(32);
ALTER TABLE of_native_plan ADD COLUMN reserved_millicents INTEGER;
ALTER TABLE of_native_plan ADD COLUMN charged_millicents INTEGER;
ALTER TABLE of_native_plan ADD COLUMN cost_settled_at TIMESTAMP WITHOUT TIME ZONE;
