# OnlyFans paid-offer import

Turns existing creator paid messages into **disabled, fixed-price** catalogue
drafts with the exact selected source price, attachment order and free
previews. Import actions never send, plan, call a model or enable anything.

## Flow

1. **Scan imported history** (`mode=inventory`). Reads only `messages` already in
   this database (outgoing, priced, with media), keyset by `(fan_id, message_id)`
   up to a frozen ceiling, 1,000 rows per committed batch. Groups by unordered
   attachment set and keeps every price and order variant. No provider session
   needed. Reported as "imported history scanned", never "full account sync".
2. **Refresh selected source details** (`mode=refresh`, at most 50 existing
   sources). The worker re-reads those chats through the existing native chat
   history GET (no new endpoint), at most 200 pages per chat and one page per
   account every 5 seconds, and records `of_offer_observation` rows with the
   real `previews` and `lockedText`. A source not reached is `unresolved`, never
   verified or deleted. Ordinary inbox recovery also records observations.
3. **Review** one exact observation: price, attachments in source order,
   free-preview badges, unknown notices, conflicts and blockers.
4. **Create drafts** (at most 20 per request, independent result per item). The
   backend takes amount, media and previews only from the observation. Each item
   is a catalogue single with `enabled=false`, `pricing_mode='fixed'`.

## Supported and blocked shapes

Supported: one photo, several photos, one or several videos, mixed paid
photos and videos, explicit photo free previews, 1 to 20 attachments, USD
300 to 20,000 cents.

Blocked with a visible reason (evidence retained, nothing reduced or guessed):
unknown previews, unknown lockedText, `lockedText=true`, video free previews,
audio/GIF/other kinds, missing or unready vault assets, more than 20
attachments, out-of-range price, every attachment a preview.

## Fixed selling

`native_sales.choice()` returns the saved amount for fixed items and never runs
quote arithmetic. `catalog_terms()` adds `pricing_mode` and the import source
to the frozen snapshot **only for fixed items**, so existing dynamic snapshots,
queued intents and campaigns stay byte-identical. Any override must equal the
saved amount. Accepted automated fixed offers write a `LadderQuote` with
`kind='fixed'` (counts as an ask, no band, ladder rung unchanged).

## Contracts

- `of-offer-import.v1` at `/onlyfans/offer-import/v1/accounts/{id}` with header
  `X-OnlyFans-Offer-Import-Version`. Content-purpose credential and tenant.
- `of-content.v2` at `/onlyfans/content/v2/...`: v1 fields plus `pricingMode`,
  `provenance`, `priceOverridden`. A price change away from the source amount
  needs `acknowledgeOverride: true`. v1 keeps its shape and refuses imported
  price changes or enabling with `version_required`.

## Schema

Revision `0089_offer_import` (parent `0088_native_vault_media`). Additive:
`catalog_items.pricing_mode` (default `dynamic`) and five `of_offer_*` tables.
Observations and decisions are append-only (checked at startup). Job inputs
are frozen by trigger.

## Deployment

The worker only runs import jobs with `ONLYFANS_OFFER_IMPORT_ENABLED=1`.
Coordinated API/worker maintenance: stop both, apply
`python -m service.db.schema upgrade --url-env ONLYFANS_MIGRATION_URL` once,
start both new binaries, verify `check`, then release the dashboard.

## Rollback

An old binary refuses 0089. Do not roll the image back after the schema bump.
Stop or cancel import jobs, keep imported drafts disabled or withdraw them
through the audited withdraw command, and ship a forward fix. Evidence is never
discarded to start an old image.

## Paid feed posts (`offer_post_source.py`)

Contract verified read-only on a real creator session, 23 Oct 2026:
`GET /users/{owner}/posts?limit=10&order=publish_date_desc&skip_users=all&format=infinite&pinned=0&counters=0`
with JSON body `{}` (an empty body is refused with 400 "Invalid JSON request").
Response `{list, hasMore, headMarker, tailMarker}`; a paid post has an int `id`,
`postedAt`, decimal `price`, ordered `media` and `preview` (list of media ids
shown free). Posts have no `lockedText`; feed text is public, recorded as an
explicit `false`. Free posts are not offers. Older pages use
`beforePublishTime=<tailMarker>` (web-client convention; only page 1 was read live).

Observations use `source_kind='post'`, `source_context='feed'`, no fan. The
worker (`ONLYFANS_CONTENT_SYNC_ENABLED=1`, account sync on) reads at most 20
pages per account every 300 s, newest first, and stops at already-known posts.
A malformed page is refused whole and shows partial coverage. Drafts from posts
go through the same reviewed, disabled, fixed-price creation.
