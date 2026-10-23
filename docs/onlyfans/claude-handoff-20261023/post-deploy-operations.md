Prepared only. No commands below have been submitted.

Target: `of-176774528`, `@niickybabyfree`, binding
`9ea94bb8-cbcf-5325-926b-dcb1ef13d7b6`, creator
`f80d1add-d10c-4039-90f3-99e6cfc04b91`, tenant `twin-internal`.

Preserve the 1,064 existing native vault assets. The missing layer is reviewed
sellable catalogue entries, not an absent vault. Inventory reads existing stored
priced outgoing message references into durable candidate/source records; it
does not re-upload media, create an enabled product, or send anything.

After accepted native 0091 and matching dashboard deployment, read these native
endpoints using each existing purpose token inside its current service. Never
copy tokens into a request log or another environment.

| Purpose | Header/version | GET path |
| --- | --- | --- |
| API | X-OnlyFans-Schema-Version: of-preview.v2 | /onlyfans/api/v1/accounts/of-176774528/readiness |
| API | X-OnlyFans-Schema-Version: of-preview.v2 | /onlyfans/api/v1/accounts/of-176774528/ledger |
| API | X-OnlyFans-Schema-Version: of-preview.v2 | /onlyfans/api/v1/accounts/of-176774528/ledger/evidence |
| CONFIGURATION | X-OnlyFans-Configuration-Version: of-configuration.v3 | /onlyfans/configuration/v1/accounts/of-176774528/history-memory/progress |
| CONTENT | X-OnlyFans-Content-Version: of-content.v1 | /onlyfans/content/v1/accounts/of-176774528/vault?limit=24&includeSummary=1 |

The ordinary dashboard request gate derives a verified actor from its existing
signed-in session. Use that gate for the following writes; do not manufacture an
`op-session` actor or reuse a previous operator's audit identity.

The signed UI at `/onlyfans/content?account=of-176774528` exposes **Scan imported
history** under **Import existing OnlyFans offers**. The UI at
`/onlyfans/launch/memory?account=of-176774528` exposes **Resume account recovery**.
Clicking these uses the existing verified session without extracting its cookie.
The backend-for-frontend derives `op-session:SHA256(actual session token)` or the
verified agency actor, then derives the durable command key. An uncertain reply
must retain the UI's **Retry the same request** body.

1. Existing-history offer inventory:

   `POST /api/onlyfans-content/accounts/of-176774528/imports/jobs`

   ```json
   {"requestId":"c4d916e8-6117-4a6f-b9bf-27638c1c5893","mode":"inventory","sources":null}
   ```

   Retain this exact body across uncertainty. The gate derives one command key.
   Record `data.jobId`; read
   `/api/onlyfans-content/accounts/of-176774528/imports/jobs/<jobId>` until a
   terminal status is actually observed. Read `/jobs/<jobId>/candidates` for the
   resulting candidates. Acknowledged/pending is not completed inventory.

   Backend equivalent: `POST /onlyfans/offer-import/v1/accounts/of-176774528/jobs`,
   CONTENT purpose, `X-OnlyFans-Offer-Import-Version: of-offer-import.v1`, body
   `{mode:"inventory",sources:null,commandKey:<server-derived>,actor:<verified>}`.

2. Resume durable history catch-up without resetting any cursor:

   `POST /api/onlyfans-launch/memory/accounts/of-176774528/backfill`

   ```json
   {}
   ```

   Backend equivalent:
   `POST /onlyfans/configuration/v1/accounts/of-176774528/history-memory/backfill`,
   CONFIGURATION purpose/version above, body `{actor:<verified>}`.

3. Ledger retry only if the fresh ledger read says `status:"held"`,
   `connectionCurrent:true`, and `leaseActive:false`. The last inspection found
   at12:35:25Z it held at offset40 with `unsupported_ledger_response`, no active
   lease and no completed scan. This response-shape error requires investigation;
   repeated retry alone is not a fix and must not be described as reconciliation.

   `POST /onlyfans/control/v1/accounts/of-176774528/ledger/retry`, CONTROL purpose,
   `X-OnlyFans-Control-Version: of-controls.v1`,
   `X-OnlyFans-Actor: <verified actor>`.

   ```json
   {"commandKey":"<stable SHA256 of this exact verified actor/request>","expectedConnectionRevision":"<integer from fresh ledger read>","expectedScanFingerprint":"<fresh scanFingerprint>"}
   ```

   `expectedConnectionRevision` must be a JSON integer, not a string. This only
   queues the frozen scan; it does not clear evidence/conflicts or reconcile
   transactions. Preserve a busy/refused result and reread state.

Processing prerequisites are separate from queueing: the worker must have
`ONLYFANS_OFFER_IMPORT_ENABLED=1`, `ONLYFANS_HISTORY_RECOVERY_ENABLED=1`, and
`ONLYFANS_HISTORY_MEMORY_ENABLED=1` to process those respective jobs. These are
not enabled by the deployment script. History memory processing does not invoke
a model or send. Keep engagement and carrier/upload enablement separate.

The normal inventory worker handles 1,000 message rows per bounded batch. More
than 410,000 priced outgoing mirrored messages were observed. Its normal
background cadence cannot honestly be described as instantaneous completion;
record actual rows/pages/status and give recent inbound work priority.

After inventory, refresh only exact selected existing fan/message sources to
recover current authoritative prices, attachment order and previews. Drafting
requires reviewed description, reuse confirmation and description approval.
Drafts remain disabled; no price, recipient or purchase budget is invented here.

For later authorized autonomy, preserve all existing paused/no-contact/takeover
states. The connection activation field `automateNewConversations:true` arms
only a future genuinely new fan+conversation with a nonhistorical inbound event
after `automation_starts_at`. It does not resume an existing paused conversation,
including an earlier test fan. Any existing-fan test needs that exact authorized
conversation and its current revision, not mass resume. Existing delivery limit,
throttle/media scope and model budget must be reviewed, not invented.

Account readiness also requires a current approved personality, usable scoped
model credential, available budget, fresh worker/history/ledger evidence and
usable enabled fixed-price catalogue terms. Report those independently from
deployment, and do not use a connection-activation endpoint to bypass the final
readiness gate.
