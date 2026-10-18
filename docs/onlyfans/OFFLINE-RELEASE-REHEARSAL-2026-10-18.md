# Offline release rehearsal — 18 October 2026

Database head is still `0078_operator_controls`. This checkpoint extends the
native workspace checkpoint with one reusable synthetic scenario, an updated
one-command demo/smoke runner, and historical/recovery/capacity acceptance.
No production import, retirement or activation is claimed.

## Run the current product offline

Use sibling backend/dashboard checkouts with the cumulative patches applied.
Tested runtime: Python 3.12, Node 24, PostgreSQL 16. Install backend dependencies
from `requirements-onlyfans-test.txt`, dashboard dependencies with `npm ci`, and
a Playwright Chromium browser. Select a local disposable PostgreSQL server on
which the test user may create/drop uniquely named test databases. Export its
URL as `ONLYFANS_TEST_PG_URL`; no production URL or provider credentials belong
in this environment.

From the backend root, this is the one smoke command:

```sh
.venv/bin/python service/tests/onlyfans_demo.py --dashboard ../twin-dashboard --smoke
```

Omit `--smoke` to inspect the UI interactively on loopback port 3018. The runner
prints a fresh local login link; Ctrl-C stops both servers and drops only its
newly-created `of_demo_<random>` database. `--port` selects another local port.
The smoke command exercises the actual browser workflow and exits after cleanup.
Install the browser/dependencies before running; the runner does not download
packages or connect external services.

The runner selects the dedicated role before project imports, excludes inherited
provider/dashboard/database credentials from child environments, and refuses
remote test URLs or `.env` files in the isolated Next harness. It generates its
own temporary read/control/session credentials and uses the real HMAC session
gate. The loopback test-login route exists only in the isolated harness. The
production dashboard does not mount it. Temporary logs/screenshots are removed.

The same neutral scenario feeds the existing full HTTP acceptance and demo:
accepted priced media, an unknown send without a transcript bubble, source-backed
synthetic purchase/conflict evidence, saved catalogue and creator settings,
separate currency/status transactions and an unpaid open offer. There is no
production fixture fallback, actual provider connection or paid model call.

## Historical import and rollback evidence

Rehearsal cases verify that an existing canonical historical message can be
replayed without duplication or reply work; old catch-up messages and explicit
import events remain history, including an imported timestamp newer than the
activation cutoff. Disabled processing cannot claim new work. Arming processing
and granting a new owner do not replay the blocked/imported turn; only a fresh
eligible input can be claimed. Existing no-contact revision/unknown evidence
and the activation cutoff survive restart.

This is the typed Event/Store import boundary. A translator from an actual old
journal/database/export has not been accepted here. The map below defines what
must survive that later import; it does not invent unavailable source identities.

| Data family | Canonical owner | Required preservation |
|---|---|---|
| Tenant/creator/account identity | Existing identity service and OfCreatorBinding | Proven account mapping, active binding and revocation; never infer identity from a display name |
| Fan/message identity | Fan/Message keyed by account and provider fan/message ID | Provider identities, directions, timestamps, retractions, actual media and prices; Telegram identifiers are not interchangeable |
| Intake and recovery | OfIngress, OfRecovery, OfScan, OfRuntime | Payload provenance, gaps/cursors, pending counts and activation cutoff; import is history, not a trigger |
| Consent and ownership mode | OfConversationState, Fan.is_muted, OfControlAudit | Revisions, no-contact/mute, takeover, attention and unresolved pointers; unknown/missing consent starts held |
| Native facts and summaries | Fan, FanProfile, OfNativeMemoryChange | Corrections, source quotes/IDs and before/after provenance; retain unrelated profile fields |
| Native model calls and plans | GrokCall/GrokDailyCost, OfNativePlan | Known and uncertain cost, reservation, input membership and frozen context; no regeneration of an old input |
| Delivery and allowances | OfTextIntent, OfMessageDelivery, OfDeliveryPolicy/Change | Exact final payload, one-use marker, reservations, receipts, consumed units, pacing and immutable policy evidence |
| Catalogue and media | VaultItem, CatalogItem | Exact scoped identities, descriptions, previews and prices; legacy scripts stay historical |
| Offers and ownership | ContentOffer, VaultSend, Message.is_paid | Accepted terms and purchase/reversal evidence; mutable catalogue edits cannot rewrite previously sent files |
| Financial records | Transaction and OfCommerceEvidence provenance | Source currency/status/transaction identity, gross/net/fees and reversal ordering; no offer-to-payment inference |
| Runtime and external writers | Deployment owner and explicit process/provider controls | Preserve disabled state; no old scheduler, official-app or external writer may silently compete after cutover |
| Credentials | Existing secret-management owner | No secret values are imported, copied or invented by this offline rehearsal |

The full-evidence restore rehearsal includes scoped no-contact control audit,
uncertain delivery, synthetic paid/reversed/conflicting commerce, model cost,
and a real same-call native fact correction in a separate synthetic account.
Encrypted roundtrip retains every public record verbatim. The target stays
quarantined even if its release-version row is manually changed. Restoring onto
the occupied source refuses and cannot overwrite newer evidence. Existing crash,
corrupt-archive and newer-send rollback tests remain required acceptance.

## Writer exclusion register

| Writer class | Current evidence | Remaining gate |
|---|---|---|
| Common native text/existing-media delivery | One-use authority; scoped synthetic and owned-loopback transport tests | Real adapter/receipt semantics not connected |
| Legacy application routes and schedulers | Dedicated role refuses legacy server startup before dependencies | Actual old deployments have not been stopped or retired |
| OFClient generic mutations | Constructor/factory and preconstructed-client checks; POST/PUT/PATCH/DELETE fail before signing in dedicated roles | An unset-role old deployment remains independent |
| Converter and multipart uploads | All four raw mutation sinks and two raw session construction sites reviewed; each retry rechecks the role | No live provider-write conversion implied |
| Newly registered legacy automation | Cached-client/factory egress tripwires and raw-sink inventory | Any new raw sink requires review and an isolation test |
| Legacy model entrypoint | Dedicated roles refuse paid legacy chat; native rehearsal accepts only the explicit synthetic model type | Real model credentials/provider validation not performed |
| Official app, other integrations, provider-scheduled posts/messages | Unverified; outside this code's process boundary | Inventory and coordinate these explicitly before any activation |
| Telegram gateway/runtime | Preserved, outside the OF sender path | Do not retire, migrate or reroute it as an OF side effect |

The local register is supported by source/behavior checks. It is not a claim
that external writers or old live processes were inventoried through account
access. Their status remains an activation hold, not a default zero.

## Measured synthetic rehearsal

Current evidence records three bounded scenarios. Intake/claim fairness covers
12 accounts, 192 conversations and four processes, followed by an actual local
database connection outage. Hot-account overload retains the 1,000-event pending
bound, returns backpressure for excess input, recovers 1,025 canonical messages
and proves a cold account progresses first.

The new native path processes 192 conversations through native composition,
one synthetic model invocation each, existing cost recording, immutable queued
intents, the real one-second account pacing, one-use synthetic transport and
accepted canonical history. It verifies 192 model calls, 192 transport calls,
192 messages/receipts, 1,344 synthetic millicents and exactly 16 consumed delivery
units out of each account's unchanged 20-unit allowance. Four workers compete.
Timing and limits are recorded in the checkpoint's JSON evidence.

This uses a zero-latency synthetic model and in-process synthetic transport in a
shared ephemeral sandbox, with only the initial quiet window advanced. It is not
a paid-provider latency test, long soak, 200/1,000-creator capacity certification,
production SLO or rollout approval.

## Open gates

Native content/configuration management writes, curated packs, full native
policy/style/semantic acceptance, provider/session lifecycle and authoritative
delivery/financial reconciliation remain open. Production legacy import and
external-writer retirement need accepted source identity/evidence and a separate
live authorization. Production monitoring, capacity/soak and staged cutover also
remain. No commit, push, deployment, real message/model call, live import, cap
reset or retirement was performed.
