-- Default only: existing conversation states and audit evidence are preserved.
ALTER TABLE of_conversation_state ALTER COLUMN mode SET DEFAULT 'paused';
