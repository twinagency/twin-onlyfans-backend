-- Frozen one-use egress marker; existing intents remain legacy wire version 0.
ALTER TABLE of_text_intent ADD COLUMN wire_version INTEGER DEFAULT 0 NOT NULL;
ALTER TABLE of_text_intent ADD COLUMN wire_started_at TIMESTAMP WITHOUT TIME ZONE;
