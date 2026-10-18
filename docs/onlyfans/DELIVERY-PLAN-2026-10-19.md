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

The ledger draft was integrated in `0084_native_ledger`; shared creator facts
were added in `0085_shared_creator_facts`; audited vault edits now bring the
schema head to `0086_vault_edits`. Runtime
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


## Shared facts and reporting checkpoint

The source implementation now reads Twin's existing typed creator row through
its existing private admin API, preserves explicit OF overrides, exposes source
freshness and inheritance in Settings, and checks the same effective facts at
planning and egress. The native prompt continues to use Fastt's existing fact
composer, with full bounded typed limits. Telegram behavior keeps its old
composer defaults. The rollout remains default-off until the private source URL
and existing admin bearer are configured on the actual deployment.

Campaign detail reports now expose 24-hour observed replies and exact
message-linked native purchases/refunds/cleared gross USD revenue. Freshness and
coverage failures have explicit unavailable reasons. Reply counts are not causal
attribution and refunds do not count as cleared revenue. The paired contracts
are `of-configuration.v2`, `of-campaigns.v2`, `of-workspace.v2`, and
`of-operations.v3`; frontend and backend must be deployed together.

Still to implement: native upload admission/worker/preview delivery, wider inbound selling behavior and complete audience
import. The existing upload owner uses temporary
scheduled carrier posts and cleanup evidence; no bypass into the old provider
client has been added. These remaining items are not described as live or done.
Actual selected-account provider formats, credentials and deployment access
remain required for live acceptance.


Verification for this continuation: shared-facts/configuration/workspace behavior
checks passed, after correcting a source-reader test and an older migration
fixture and rerunning their failed cases successfully. The populated 0078-to-current and 0084-to-current
migration checks preserve existing rows. The additional isolation check now
passes without importing the model adapter at API startup; the route inventory
explicitly lists the existing campaign/content/connection command surfaces.
The 52 admin-API checks, four affected frontend contract checks, TypeScript and
production Next build pass. Campaign reporting passed its five focused PG cases
and the broader campaign/media suite. The later vault/earnings checkpoints below
record the completed local browser acceptance. Live provider/model deployment
remains a release gate.


## Vault editing checkpoint

Head `0086_vault_edits` adds immutable command evidence only. Assets retain
`VaultItem`; folders retain `VaultFolder`/`VaultFolderItem`. The legacy editor
and native adapter share tag normalization and the operator override/lock/compat
projection in `vault_ai_effective`. No second media catalogue, folder owner,
classifier or provider sender was created. A source refresh with identical
facts now also preserves the original JSON and does not record a false edit.

The native dashboard can edit asset descriptions/tags and create, rename,
archive, add/remove and filter internal folders. Commands carry account/actor,
nonce and fingerprint fences, retain historical retry acknowledgements and
refuse stale edits. All folder membership references must belong to the account;
archiving preserves membership records. These folders do not publish OF lists.
One selection feeds organization and product curation. Cached/unready assets
can be organized, but a catalogue product still requires verified readiness.
Folder management targets and the library's current filter are separate UI
choices so a new empty folder does not hide the assets that must be added.

Account connection controls now remain accessible when conversation history is
stale. Ownership/authentication and backend connection checks still apply;
chat-history freshness is not a prerequisite for configuring a connection.

Verification: nine focused database/edit/migration cases passed; 85 vault,
shared-fact, ledger-upgrade and egress-isolation regression cases passed. New
vault-edit contracts and existing vault contracts passed. The complete browser
flow passed through real Next/FastAPI handlers and disposable PG, including
shared-fact inheritance, metadata lock/normalization, exact lost-ack retries,
internal-folder membership/filter/rename/archive, readiness holds, account
onboarding, one-use manual/worker sends, campaigns, Operations, session clearing
and mobile layout. TypeScript and the production Next build passed. These are
synthetic local acceptances, not live OF/model/production deployment results.

Both saved Desktop Commander connections remain offline. A selected actual OF
account/session, reachable deployment and configured private Twin source are
still needed for provider-shape validation and the live pilot. No outgoing
activation or production allowance was changed by these checks.

## Combined earnings and onboarding checkpoint

The Overview now reads the existing Twin revenue cache and the native canonical
ledger for one exact reporting window. The cache exposes the window it already
uses; this adds no second rollup, background job or financial record owner.
Periods are 1W, 1M and 3M using the existing Sydney calendar/DST resolver.
The read-only native route is `GET /onlyfans/api/v1/earnings?start=...&end=...`,
under the existing read-token and `of-preview.v2` envelope. Its nested contract
is `of-earnings.v1`; the dashboard combines it as `combined-earnings.v1`.

Telegram shows recorded Stars and the existing configured USD payout estimate.
OnlyFans shows cleared gross USD and recorded net only when every included
cleared row supplies net. It also shows pending/refunded counts and account
coverage. No fee percentage, revenue share or combined mixed-basis grand total
is inferred. OF numbers require current, complete native ledger evidence for
the requested window and all authorized accounts. Missing, stale, held,
unsupported or unverified coverage returns unavailable values, not zero.
The read selects no sealed credentials and never contacts the provider.
The combined operator API rejects agency/view-as access and clears private
values on session expiry.

The full browser run exposed an existing account-selection race after
registration. URL correction now consults the current request state instead of
an older rendered account list; a deliberately delayed response verifies that
the newly registered account and connection editor remain selected.

Verification: five focused earnings PostgreSQL cases, the earnings contract
checks, five existing revenue-window cases and the actual HTTP integration
passed. The complete browser/API/worker/PG flow then passed, including earnings
periods, unavailable-versus-zero values, access/session checks, mobile layout
and delayed onboarding. No real revenue, provider, model or fan was used.

Before frontend release, integrate this branch with current main using a normal
three-way merge or rebase. Main has newer viewer-specific revenue cache keys
and platform-cut terms. Preserve those changes together with the new
`reportWindow` metadata; do not overwrite main with this older feature checkout.
A three-way merge of the cache file was rehearsed without conflicts and retained
both `viewerCacheKey(access)` and `reportWindow`. Post-merge verification and
selected-account live acceptance remain required.
