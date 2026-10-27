ALTER TABLE of_ledger_scan ADD COLUMN continuation_json TEXT;
ALTER TABLE of_ledger_scan ADD COLUMN coverage_json TEXT;
ALTER TABLE of_ledger_scan ADD CONSTRAINT ck_of_ledger_scan_continuation CHECK (continuation_json IS NULL OR length(continuation_json) <= 8192);
ALTER TABLE of_ledger_scan ADD CONSTRAINT ck_of_ledger_scan_coverage CHECK (coverage_json IS NULL OR length(coverage_json) <= 2048);
ALTER TABLE of_ledger_history ADD COLUMN continuation_json TEXT;
ALTER TABLE of_ledger_history ADD COLUMN coverage_json TEXT;
ALTER TABLE of_ledger_history ADD CONSTRAINT ck_of_ledger_history_continuation CHECK (continuation_json IS NULL OR length(continuation_json) <= 8192);
ALTER TABLE of_ledger_history ADD CONSTRAINT ck_of_ledger_history_coverage CHECK (coverage_json IS NULL OR length(coverage_json) <= 2048);

-- Offset exhaustion did not establish coverage. Preserve receipts, observations,
-- grants and each frozen window, then revalidate it using the actual marker walk.
UPDATE of_ledger_scan SET last_complete_at=NULL, last_full_at=NULL, page_offset=0,
  last_page_hash=NULL, status='held', last_error='marker_revalidation',
  lease_token=NULL, lease_until=NULL, next_poll_at=CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
  updated_at=CURRENT_TIMESTAMP AT TIME ZONE 'UTC';
UPDATE of_ledger_history SET last_complete_at=NULL, last_full_at=NULL, page_offset=0,
  last_page_hash=NULL, status='held', last_error='marker_revalidation',
  lease_token=NULL, lease_until=NULL, next_poll_at=CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
  updated_at=CURRENT_TIMESTAMP AT TIME ZONE 'UTC';
