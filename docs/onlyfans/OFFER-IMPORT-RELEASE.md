# Offer import + content sync: release procedure and prepared live checks

Prepared, not executed. Every live step below needs its own explicit
authorization from Ronan/Jesse at the time it runs. Nothing here enables an
account, an item, a chat or outgoing authority.

## Candidate

- Backend `codex/onlyfans-offer-import` (and later `codex/onlyfans-launch-integration`
  once Chats 1-4 are wired). Record the exact SHA and the tested image digest
  from the `OnlyFans offer import and content sync acceptance` workflow
  (`candidate-image-id.txt`).
- Dashboard `codex/onlyfans-offer-import`; record its SHA and build digest.
- Schema: `0089_offer_import` (and `0090_launch` when integrated). Single head.

## Before release (read-only unless stated)

1. Reconfirm prod: API/worker image digests, `alembic_version` (expect 0088),
   connection status/expiry, pending or unknown delivery intents
   (`of_text_intent` status not terminal), account send/mode flags, catalogue
   counts, recovery/ledger scan state. Save the snapshot.
2. Run `bash scripts/check-onlyfans-offer-import-integration.sh --dashboard-dir <abs path>`
   and the CI workflow on the candidate SHA. Zero failures except the two
   documented pre-existing ones; zero skips.
3. Encrypted database backup through the established mechanism; rehearse
   restore + quarantine on a disposable database.
4. Record a release lock (one deployer).

## Maintenance deployment (needs explicit go)

Old binaries refuse a newer schema, so API and worker move together.

1. Keep outgoing authority disabled. Leave `ONLYFANS_OFFER_IMPORT_ENABLED`,
   `ONLYFANS_CONTENT_SYNC_ENABLED` unset (worker does no import/sync work).
2. Stop `twin-onlyfans-api-1` and `twin-onlyfans-worker-1` cleanly. Do not
   touch other containers on the shared host.
3. Preserve ambiguous transport evidence; never replay unknown sends.
4. `python -m service.db.schema upgrade --url-env ONLYFANS_MIGRATION_URL` once,
   then `python -m service.db.schema check --url-env ONLYFANS_MIGRATION_URL`.
5. Start API and worker on the tested digest. Verify `/healthz`, schema check,
   unchanged account/conversation permissions and model-call counters.
6. Release the dashboard build (offer import panel, content v2 editor,
   content sync status).
7. Queued intents created before the release keep their byte-identical dynamic
   snapshots (fixed-only snapshot fields); no quarantine is needed for dynamic
   items. If any fixed item existed (none at 0088), quarantine its queued intents.

## Rollback

Do not roll the image back after the schema bump. Cancel import jobs, keep
imported drafts disabled or withdraw them with the audited command, turn sync
settings off, ship a forward fix. Never discard observations, decisions or
sync evidence to start an old binary. Backup restore is an emergency data
operation with delivery reconciliation, not a feature rollback.

## Prepared live checks (each separately authorized)

| # | check | needs | proves |
|---|---|---|---|
| L1 | Read-only inventory over prod history; compare counts with a fresh SQL count | worker flag on for one account, no session use | inventory coverage is honest |
| L2 | Refresh 3-5 representative sources (one photo, multi-video, mixed bundle) | verified session, `ONLYFANS_OFFER_IMPORT_ENABLED=1` | real previews/lockedText captured; terms match the creator's account view |
| L3 | Create reviewed disabled drafts from L2 | dashboard, manager actor | exact price/attachments/previews persisted; nothing enabled |
| L4 | Paid-post read contract: one bounded GET of the creator's posts, redacted shape | session read, manual-mode window | posts adapter contract (B2) |
| L5 | Enable automatic sync for the one account (`POST /settings`) | `ONLYFANS_CONTENT_SYNC_ENABLED=1` | 300s cadence, coverage, no extra provider polling |
| L6 | Authorized media upload | an unpublished upload path (B1 decision) | provider mapping on the correct account, no post/fan message |
| L7 | One controlled paid message to an agreed test recipient at a fixed price | explicit send authorization + test recipient | exact amount/attachments/previews on the wire and in the receipt |
| L8 | Test purchase of L7 | explicit purchase authorization | ledger → ownership update (depends on Chat 1 ledger fix) |

Report each as passed/failed with evidence; never label fixture results as
provider verification.
