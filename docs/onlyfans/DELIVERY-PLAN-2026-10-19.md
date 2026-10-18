# Current OnlyFans integration plan

This plan supersedes earlier remaining-work lists. Historical checkpoint files
describe what was true at their checkpoint, not the current scope or release.

1. Resolve unfinished drafts and use one owner for shared rules and records.
2. Connect existing Twin onboarding/structured facts to existing OF account
   binding and configuration. Record deliberate platform overrides; preserve
   Fastt prompt/model policy and human edits. No second onboarding service.
3. Reuse Twin media controls and Fastt upload/metadata/folder/catalogue logic.
   Add native asset/preview/readiness and USD adapters. Do not apply Telegram
   transformations, Stars or limits to OF. Upload cleanup and retry evidence
   must survive crashes; exact media terms remain frozen at dispatch.
4. Complete agreed native inbound conversation/selling and authoritative
   purchase/refund intake into existing Transaction, Fan, Message, ContentOffer
   and VaultSend records. No heuristic money-to-media ownership claims.
5. Extend existing campaign schedules/sender with audience import and truthful
   reply/purchase/revenue reporting. Complete the combined earnings overview
   with explicit currencies, periods, freshness and conversion basis.
6. Verify actual browser/API/worker flows, migrations, account isolation,
   restart/backup/recovery, then selected-account deployment/provider/model
   acceptance. Static sample data is not a connected deployment.

## Current source work

The ledger draft has been integrated as revision `0084_native_ledger`; runtime
schema readiness, migration metadata and append-only evidence are registered
together. The canonical financial owner remains Transaction. The existing
transaction vocabulary and media-settlement function are shared rather than
copied. No legacy ledger supervisor or sender is started.

`ONLYFANS_LEDGER_ENABLED=1` explicitly enables one bounded transaction GET page
per worker refresh, independently of outgoing-message activation. Defaults off.
The first scan covers 90 days; frequent refreshes cover seven days and a daily
90-day scan revisits older reversals. This is bounded coverage, not an all-time
import or a guarantee about transactions older than 90 days. Offset paging is
not claimed to be a provider snapshot. Limits or unsupported shapes hold the
scan visibly; it never silently skips malformed rows.

The supported parser requires explicit USD money, status, transaction identity
and UTC-normalizable timestamps. Exact provider message references can settle
known matching messages/media. PPV rows without exact message evidence retain
their monetary record but do not grant ownership or campaign attribution;
media sales remain held. Native paid sends also require a completed ledger
scan within 15 minutes; text replies do not require that media permission.
A supported live linkage/evidence path is still a
release gate. No synthetic source revision is assigned to live observations.

Staff invitations, full agency permissions/assignments/reviews and shifts/payroll,
dynamic pack generation, and the wider legacy automation suite are separately
scoped. Existing staff code is reuse material, not a completed Twin staff suite.

## Release state

The initial ledger/commerce/worker/provider/workspace verification passed
143 cases. The vault suite passed 32 cases, with the updated effective-metadata
search case rerun after its final edit. Follow-up admission/lease checks and
native paid-send regression verification are recorded in the continuation.

Twin and OF now share the existing media filter toolbar and input styling. Six
OF command helpers share their low-level one-attempt transport and purpose-token
configuration; their individual contracts, parsers and uncertainty rules remain.
OF local vault search covers existing descriptions, tags, notes and override
metadata with literal query escaping and native media-type filtering.

Frontend TypeScript and production build passed. The six affected command
checks passed. The broader package suite is not fully verified: CLI tsx IPC is
blocked in this environment (the same checks were run through Node’s tsx loader);
some reporting checks require unavailable Supabase configuration, and sequence
checks require absent sibling Telegram files. The Users-tile script also has
source assertions against a different pre-existing page structure. Those are
unresolved full-repository gates, not passes. No live provider,
paid model, fan send or production database has been exercised for this work.
Deployment devices remain offline and no live OF creator has been selected.
