# Native uploads and full video playback

Paired sources: `jesse98/twin-onlyfans-backend` and `jesse98/twin-dashboard`,
branch `codex/native-media-20261020`. The backend branch builds on the deployed
creator-enrollment source, not the older backend default branch.

## Behavior and boundaries

- Upload one or multiple photos/videos from the native Content vault panel.
  Defaults: 40 files, 4000 MiB per file, 8 MiB chunks, 2048 MiB disk floor.
  File bytes are staged outside the HTTP process heap. Offsets, SHA-256 chunk
  receipts and acknowledged-prefix hashes allow safe resume after lost replies.
- Existing `vault_upload` owns file preparation, batch caps, dedupe, S3 multipart
  protocol, convert claims, far-future carrier posts, pacing and cleanup evidence.
  The native adapter owns only scoped transport. No legacy OFClient is created.
- One upload and one video-preparation job can be active per account. Background
  work is globally bounded to one upload and one video thread in this release;
  neither occupies the chat worker loop. It does not enable outgoing messages.
- A crash expires the job lease into a review hold. It never reposts an uncertain
  carrier. Recovery searches only the exact native job marker and recorded IDs.
  Cleanup rotates across eligible accounts and retained unresolved evidence
  prevents another upload on the same account.
- Full MP4/WebM bytes are downloaded on demand by a worker through the selected
  verified connection's fixed proxy. Browser/API requests cannot supply source
  URLs. Authenticated same-origin playback supports full reads, HEAD and one
  byte range, with private no-store responses. DRM-only/unsupported sources are
  unavailable; the UI never represents their poster as playable video.
- Video caches have 48-hour receipts and worker expiry cleanup. Cached reads use
  the same current owner/binding/provenance checks as permanent stills. A binding
  revocation denies access. An expired provider capture does not erase previously
  cached same-owner bytes, consistent with the existing still policy.

## Verification

Backend tests use a fresh disposable PostgreSQL server and synthetic captures and
transport only. Run:

```sh
ONLYFANS_TEST_PG_URL=postgresql+psycopg2://postgres@127.0.0.1:55439/postgres \
python -m pytest -q service/tests/test_onlyfans_vault_media_pg.py \
  service/tests/test_onlyfans_vault_pg.py service/tests/test_onlyfans_vault_previews_pg.py \
  service/tests/test_onlyfans_provider_adapters.py service/tests/test_onlyfans_worker_entry.py
```

The new cases cover exact lost-ack retry, partial append repair, prefix receipts,
actor/tenant refusals, mixed per-file failures, uncertain carrier cleanup without
reposting, narrow transport admission, a real multipart photo and video protocol,
0087-to-0088 preservation, immutable admission, full/HEAD/suffix/range/416 video
reads, cache expiry and worker preparation. On hosts without ffprobe, the video
probe/preparation dependency is injected; production Docker includes ffmpeg and
ffprobe and the browser test decodes a generated real MP4.

Frontend: `npm run test:onlyfans-media` and `npm run build`.
For component browser checks, start the isolated integration app on loopback,
provide a generated four-second MP4 with `ONLYFANS_TEST_VIDEO`, then run
`scripts/check-onlyfans-vault-media-browser.mjs` with `ONLYFANS_TEST_WEB_URL`.
`PREVIEW_CHROME` can select an installed Chromium executable. These checks cover
committed lost-ack resume, changed-file rejection, individual outcomes, actual
MP4 decode/play/seek, session clearing and 390px/320px layouts. The preview page
and its intercepted synthetic requests are not proof of live provider acceptance.

## Deployment order

1. Record current runtime/browser/dashboard immutable image digests, schema
   revision and outgoing policy. Take and verify the normal PostgreSQL backup.
2. Keep media admission off. Use the existing explicit schema command to upgrade
   0087 to 0088 and run readiness checks. No process performs startup migration.
   The frozen migration adds a table/indexes and admission protection; existing
   account, connection, message and financial records remain unchanged.
3. Build the native Dockerfile from the pinned new commit. It includes ffmpeg.
   Mount the same private persistent spool writable in API and worker. Preserve
   the existing image-cache volume with API read-only and worker writable access.
4. Deploy the pinned API/worker and paired dashboard through the existing owners.
   Preserve the separate sign-in browser and all outgoing policy values. Check
   health and actual running image revisions before enabling media admission.
5. Set ONLYFANS_VAULT_MEDIA_ENABLED=1 on API and worker together. Verify native
   status and Content UI using the signed-in operator route. Select an actual
   account and user-approved test media, then verify one photo, one short video,
   a mixed batch, processing/readiness, full playback/seek, and carrier cleanup.
   No arbitrary creator media is uploaded by the synthetic tests.
6. If acceptance fails, turn media admission off and retain jobs, runs and the
   spool. Do not downgrade/delete the recovery table or replay interrupted jobs.
   Use a schema-compatible prior runtime or the new runtime with the flag off;
   the old 0087-only runtime will intentionally refuse an 0088 database.

Live selected-account acceptance and production rollout are tracked separately
from source/test completion. Do not call the feature live until both are proven.
