-- Maintenance preflight before upgrading the pinned candidate.
-- Stop worker/sender admission and verify a backup first.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
ANALYZE public.messages;
ANALYZE public.of_content_sync_source;
ANALYZE public.of_runtime;
ANALYZE public.of_content_sync_account;
ANALYZE public.of_offer_import_job;
COMMIT;
