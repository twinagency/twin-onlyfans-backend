-- Durable reason a reply input is blocked, and a routine-poll marker. Additive only.
ALTER TABLE of_work ADD COLUMN block_reason VARCHAR(48);
ALTER TABLE of_work ADD COLUMN block_phase VARCHAR(16);
ALTER TABLE of_work ADD COLUMN block_input_revision INTEGER;
ALTER TABLE of_runtime ADD COLUMN poll_pending BOOLEAN NOT NULL DEFAULT false;
