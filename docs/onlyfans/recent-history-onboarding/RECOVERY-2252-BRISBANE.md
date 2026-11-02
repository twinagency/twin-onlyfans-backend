# Maile checkpoint, 2 November 2026, 22:52 Brisbane

The running application remains 91c260ba54451ba51525cd3cc2ab0737bedc1592. No service deployment, activation or image replacement occurred during this checkpoint.

## Memory recovery

Production diagnosis distinguished 13 stored historical model error flags from current work: only four fans still had unprocessed current-window sources; nine did not. The failures were older HTTP 402 OpenRouter insufficient-credit responses, predating this pilot. All 62 current-pilot model calls at diagnosis were done, with no current-pilot failed calls.

Committed operational helper `deploy/recent-history-20261102/retry-recovered-memory.py` defaults to read-only, requires replies disabled and current successful model calls, refuses more than four jobs and any fan with a new pilot failure, and reschedules only pending recent sources. It changes next_at only. Error codes, retry counts, receipts, reservations, caps and controls are preserved. GitHub application/reporting source: a5102ec28b7dab4a415f35f5663160df3850e0c9.

SSM command 5db3f863-75b7-4133-8a63-995e523c658e succeeded: four eligible jobs, four rescheduled, replies false. Readback 19596710-ef7a-47d3-8ed2-ab90499bb931 succeeded at 12:51:52 UTC: 2,839 incoming window messages, 2,839 processed, zero pending. This is readiness of currently stored messages, not completed history coverage. Actual provider-reported current-pilot model cost US$0.0104, active/unresolved current-pilot reservations both zero, cumulative model latency 72.613 seconds. Neither latency nor receipt wall span is total memory-processing duration.

## Coverage and remaining blockers

91,094 stored window messages across 16,573 fans; 7,123 dispatched provider requests; elapsed 11,248.866 seconds from fixed policy start and import still incomplete. Discovery pass zero, 23,897 conversations awaiting successful coverage. Stored cursor errors: 251 protocol, nine timeout, one transient HTTP, two conflicting history. Recent importer logs establish HTTP 404 for some message reads; they do not establish that each thread was deleted, nor successful empty coverage. No requests or cursor failures were erased or treated as complete. Timestamp ordering remains unverified; exhaustive ID traversal is still required by the supported fallback.

All 25 saved personality revisions are drafts, with no structured onboarding or approved sections. Drive search found the Maile full-history folder but no accessible child files or Maile onboarding sheet. General onboarding templates and other creators' sheets are not authority for Maile. Her creator details still need an authoritative supplied document and approval through the existing workflow.

Exact source for the running dashboard remains missing. The backend compatibility protection remains live; dashboard replacement stays blocked. No full-history speedup or fresh onboarding completion time has been demonstrated. Most original sources and receipts were reused, as quantified in the prior baseline comparison.

## Tests and release state

Reporting receipt/scope plus recent policy suite: 23 passed in 52.23 seconds pytest (54.455 wall). After adding the recovery helper test: focused suite five passed in 10.15 seconds pytest (11.504 wall), overlapping the preceding suite. A synthetic test fixture initially omitted required was_dry_run; corrected before the passing run.

The reporting correction counts only failures attached to pending recent sources, separately exposes errorsOutsidePendingRecentWork and preserves historical errors. It is committed and tested but NOT deployed. The operational helper was run from committed source through stdin in the existing worker; no application files were patched. Moxy, concurrent replies, messages, memories, purchases and existing controls were untouched. AI replies remain disabled.
