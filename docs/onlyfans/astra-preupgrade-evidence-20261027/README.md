# Pre-migration backup repair evidence

Runtime/backend: 12c7fed4d15d902e999a4c8b9e45823c3f07d9ab.
Dashboard retained: 444fd1303858e6d87204efe5811278657571f8f6.
[Workflow](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/40490735091).

Passed on this exact backend:

- 42 affected PostgreSQL recovery/schema/release cases: zero failures, errors or skips.
  The two new boundary cases create genuine populated 0092 via the historical
  migration chain plus the pre-upgrade broker schema. They prove encrypted
  public+proxy backup before migration, unchanged source revision/rows, complete
  isolated restoration with runtime/broker quarantine, unsupported and mismatched
  source refusal, then bounded statistics and explicit 0092 to 0096 upgrade.
- Both native suites: results.json records every command, exit and skip outcome.
- Actual retained compatible rollback image: 4 cases passed, zero failures/errors/skips.
  This includes the restricted read API through the immutable rollback image and
  both new pre-upgrade boundary cases. Writers and mutation credentials remain off.
- Changed backend forward image built and retained. Exact Docker image ID,
  source SHA, archive SHA256 and dependencies are in forward-image-metadata.json;
  immutable artifact identity/digest/expiry are in status.json.

## Not yet accepted for release

The six paired acceptance shards refused before collection because CI has neither
an explicitly scoped dashboard reader nor the private dashboard archive capability.
Their red results and unaccepted aggregate are preserved, not classified as passing
application tests. The native and independent boundary/image jobs did complete.
The laptop was offline, blocking the existing authorised private archive handoff
and fresh AWS fingerprints. Reconnect Desktop Commander, reuse the already verified
dashboard 444 source/archive, then rerun all six shards, both isolated scripts and
aggregate against backend 12c7fed4. Do not reassign the old 2,260-case proof to it.
The older proof remains unchanged under commit 345deb97603557ec8af3b25d99e94ed6a84b706f.

[Corrected executable backup commands and release order](https://github.com/twinagency/twin-onlyfans-backend/blob/12c7fed4d15d902e999a4c8b9e45823c3f07d9ab/docs/onlyfans/astra-correction-release-gates.md#pre-migration-backup-correction-astra-follow-up).
Backup defaults to HEAD; pre-migration use must explicitly select
--source-revision 0092_ledger_history --include-proxy. API/worker schema checks,
archive authentication before restore, empty-target refusal, active-revision
suppression and the independent quarantine remain enforced.

## Remaining authorised preparation and approvals

Finish the source handoff and exact-pair verification, refresh actual API/worker/
sender/dashboard source and non-secret configuration fingerprints, and record any
unexplained drift as a stop. Existing rollback packages remain retained and unexpired.

Then obtain confirmation of control of fan 318022380 for account of-176774528 and
explicit authorisation for the accepted bounded packet: one synthetic model draft,
at most one manual text, one existing US$3 item and one AI reply, with fresh real
fan input, current connection/actor/revisions, the saved network/model/persona,
existing allowances and all uncertainty stop conditions. No calls or sends occurred.

Obtain separate approval for the actual deployment window and ordered production
work: stop/fence all writers and admissions, prevent restart/late browser writes,
verify encrypted CRM+proxy backup and isolated quarantine restore plus separate
session-store recovery, statistics preflight with 5-second locks/30-second statements,
explicit migrations 0093 through 0096, broker recovery upgrade, additive session SQL
before dashboard code and default legacy HMAC hard cutover with re-login notice.
Keep outgoing automation disabled until the bounded checks and reviewed rollout
scope are approved. No production mutation, allowance reset, model/persona change,
new features, Moxy/Flirt expansion or scaling work was performed.

