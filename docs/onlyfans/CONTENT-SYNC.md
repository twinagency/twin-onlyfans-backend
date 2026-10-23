# Continuous import and two-way content sync (handoff section 15)

Status: being implemented on `codex/onlyfans-offer-import`. Schema lives in the
same unpublished `0089_offer_import` revision (`service/db/onlyfans_offer_import_models.py`,
`service/db/snapshots/0089_offer_import.sql`). Nothing here deploys, enables an
item or account, uploads to a production creator, sends a message or publishes a post.

## Upload path (decision 23 Oct 2026: option b)

OnlyFans has no unpublished "save to vault" call; the existing native uploader
(`vault_upload.py`, `vault_upload_transport.py`, `of_client.materialize_to_vault`)
gets media into the vault through a far-future scheduled carrier post marked
"DO NOT PUBLISH" that is deleted at once (intent recorded first, sweep removes
orphans). Ronan approved using it for CRM Save, per account only:
`content_sync_carrier.transport_for` returns the carrier transport solely for
accounts listed in `ONLYFANS_CARRIER_UPLOAD_ACCOUNTS`. Every other account
keeps the no-transport default: assets show `needs_review` /
`unpublished_upload_unavailable` and the item cannot be enabled.
`content_sync_media` itself still never imports the carrier code. A lost
acknowledgement is reconciled from the vault_upload run receipt (keyed by the
asset's upload token) before any retry. No fan message is sent.

## Module ownership

| file | owns |
|---|---|
| `service/onlyfans_runtime/content_sync.py` | settings, status/coverage, scheduler (initial, 300s incremental, daily reconcile), sync-now, operations list/get/retry dispatch, echo recognition, conflict detection/resolution, sale holds |
| `service/onlyfans_runtime/content_sync_media.py` | Save drafts, chunk staging, atomic commit of draft revision + upload work, upload/verify worker over `SyncTransport`, lost-ack reconciliation, materializing the disabled fixed CatalogItem, asset retry |
| `service/onlyfans_commands/content_sync_api.py` | HTTP contract below; enqueue/read only |
| `service/onlyfans_worker.py` | `_process_content_sync` calling `content_sync.process_due` then `content_sync_media.process_uploads`, behind `ONLYFANS_CONTENT_SYNC_ENABLED=1` |

### Cross-module functions (fixed signatures)

```python
# content_sync_media.py
class SyncTransport(Protocol):
    def lookup(self, *, upload_token: str) -> int | None: ...       # provider media id, None = never landed; raise UploadUnknown if undeterminable
    def upload(self, *, upload_token: str, path: Path, kind: str) -> int: ...  # raise UploadUnknown on lost ack, TransientUpload, AuthRequired
    def verify(self, *, provider_media_id: int) -> str: ...          # 'ready' | 'processing' | 'unusable'
def save_draft(store, tenant, account, *, value: dict, actor: str) -> dict
def put_chunk(store, tenant, account, draft_id: str, file_id: str, index: int, data: bytes) -> dict
def commit_draft(store, tenant, account, draft_id: str, *, command_key: str, actor: str) -> dict
def get_draft(store, tenant, account, draft_id: str) -> dict
def list_drafts(store, tenant, account, *, after: str | None, limit: int) -> dict
def retry_asset(store, tenant, account, operation_id: str, *, command_key: str, actor: str) -> dict
def process_uploads(store, tenant, *, limit=1, stop=None, transport_for=None) -> int

# content_sync.py
def upload_counts(session, account: str, binding_id: str) -> dict   # counts of_content_sync_draft rows by state
def echo_item(session, account: str, binding_id: str, source_id: int) -> int | None
def open_conflict(session, account: str, item_id: int) -> bool
def process_due(store, tenant, *, limit=1, stop=None, now=None) -> int
```

## HTTP contract

Prefix `/onlyfans/content-sync/v1/accounts/{account_id}`, schema
`of-content-sync.v1`, required header
`X-OnlyFans-Content-Sync-Version: of-content-sync.v1`. Same content-purpose
bearer credential and tenant as offer import. Every response is
`{"schemaVersion": "of-content-sync.v1", "ok": true|false, ...}` with
`Cache-Control: private, no-store`. Failures: `{"ok": false, "code", "error"}`
with codes `invalid_sync_request` 400, `unsupported_schema` 400, `unauthorized` 401,
`not_available` 404, `connection_unavailable` 409, `revision_conflict` 409,
`idempotency_conflict` 409, `sync_conflict` 409, `outcome_unconfirmed` 500,
`sync_unavailable` 503. Mutations take exactly the listed keys (unknown keys
refused), bodies <= 128 KiB JSON (chunks: octet-stream <= CHUNK_BYTES).
IDs are strings; money is integer cents; times are ISO-8601 UTC with `Z`.
`commandKey` is 64 lowercase hex; `actor` is validated by `actor_allowed`.

### Status
`GET /status` ->
```json
{"accountId": "...", "sync": {
  "enabled": true, "status": "off|active|paused_auth|blocked", "revision": 3,
  "blockedReason": null, "lastIncrementalAt": "...", "nextIncrementalAt": "...",
  "lastReconcileAt": "...", "nextReconcileAt": "...", "lastFullReconcileAt": null,
  "coveredThroughMessageId": "123", "backlog": 0,
  "initial": {"jobId": "...", "status": "complete", "rowsScanned": 1000, "complete": true} ,
  "coverage": {
    "media":        {"state": "current|delayed|partial|not_started|unsupported", "checkedAt": "...", "detail": "..."},
    "paidMessages": {"state": "...", "checkedAt": "...", "detail": "..."},
    "paidPosts":    {"state": "unsupported", "checkedAt": null, "detail": "Paid-post reading is not verified yet."}},
  "openConflicts": 0,
  "uploads": {"draft": 0, "queued": 0, "uploading": 0, "verifying": 0, "synced": 0, "retrying": 0,
              "connectionRequired": 0, "needsReview": 0, "outcomeUnconfirmed": 0}}}
```
`delayed` = last success older than 2 x 300s; `partial` = a bound or backlog stopped the pass.

### Settings
`POST /settings` `{"enabled": bool, "expectedRevision": int (0 when never set), "commandKey", "actor"}` -> `{"sync": <status.sync>}`.
Enabling starts the initial inventory job for this account only (reusing an
active one), sets ongoing read sync, records actor + revision in
`of_content_sync_decision`. Never changes outgoing authority, chat modes or other accounts.

### Sync now
`POST /sync` `{"kind": "incremental"|"reconcile", "commandKey", "actor"}` -> `{"operation": <op>}`.
Same idempotent bounded work as the schedule; returns the already-active
operation instead of a duplicate; refuses with `connection_unavailable` when
auth is expired.

### Operations
`GET /operations?after=<operationId>&limit=1..50` -> `{"items": [<op>], "nextCursor": "..."|null}`
`GET /operations/{operation_id}` -> `{"operation": <op>}`
`POST /operations/{operation_id}/retry` `{"commandKey", "actor"}` -> `{"operation": <op>}` (only `retryable: true`)

`<op>` = `{"operationId", "kind": "initial|incremental|reconcile|sync_now|upload|retry",
"status": "queued|running|complete|partial|failed|cancelled|outcome_unconfirmed",
"createdAt", "updatedAt", "completedAt", "coverage": {...}, "result": {...},
"lastError": null|"code", "retryable": bool, "draftId": null|"...", "assetId": null|"..."}`

### Conflicts
`GET /conflicts?status=open|all` -> `{"items": [<conflict>]}`
`POST /conflicts/{conflict_id}/resolve` `{"resolution": "accept_source"|"keep_crm"|"disable", "expectedRevision": int, "expectedItemFingerprint": "<64hex>", "commandKey", "actor"}` -> `{"conflict": <conflict>, "itemId": "..."}`

`<conflict>` = `{"conflictId", "itemId", "revision", "status": "open|resolved", "createdAt",
"current": {"priceCents", "mediaCount", "previewCount", "sourceAt"},
"source":  {"priceCents", "mediaCount", "previewCount", "sourceAt", "lockedText"},
"itemFingerprint", "resolution": null|"...", "resolvedAt": null|"..."}`.
While open, new sales of that item are refused by `catalog_terms`; other items are unaffected.

### CRM Save (drafts)
`POST /drafts` `{"commandKey", "actor", "label", "description", "priceCents", "files": [{"fileId": uuid, "name", "size", "sha256": "<64hex>"}], "previewFileIds": [uuid...]}` -> `{"draft": <draft>}` (state `draft` until every file's bytes are staged; files already synced on this binding by hash are reused and need no bytes).
`PUT /drafts/{draft_id}/files/{file_id}/chunks/{index}` octet-stream -> `{"received": int, "complete": bool}`
`POST /drafts/{draft_id}/commit` `{"commandKey", "actor"}` -> `{"draft": <draft>}` (atomically commits the draft revision and upload operations; state becomes `queued`).
`GET /drafts?after=&limit=` -> `{"items": [<draft>], "nextCursor"}`; `GET /drafts/{draft_id}` -> `{"draft": <draft>}`

`<draft>` = `{"draftId", "revision", "state": "draft|queued|uploading|verifying|synced|retrying|connection_required|needs_review|outcome_unconfirmed",
"reason": null|"code", "label", "description", "priceCents", "pricingMode": "fixed",
"itemId": null|"...", "enabled": false,
"assets": [{"assetId", "fileId", "name", "kind": "photo|video", "order", "freePreview", "state", "attempts", "lastError"}],
"readiness": {"mediaSynced": bool, "canEnable": bool, "blockers": ["code", ...]}}`

Save never sends a fan message, never publishes a feed post and never changes
account outgoing authority. `itemId` appears only after every asset is synced
and verified; the CatalogItem is fixed and disabled. Enabling stays the
existing explicit content v2 action and its checks.

### Commands
`GET /commands/{command_key}` -> `{"command": {"commandKey", "recorded": bool, "action"?, ...result}}`

## Schedule

* Incremental: every 300s +/- 30s jitter per enabled `active` account, one
  lease per account. DB-only fold of outgoing priced messages past
  `covered_through_message_id` into the account's initial-job candidates, plus a
  bounded refresh job (<= 50 sources) for new sources without observations.
  Provider reads only through the existing refresh worker (1 extra page per
  account per 5s). No inbox poller, no full rescans.
* Daily reconcile: every 24h, rotating `reconcile_after_item_id` over fixed
  items (enabled + open-conflict first), revalidates mapped media against the
  latest vault observations and queues refresh of each item's selected source
  (bounded). `last_full_reconcile_at` set only when the rotation wraps.
* Lost auth: status `paused_auth`, no new provider work, resume from cursors
  after reconnection. Binding change: old mappings never reused.

## Implementation notes (scheduler / status / conflict / echo half)

* `upload_counts` lives in `content_sync.py` and counts `of_content_sync_draft` rows by state.
* Everything `process_due` does is database work; it enqueues only the existing bounded
  refresh job (<= 50 sources, new sources lacking observations) and the existing vault sync
  job (re-read at most every 240 s). Provider reads stay in `process_refresh_jobs` /
  `process_vault_jobs` (1 extra page per account per 5 s).
* Fold cursor is `message_id`. A message imported late with an older provider id is found by ingestion time
  (`messages.ingested_at`, window `late_through_at`, 120 s overlap) and folded through the same ledger
  (`of_content_sync_source`) the cursor fold and the initial scan use, so every message is counted exactly once
  and nothing older than the moment sync was enabled is re-folded (that history belongs to the initial scan).
* Sources that still need provider details are a durable queue (`of_content_sync_source.refresh_pending`), not a
  by-product of one pass: each pass requests the newest 50, sources beyond the cap or deferred because another
  refresh job occupies the account stay queued, each is requested at most 3 times, and an observation removes it.
* Daily reconcile rotates the priority set (enabled + conflicted) on its own cursor
  (`reconcile_priority_after_item_id`) as well as the whole-catalogue rotation.
* Observations are append-only sightings: an unchanged re-read is not stored again, but a source returning to an
  earlier state after a different one is a new sighting (`occurrence`), so A -> B -> A opens a conflict again.
* Provider reads (refresh pages, paid posts, vault pages) each take the account's read lease and pace
  (`of_provider_read_gate`, default 5 s, `ONLYFANS_PROVIDER_READ_INTERVAL_SECONDS`): one reader per account at a
  time across all workers, and the next reader of that account waits for the pace.
* The carrier upload client authorizes every provider write: account approval, then the asset lease, binding and
  connection revision (`content_sync_media.lease_authority`); without an authority it refuses all writes.
* Conflicts are detected by the scheduler (and held live by `catalog_terms` once sync is on):
  a NEW observation (observed later, different fingerprint) of the selected source id.
  `accept_source` writes price/media/previews with an `of_content_change` audit row and stays
  fixed; `keep_crm`/`disable` leave or disable the item. Holds are `Refused("open_conflict")`
  and `Refused("media_unusable")` (media removed/deleted at the current verified connection).
* Echo: an observed message whose id equals an `OfTextIntent.provider_message_id` (same
  account/binding, with a catalogue item) is skipped by the fold and inventory, never opens a
  conflict, is refused as a draft source, and appears in `GET .../candidates/{id}` observations
  with the additive keys `crmOriginated: true` and `itemId` (absent for other observations).
* Paid-post coverage follows the bounded post reader (`partial` until the history walk reaches the end of the feed). Sync-now returns the active operation instead of
  a duplicate and is refused with `connection_unavailable` when auth is expired.
* Agency scope: `content-sync/v1` is allowed like `offer-import/v1`; POST/PUT need `canManage`,
  the draft chunk `PUT` is the only PUT, and `GET /operations` is readable.
