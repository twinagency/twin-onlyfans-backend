# Canonical campaigns

Campaigns persist operator-authored text, an optional existing paid catalogue
item, a fixed local audience and an optional timezone-qualified start time.
They use individual messages through the same
`TextAuthority` as reactive and manual replies. They do not invoke a model or
the legacy `send_mass_message`, `reply_mass_funnel` or `scheduled_send` writers.

## Operator capabilities

- Create a campaign for explicitly selected conversations or all known local
  fans, bounded to 1,000 recipients. Creating a campaign does not imply a live
  provider connection or enable delivery.
- An all-known audience is a snapshot of canonical local fan rows, not an
  assertion that every OnlyFans subscriber has been imported. Later fan imports
  never silently join an existing campaign.
- Set a start time up to 366 days ahead. No expiring delivery grant is created
  before that time; scheduling remains in PostgreSQL after browser/worker exit.
- Pause, resume or cancel with an expected revision and immutable command key.
  Cancellation stops pending/queued recipients. It cannot retract a message
  whose one-use network marker has already committed.
- Inspect pending, queued, dispatching, accepted, not-accepted, unknown,
  cancelled, refused and skipped recipient counts. Accepted means accepted
  message evidence, not opened or purchased.
- Select an existing enabled paid catalogue item, including an explicitly
  curated multi-file item. The campaign freezes exact ordered media/preview
  identities, USD cents, description, item/file versions and hashes at creation.
  A later catalogue/price/file change holds the campaign instead of silently
  repricing it. Existing purchases are rechecked and owned recipients skipped.

Text-only and existing paid-media campaigns are supported here. Free-media
teasers, provider-native bulk queues/unsend, automated funnel branches and campaign purchase/reply
attribution are not exposed. Reporting returns null for unsupported attribution
instead of inventing zero revenue or crediting every later purchase to a blast.
Paid send acceptance creates an open canonical content offer and unpaid media
send records. It does not create a purchase, ownership or revenue event.

Dynamic legacy pack rows with no fixed media IDs remain disabled. Their native
planner resolves media live and some paths invoke additional models; campaigns
do not call it or create a competing pack resolver. An existing enabled
catalogue item with a fixed multi-file set uses the same common media contract.

## Persistence and dispatch

Revision `0080_onlyfans_campaigns` adds `of_campaign`,
`of_campaign_recipient` and append-only `of_campaign_change`. Creation records
the human command fingerprint, immutable definition hash and sorted audience
hash. Optional paid terms are bounded to 16 KiB and use the existing
`media_delivery.catalog_terms` price/media/preview validation. Supported paid
prices are 300–20,000 cents; previews remain a strict subset, priced videos
cannot be free previews, and media must exist in the same account's vault.
At prepare, admission and final one-use consume, the definition/audience
is compared with the original append-only creation evidence. A recipient has
one stable command key and at most one linked intent. That link is committed
in the same transaction as its intent, preventing a crash gap from generating
another send.

The worker gives canonical incoming reactive work priority, then queues at
most one due campaign recipient per selected account/cycle. The existing
account pacing, allowance, recovery freshness, no-contact/mute, paused state,
takeover, input revision and unresolved-send holds still apply. A refused or
ambiguous attempt is not silently retried. Unknown outcomes retain their send
reservation and account/conversation hold, including after campaign cancel.

Pause prevents new recipients and is rechecked before a queued/granted send
can begin wire I/O. If pause races with an unattempted queued recipient, that
recipient may finish cancelled; resume continues the remaining pending
audience and never regenerates a terminal attempt.

## API configuration

The API uses a separate server-side campaign-purpose credential:

```
ONLYFANS_PROCESS_ROLE=api
ONLYFANS_CAMPAIGN_TENANT=<explicit tenant>
ONLYFANS_CAMPAIGN_TOKEN=<distinct server-only secret>
```

The campaign token must differ from read/control/configuration tokens. Routes
live under `/onlyfans/campaign/v1` and require
`X-OnlyFans-Campaign-Version: of-campaigns.v1`. Create/action requests reject
unknown fields, duplicate JSON keys, non-finite numbers and bodies over 128 KiB.
The create body accepts optional `catalogItemId`; absent/null retains the
text-only contract. Campaign reads add `offer: null` for text or frozen exact
item ID, price, media/preview IDs and snapshot hash for a paid campaign. Public
display labels are bounded; raw descriptions/configuration are not exposed.
The dashboard derives `actor` and body-bound `commandKey` on its authenticated
server boundary. A new compose gets a new browser request UUID; retries retain
that UUID and exact payload. No service credential belongs in browser code.

List returns the most recent 100 campaigns plus a `truncated` flag. Detail
returns the bounded complete snapshot (at most 1,000 recipients). Reads are
private/no-store, scoped to the currently bound tenant/account and do not mutate
the operation revision.

## Verification

`service/tests/test_onlyfans_campaigns_pg.py` uses disposable real PostgreSQL
databases and synthetic transport only. It exercises schedule persistence,
frozen audience/replay/scope, blocked fans, takeover/input changes, shared
delivery receipts, zero-model worker delivery, prewire pause, cancellation of
queued work, preservation of unknown holds, immutable audit enforcement,
competing worker processes and strict purpose-credential/API parsing.
`service/tests/test_onlyfans_campaign_media_pg.py` additionally exercises paid
price boundaries, foreign vault/item refusal, catalogue edits before fire,
ownership, cancellation while unknown, late receipt projection from frozen
terms and native campaign/manual paid delivery with zero model invocations.

These tests do not establish real OnlyFans delivery or production readiness.
