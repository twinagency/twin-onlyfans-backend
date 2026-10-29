-- For a partially installed V1 candidate only. Run before enabling V2 workers.
-- These columns already exist when installing the current schema.sql from empty.
ALTER TABLE of_profile_learning ADD COLUMN IF NOT EXISTS queued_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE of_profile_learning ADD COLUMN IF NOT EXISTS last_completed_at timestamp;
ALTER TABLE of_profile_learning ADD COLUMN IF NOT EXISTS lease_token text;
ALTER TABLE of_profile_learning ADD COLUMN IF NOT EXISTS lease_until timestamp;
ALTER TABLE of_profile_learning ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;
ALTER TABLE of_profile_learning ADD COLUMN IF NOT EXISTS maintenance_at timestamp;
