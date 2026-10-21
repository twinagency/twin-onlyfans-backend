# Offline recovery checks — 22 October 2026 (Brisbane)

## Current account evidence
Read-only snapshot at 2026-10-21 21:35 UTC: of-176774528, labelled Jesse Creator Test, verified at 21:28 UTC with no last_error; renewable expiry 2026-10-22 03:28 UTC. Enrollment accepted. Outgoing runtime disabled. This is the only active binding found.
Discovery completed after 1195 pages; last complete account sync remains null and recovery_needed remains true.
Later aggregate: one complete history, 32 running, 12472 needed; untouched histories all generation zero, running histories up to generation 287. Canonical data contained 31 fans, one chat, three messages, three applied ingress events.
Connection recovery does not establish complete inbox import or identify the cause of each failed provider history request.

## Queue fix
Existing ordering preferred the lowest fan ID among never-completed histories even after repeated expired attempts. A failed low-ID history could starve untouched work.
Order eligible histories by generation (attempt count), then prior completion and fan ID. Existing account locks, revision validation, lease tokens, high-water/cursor state and outgoing gates remain intact. No schema change.
Added a synthetic PostgreSQL regression and included it in hosted PostgreSQL smoke checks.
This change is prepared and tested, not deployed by this checkpoint.

## Offline validation
- Exact running Python 3.12 image 9dd1223db428, isolated internal Docker networks, disposable PostgreSQL 16, no production credentials or provider access.
- Baseline regression failed as expected: second claim repeated fan101 instead of untouched fan102.
- Patched regression plus existing foundation suite: 24 passed (SSM 9d8a43a0-a71e-457e-8cbc-7d38c489c25f).
- Renewal, read-only inbox, enrollment and encrypted recovery run: 61 passed, two restore failures with unmatched distribution-default PostgreSQL clients (23d2c279-2d93-4bea-80b6-f99776ab4d99).
- Both restore failures passed when rerun with matching PostgreSQL 16.15 pg_dump/pg_restore (3589d95f-aab1-475a-8cda-bf46b47b1c8f). Use matching client binaries for recovery rehearsal.
- Test containers and disposable networks are removed by completion traps; no production restart or restore.

## Other launch evidence
Dashboard main CI39674693869 passed. Deployment39674693697 still fails its catalogue gate:1309/1313 previews passed.
Read-only source diagnosis f415b464-5031-4106-af07-5a70a34a2c40 confirms all four recorded Briana files are missing moov metadata under both current and wider format probes. Originals remain intact; no unsupported repair, replacement or audit suppression.
No live fan messages, provider uploads, purchases/refunds, email invitations or new login verification were initiated.

## Remaining acceptance
Deploy the queue fix through the guarded backend release process, then observe normal history progress without forcing a provider request. Fair ordering does not guarantee provider requests succeed.
Complete history import, actual recipient-side messaging/media/payment acceptance, real invitation delivery, production backup scheduling/offsite retention and the four damaged originals remain separate gates.
