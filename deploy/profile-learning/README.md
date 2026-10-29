# OnlyFans dated profile memory

## Release status, 29 October 2026

The API, FASTT prompt integration and a separate supervised learner are deployed.
Jesse explicitly authorised this task's direct safe deployment, overriding the
earlier PR-only restriction. All deployment commands used IAM user jesse-gpt.
No AWS root or IAM changes were used. Backups and rollback containers remain.
Reply activation, fan permissions, allowances, model selection, caps and sales
rules were not changed. Only the profile read/reply integration flag was enabled.

Application code revision: `8a912627879e2d59d65fe6cca0b21a78a5b2634a`.

| Component | Live image | Preserved parent |
| --- | --- | --- |
| API and independent learner | 3d2825f5cfe8 | 3e6e4579fefd |
| FASTT sender | 1fb5e52bc4d3 | f542dedd0ac5 |
| Sync worker, separately deployed | 946e343a07d2 | Preserved |
| Dashboard | 3e7aff70aa8d | Preserved |

The first release used the originally observed API b338c0c and sender f234c641.
A concurrent sync deployment changed all backend images. The cutover guard
refused the outdated candidate. The final upgrade verified that the three memory
modules were unchanged, then layered only those files onto the new API and sender
images. Do not deploy this PR's entire source tree over newer runtime work.
`runtime-overlays.json` records original integration changes and current parent
images/content guards. `upgrade-current-runtime.py` records the guarded cutover.

A schema-only backup was saved on the host before additive DDL. The evidence
lookup index was built concurrently and verified valid. Profile tables and the
message-change trigger are installed; the existing Alembic head remains
0098_ledger_exclusion. No sender/control/payment table was rewritten.
The learner has restart-unless-stopped supervision, two concurrent calls, a
512 MiB memory limit and bounded logs; the active compose definition includes it.

## Implemented behaviour

* Separate persistent facts, changing circumstances, events and outstanding
  requests, with exact message evidence, dates, account/tenant/binding identity,
  review dates, status and manual protection. Repeated canonical facts merge;
  clear updates supersede, ambiguous contradictions remain uncertain.
* Relative dates use the original message timestamp and known IANA timezone.
  Passed plans have unconfirmed outcomes. Ordinary events leave active context
  fourteen days after their end. Unresolved dates get a review period. Cancellation
  and postponement are explicit. Future career plans are not current employment.
* Known supported birth dates calculate age at read time, including leap-year
  boundaries. Age-only statements retain their date and become uncertain ranges.
  Unconfirmed jobs/locations become potentially stale after ninety days.
* A 250-word summary, structured memories and a separate observation archive.
  Replies select at most ten memories, normally eight, under a 6,000-byte budget.
  Names, occupations and enduring interests survive temporary chatter and large
  request archives. At most three requests occupy the compact presentation;
  outstanding requests remain stored until fulfilled, cancelled or superseded.
* Current-binding evidence checks and the same dynamic projection serve the
  existing inbox, FASTT hook and native planner. Dates are re-evaluated on every
  read. Preceding questions accompany short answers. Unverified creator account
  messages remain distinct from verified backend facts and manual information.
* Persistent per-chunk receipts process long messages without 1,400-character
  truncation. A message is complete only after every chunk commits. Edits,
  withdrawal and changed question context invalidate evidence.
* Resumable full-roster discovery, silent-fan account information, active-thread
  priority, leased jobs, account fairness, retries, two-hour refresh scheduling,
  daily maintenance and original queue-age measurements. Restart maintenance
  preserves failure retry deadlines. Incoming/imported messages queue new work.
  Pending edits/refreshes are displayed as processing, not fully analysed history.
* Stored-history learning uses each account's saved matching credential and
  existing budget independently of reply activation and OnlyFans session expiry.
  Purchase, subscription and spend information remain with canonical OF records.

## Verification

Ninety focused cases passed together, including seventeen real PostgreSQL cases.
After the final career-plan correction, sixty-two evidence/date cases passed,
covering three additional cases: ninety-three unique focused cases in total.
The earlier current-release integration also passed sixty-four existing regression
cases and twenty-six native prompt checks. Compilation and whitespace checks pass.

PostgreSQL coverage includes claims/locks, restart leases, transaction rollback,
first-budget reservation races, cap refusal, long chunks, edited/withdrawn evidence,
manual correction/tenant isolation, refresh state, a 301-memory request archive,
and actual native/FASTT context construction. Full-roster tests discover 1,001 fans
and process all 505 fans in a separate silent-roster fixture. Test PostgreSQL was
local and disposable and has been stopped. No synthetic fan message was sent.

Authorised real-history extraction used eight stored messages for one fan each
on Nicky, Naomi and Maile, through their saved OpenRouter/DeepSeek credentials.
It correctly excluded promotional spam. Review found and fixed relative-place
confusion, birthplace/current-residence conflation and request-heavy presentation.
A read-only canary on existing history verified the final API-parent image,
native prompt, FASTT hook, expired-memory filtering and 6 KB context limit.
The authenticated live profile endpoint returns HTTP 200; the live inbox repository
returns the new summary/status/coverage rows. The sample summary was 99 words and
selected eight memories. No model-generated reply was sent as a test.

A creator-facts freshness guard refused two canaries while the existing cache was
stale. Automatic approval review rejected an additional forced cache refresh;
that refresh was not executed. A materially safer read-only check passed after the
normal source updater refreshed the cache. The freshness guard was retained.

## Backfill and outstanding acceptance

Last recorded live snapshot: 68,364 discovered fans, 68,358 queued, 281 processed,
68,083 pending and six deferred fan profiles. Counts keep changing as historical
import discovers fans. Queue lag was about 56 minutes. These are actual processing
counts, not a claim of complete provider history or complete fleet backfill.
Every creator still reported incomplete provider history. Two hours is the refresh
schedule; completion within two hours has not been established.

Alva and Katie lack account-specific saved model credentials. Their current saved
model selections also require owner review. No other account's key was copied.
Naomi and Maile creator-draft jobs have deferred analysis errors and will retry;
Nicky's creator job has processed. These jobs are not reported as completed.

Browser visual acceptance remains pending because the available browser is at the
CRM sign-in screen. A dedicated structured-memory visual editor has not been added;
review/correction and historical pagination are API capabilities. Superseded source
observations are archived, but deeper old-version retrieval beyond materialized
history still needs acceptance and further work. Exact ISO birth-date evidence is
supported; unsupported date forms remain unaccepted/uncertain rather than invented.
Real-sample checks are bounded and do not establish fleet-wide extraction quality.

## Operations and rollback

Existing profile endpoints are under the account's configuration-purpose auth:
`/history-memory/profiles/progress`, `/history-memory/fans/{id}/profile`,
`/history-memory/fans/{id}/profile/history`, and versioned memory corrections at
`/history-memory/fans/{id}/profile/memories/{key}/corrections`.

The running learner is `twin-onlyfans-profile-learner`. Runtime/private snapshots,
compose backups and stopped rollback containers are retained on the same host.
Restore only the changed API/sender/learner generation after a fresh version check;
preserve newer sync work and all counters/credentials. Disable profile read context
and stop the learner if needed. Retain observation tables, source history and budget
audits. Do not drop evidence or delete backups. Reconcile the repository/main and
normal pipeline before replacing these pinned runtime derivatives in a later release.
