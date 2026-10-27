# Ledger recovery and parallel work boundaries

## Initial setup status

23 October 2026. Investigation branch created by ChatGPT at Jesse's request. This initial commit documents ownership and verified symptoms. It does not change runtime code or claim a completed fix.

## Verified symptom

At 05:03 UTC, the account connection was verified. The ledger scan was held at offset 40 with unsupported_ledger_response and no completed ledger scan. Account automation was disabled. The existing ledger.py collapses multiple protocol validation failures into this error; the offending response field has not yet been established.

## This branch owns

- Diagnosis and a narrowly scoped correction in service/onlyfans_runtime/ledger.py.
- Dedicated ledger regression fixtures and tests under service/tests, with filenames agreed before editing existing shared suites.
- This investigation note and its evidence updates.

First establish the actual failing response shape through a bounded read-only diagnostic. Do not guess the accepted schema or weaken ownership checks to clear the hold. Preserve exact amounts, transaction identity, creator/fan isolation, reversal handling, idempotency, and cursor advancement only after complete validation.

## Existing handoff owner

Leave offer_terms.py, offer_import.py, offer_import_worker.py, content_sync files, native_sales.py, media_delivery.py, native_connector.py, native_protocol.py, store.py, worker entry points, catalogue creation, migrations, schema registration and import/content dashboard components with the existing offer-import handoff owner. Changes needed in any of these files require an explicit handover before editing.

No new migration or API change is planned for the ledger diagnosis. If one becomes necessary, coordinate its ownership and interface before implementation.

## Integration

Use separate branches and separate working copies. Review the completed offer-import changes once pushed, and use their actual commit SHAs when combining work. Separate files do not guarantee compatible behaviour: test the combined tree for ledger reconciliation, refunds/reversals, duplicate prevention, exact fixed prices and already-owned-content rejection before release. Preserve the single existing 50 percent checkpoint; this note does not add another checkpoint.

Jesse reports Claude's handoff work finished. The accessible 15:09 AEST email describes the original importer as built locally and the two-way sync addition as planned, with neither branch pushed. Treat any newer completion as reported until its commits and tests are available. This note does not claim Claude has received or accepted these ownership boundaries.

Production deployment, migrations, account activation and live sends remain subject to the existing release authorization.

## Recovery contribution, 23 October 2026

### Pins and boundaries

- Assigned branch: `codex/onlyfans-ledger-recovery-20261023`.
- Backend pin supplied by Claude: `dbd09b06dace2c24829ded6bc778d7e6d2ded4f0`,
  branch `codex/onlyfans-offer-import`, based on
  `88e79248e4b504cdaad6cb6c998fad79423e971d`.
- Dashboard pin inspected read-only: `927b2d75105f6180c37f2759ed119f89521b979d`,
  branch `codex/onlyfans-offer-import`, based on
  `244271a36031a9eaf10b1d0ea64f41d97babd80a`.
- The contribution is based directly on the backend pin and retains this
  investigation note. The earlier setup commit
  `e123c80b96c4368d5858b35b22f5ea22ad383ed5` changed
  documentation only. No production state was adopted or modified.
- No applicable `AGENTS.md` or `CODEOWNERS` was present in the checked ancestry
  or backend pin. `CLAUDE.md` was read. `launch_ledger/` and the dedicated
  `test_onlyfans_launch_ledger*` names were absent on both the original branch
  and the backend pin. Claude reported no separate ownership register.
- Only `ledger.py`, `launch_ledger/`, dedicated new tests and this note are
  changed by this contribution. Schema 0089 and all inherited shared changes
  belong to Claude. No dashboard files, shared routes, models, migrations,
  worker entry points, dependency manifests or CI files were edited here.

Contribution file manifest (relative to the backend pin):

```text
docs/onlyfans/LEDGER-RECOVERY-2026-10-23.md
service/onlyfans_runtime/ledger.py
service/onlyfans_runtime/launch_ledger/__init__.py
service/onlyfans_runtime/launch_ledger/validation.py
service/onlyfans_runtime/launch_ledger/diagnostics.py
service/tests/test_onlyfans_launch_ledger_boundary.py
service/tests/test_onlyfans_launch_ledger_contract.py
service/tests/test_onlyfans_launch_ledger_diagnostics.py
service/tests/test_onlyfans_launch_ledger_recovery_pg.py
```

### What is established, and what is not

Claude subsequently supplied a redacted read-only diagnostic through Jesse:
the existing connection, frozen 90-day window and offset 40 returned 20 rows,
`hasMore=true`, with no duplicate identities. Rows 1-14 were valid in-window
transactions (8 PPV messages, 2 paid posts, 4 tips). Rows 15-20 were older than
the requested start boundary and otherwise well-formed. All statuses were
`done`. The top-level keys were `hasMore`, `list`, `marker`, `nextMarker`.
The reported existing parser refused the entire page on its timestamp check.
This establishes the window overrun as the reported failure cause; it was not
an unknown kind, status, currency or amount. We did not independently read the
production account. The report still showed no completed ledger pass.

The demonstrated response-specific correction validates **all 20 rows** with
the same identity, exact-money, status, kind and timestamp checks. Only rows
inside `[start, end]` become observations. An older tail ends the bounded window
(`more=False`) only when the entire page is monotonically newest-first; equal
timestamps are allowed. An interleaved/reordered boundary page, any row newer
than the frozen end, or an invalid field even in the older tail refuses the
whole page. Older rows never become transactions or ownership evidence. The
cursor records 14 applied observations (40 -> 54) on this terminal page; no
further offset GET occurs for this scan. Boundary timestamps remain inclusive.

The exact redacted field shape is reproduced with synthetic values in
`test_onlyfans_launch_ledger_boundary.py`. Provider marker fields remain unused:
changing the shared connector's pagination contract is Claude-owned and is not
necessary for this demonstrated boundary failure. This fix assumes the
reported newest-first endpoint contract and validates its ordering before
using an older tail as completion evidence.

The generic worker error still covers other transport and validation failures;
the bounded diagnostic remains useful for investigating a different failure.
No status mapping, currency, identity, amount, or ownership rule was relaxed.

The following independent defects are demonstrated by synthetic regressions:

1. A held scan reset its frozen window and offset to zero on retry. It now
   resumes the same window/offset while the binding, connection revision and
   owner still match. A changed connection restarts the bounded scan safely.
2. The maximum offset check applied only to nonterminal pages. Every page now
   checks its resulting offset before applying observations.
3. Duplicate-page protection used raw JSON bytes and skipped terminal pages.
   Consecutive equal sets of provider transaction IDs now hold the scan even
   if whitespace, field order, row order, optional fields or `hasMore` change.
   Exact legacy raw hashes are also checked for in-progress scans from the old
   version. A differently serialized legacy page cannot be compared by identity
   retroactively; nonconsecutive page cycles remain bounded by `MAX_OFFSET`.
4. A later unsend or message/offer price edit could veto a refund/reversal of
   an already matched purchase. Exact bound terminal observations now revoke
   that purchase, including same-terminal metadata refreshes. Creator, fan,
   provider transaction ID, original amount/currency/kind/timestamp and any
   existing message binding must still agree. New purchase-link admission
   retains all original checks. Independent purchases of the same media remain
   owned. Conflicting identities remain conflicts; no amount/time heuristic
   supplies missing ownership.
5. A later observation omitting `clearedAt` erased the prior settlement date.
   Missing optional values now preserve that date, as the other optional
   financial fields already did.

Validation errors now carry a static `code`, allowlisted field `path`, and
zero-based `row_index` (or null for page errors), while keeping the existing
generic exception text and worker `last_error`. A whole page still validates
before money or cursor changes; amounts remain exact integer cents.
`pending`, `cleared` and `refund_pending` retain the existing paid/spending
semantics. `refunded`, `chargedback` and `failed` are terminal; stale paid
observations cannot resurrect them.

### Bounded diagnostic for Claude

Run once from Claude's existing authorised worker context, using its existing
`store`, tenant and selected account. Coordinate the read so the regular worker
does not request the same account concurrently. This helper does not itself
pause a worker or acquire a durable provider-read lease. Do not discover or copy
credentials, edit a cursor to force offset 40, activate an account, or call it
from a dashboard polling loop.

```python
from onlyfans_runtime.launch_ledger.diagnostics import collect_diagnostic

report = collect_diagnostic(store, tenant, account, expected_offset=40)
# Return only this report through the agreed private handoff.
```

Preconditions: explicit worker role; allowed tenant/creator binding; current
verified or paused connection; existing held scan whose error is
`unsupported_ledger_response`; exact requested offset; no lease; valid frozen
window. A scope failure performs no provider request. It reads through the
existing encrypted connection and `NativeProviderConnector.transaction_page`:
at most the existing identity GET plus one transaction-page GET, limit 20,
unchanged timeout and 65,536-byte response budget, no retries or page loop.
Database transactions end before the network read. All scan and connection
metadata is checked again afterward; any change discards the result.

The callable writes no rows, takes no recovery action, advances no cursor,
records no purchase, changes no automation and returns no credentials.
`inspect_page(raw, *, owner_id, start, end)` offers the same inspection offline
when Claude already holds an authorised response in memory.

Diagnostic data contract:

```text
schema: "onlyfans.ledger-diagnostic.v1"
valid: boolean                 # complete page passes the existing parser
applied: false                 # always; never reconciliation evidence
error_code: static string|null
structure: null|{
  type, fields, unknown_field_count, row_count?, rows_truncated?,
  rows: [{row_index, type, fields?, unknown_field_count?,
          user_fields?, user_unknown_field_count?}]
}
issues: [{code, path, row_index}]    # at most 20; first parser issue per row
issues_truncated: boolean
collection?: {attempted, snapshot_unchanged}
```

`fields` contains only allowlisted names with `{present, type}`. Unknown names
are counted, never returned. No raw values, IDs, descriptions, names, money,
dates, URLs, headers, cookies, response hashes or exception text are returned.
Page-wide validation still catches duplicate transaction identities. Malformed
or oversized JSON receives no permissive second parse. `diagnostic_read_failed`
means the connector did not return a page; it is not proof of a parser issue.
`diagnostic_state_changed` means discard/recheck through Claude. `valid=true`
does not explain the earlier failure and does not establish a completed scan.

Claude has supplied the matching report described above, so the boundary fix
and regression now have direct evidence. The helper is available for any
further investigation; another live read is not required to finish this branch.
If a later transport failure prevents inspection, additional status/content-type/
size instrumentation belongs in Claude's shared connector. Never return raw
provider error bodies.

### Dashboard read, retry and investigation contract (integration required)

This branch registers **no new HTTP endpoints or dashboard action**. The
following contract is the required integration surface for Claude. The pinned
connection API currently has `save/session/verify/activate/pause` only; `verify`
is not a ledger retry, and the dashboard must not repurpose `activate`.

Proposed authenticated read: `GET /onlyfans/api/v1/accounts/{account_id}/ledger`,
returning `schemaVersion: "of-ledger.v1"`, private/no-store. Reuse the existing
read credential, tenant and creator/agency grant checks. Return only the
selected account, with a non-disclosing scope-unavailable response for denied
accounts. Suggested fields and authoritative sources:

| Field | Meaning/source |
| --- | --- |
| `status` | `not_started` if no scan; otherwise `OfLedgerScan.status` |
| `windowStart`, `windowEnd`, `pageOffset` | The actual frozen query and validated offset; UTC dates |
| `lastCompleteAt`, `lastFullAt` | Recorded provider cutoffs; null remains null |
| `nextPollAt`, `lastError`, `leaseActive` | Recorded retry/error/lease state, static public error codes |
| `connectionCurrent`, `connectionRevision` | Verified current binding/owner/revision, never credentials |
| `conflictCount` | Account-scoped `OfLedgerEvidence.outcome == conflict` |
| `unlinkedPaidCount` | Account-scoped ledger PPV transactions with no message ID and a paid status |
| `requiresInvestigation` | Held validation/pagination failure, conflicts, or unlinked paid purchases |
| `commerceHeld` | Existing `commerce.held` result, not a newly invented release rule |
| `scanFingerprint` | Opaque public CAS token derived from nonsecret scan/connection metadata |

No percentage completion is inferred from page offset. `lastFullAt` covers the
supported 90-day window, not lifetime history. Show "reconciliation pending"
until a scan has actually completed; show its cutoff/freshness independently
of any remaining ownership hold. Do not mix per-account values across creator
switches. Any monetary totals exposed later must use integer cents encoded as
decimal strings with explicit USD, not floating-point dollar arithmetic.

Proposed retry: `POST /onlyfans/control/v1/accounts/{account_id}/ledger/retry`
using the existing authenticated command context, same-origin protection and
agency management permission. Body: `{commandKey, expectedConnectionRevision,
expectedScanFingerprint}`; the trusted gateway supplies the actor. The server
locks the runtime, rechecks scope/current connection and CAS values, refuses a
live lease, and durably records the actor/request/result before scheduling.
Same key and same payload returns the original acknowledgement; changed payload
with that key is a conflict. Acknowledgement is `accepted/queued`, never
`reconciled`. A retry only makes this held scan due; it preserves the frozen
window/offset, last error, immutable evidence and all money/ownership until the
existing worker validates a page. Do not set `status=idle`, erase conflicts or
write `lastCompleteAt`. API requests must not contact the provider. Ordinary
transient reads already retry on the worker's existing five-minute schedule.

Validation/pagination errors need investigation before repeated manual retry.
A historical conflict remains immutable, and an identical conflicted
observation remains a duplicate even after related history later appears.
Retry cannot promise to resolve that conflict. Investigation must distinguish
transport unavailable, unsupported response, conflicting evidence and unlinked
purchase. No "mark paid", ownership override, silent skip or clear-hold action
is part of this contract. A separately designed evidence-resolution operation
would require Claude's shared model/audit integration and tests.

### Exact shared integration work, owned by Claude

1. Integrate this contribution onto the supplied backend pin and run combined
   tests with offer import/fixed pricing/content sync. The ledger change itself
   needs no new migration, schema registration, dependency or worker mount.
2. For dashboard exposure, add the scoped read handler in
   `service/onlyfans_read/api.py` and the retry handler in
   `service/onlyfans_commands/api.py`; reuse their existing security/versioning
   conventions. Persist durable retry idempotency/audit in a shared model only
   after Claude chooses its schema. No suitable ledger command-audit writer is
   introduced here. Keep Retry unavailable until that writer is integrated.
3. Add the corresponding server proxy/types and display in the dashboard,
   based on the pinned `lib/onlyfans/route.ts` and command-context helpers.
   Connection reads currently use exact contract validation in
   `lib/onlyfans-connections/contract.ts`; do not append ledger fields to that
   response without updating its validator. A separate ledger read avoids this
   coupling. Navigation and API mounting remain Claude-owned.
4. Claude has supplied the coordinated diagnostic. During his later authorised
   integration/reconciliation run, verify the worker passes the older-tail page
   and records the actual completed cutoff. A completed scan does not itself
   clear unlinked PPV ownership or historical conflict holds. No live diagnostic
   or reconciliation was run by this branch.
5. Preserve the one combined 50% checkpoint. This document adds no approval
   stage and authorises no merge, deployment, production migration or send.
6. Repair the shared fixture in
   `service/tests/test_onlyfans_ledger_pg.py::test_populated_0083_upgrade_preserves_disabled_state_and_adds_checked_evidence`.
   On the 0089 pin it drops the 0084-0088 additions and stamps 0083, but leaves
   0089 objects behind. The attempted upgrade then fails with
   `DuplicateColumn: catalog_items.pricing_mode already exists`. Before its
   existing downgrade setup, use the same removal already present in
   `test_onlyfans_offer_import_upgrade_pg.py` (test databases only):

   ```sql
   DROP TABLE of_content_sync_decision, of_content_sync_conflict,
     of_content_sync_draft, of_content_sync_asset, of_content_sync_operation,
     of_content_sync_account;
   DROP FUNCTION of_content_sync_decision_immutable();
   DROP FUNCTION of_content_sync_asset_frozen();
   DROP FUNCTION of_content_sync_conflict_frozen();
   DROP TABLE of_offer_import_decision, of_offer_import_candidate,
     of_offer_import_cursor, of_offer_import_job, of_offer_observation;
   DROP FUNCTION of_offer_observation_immutable();
   DROP FUNCTION of_offer_decision_immutable();
   DROP FUNCTION of_offer_import_job_frozen();
   ALTER TABLE catalog_items DROP CONSTRAINT ck_catalog_items_pricing_mode,
     DROP COLUMN pricing_mode;
   ```

   Alternatively reconstruct a genuine 0083 database from the frozen baseline
   and upgrade path. Do not make production migrations silently accept a
   mislabeled database to accommodate this test. This shared test is unchanged
   in the ledger contribution.

### Verification and outstanding work

All automated provider responses are synthetic. PostgreSQL tests create/drop
unique `of_test_*` databases on a dedicated loopback-only disposable cluster;
diagnostic scope tests use temporary SQLite databases. No production database,
account, provider connection or fan was contacted by these tests.

Initial pre-pin run: 43 passed, one warning (242.66 seconds).
Standalone diagnostic/strict-contract run: 77 passed (8.19 seconds).
After Claude's diagnostic, the boundary/strict-contract/diagnostic suites:
99 passed (10.88 seconds).

The first pinned-base combined run (before the boundary correction was added)
finished with **134 passed, 1 failed, 1 warning** (512.45 seconds). The sole
failure was the shared 0083 reconstruction fixture described above. All new
ledger tests and all fixed-price compatibility tests in that run passed.
The final focused run explicitly deselects that one known shared fixture;
its failure remains an integration issue, not a silently reported pass.
**Final focused result on the pinned backend: 158 passed, 1 deselected, 1 warning in 434.37 seconds (7:14).** This includes the corrected 14-plus-6 boundary worker test, complete-page rejection for an invalid older tail, refunds/reversals and metadata refreshes, exact fixed prices and mixed media, duplicate pages, interrupted rollback/replay, cross-creator identity isolation and diagnostic redaction/access checks.

Focused command from the repository root, with `PYTHONPATH` containing
`service` and `service/tests` and `ONLYFANS_TEST_PG_URL` pointing only to a
disposable PostgreSQL server:

```sh
python -m pytest \
  service/tests/test_onlyfans_launch_ledger_boundary.py \
  service/tests/test_onlyfans_launch_ledger_contract.py \
  service/tests/test_onlyfans_launch_ledger_diagnostics.py \
  service/tests/test_onlyfans_launch_ledger_recovery_pg.py \
  service/tests/test_onlyfans_ledger_pg.py \
  service/tests/test_onlyfans_fixed_price_pg.py \
  -q --disable-warnings --tb=short \
  -k 'not test_populated_0083_upgrade_preserves_disabled_state_and_adds_checked_evidence'
```

The final disposable cluster disables fsync/synchronous commit/full-page writes
for test speed. Tests verify transaction rollback, lease fencing and replay;
they do not claim power-loss durability testing. Real runtime durability
settings are untouched. Syntax checks and `git diff --check` also pass.

**Production reconciliation is pending.** The response-specific cause is
supported by Claude's redacted diagnostic, and the correction is tested with
synthetic fixtures. No completed production scan is claimed. Dashboard read/
retry integration and durable investigation resolution are specified, not shipped.
