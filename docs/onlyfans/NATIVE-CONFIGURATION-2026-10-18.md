# Native creator configuration — 18 October 2026

This paired offline checkpoint adds the Settings editor to the existing native
workspace. It updates the existing `AccountAiConfig` owner for a bound account;
it does not introduce another persona, model selector, prompt or fact store.
Database head is `0079_native_configuration`. Read projections remain
`of-workspace.v2` under `of-preview.v2`; edits use `of-configuration.v2`.

## Operator behavior

On Settings, an authenticated internal operator can edit the full bounded
creator persona, the nine existing approved creator-fact fields, the saved
default model and the `ai_chatter` reply override. The editor submits only dirty
fields. Clearing a fact removes that known key. Clearing a reply override removes
only `ai_chatter`, including a legacy explicitly stored null. Other purpose
choices, unknown fact keys, language/voice, sales permissions and all other
configuration columns are retained.

Model choices come from the native static catalogue. Resolution uses the exact
existing explicit saved-model selector, extracted into a provider-free leaf.
Missing, unsupported or null saved choices do not select a house fallback.
An overlong persona is not loaded as an editable excerpt: a replacement requires
entering a complete new value. Corrupt saved JSON refuses a merge; unreadable
fact values remain marked for review. Creator ages must be adult ages, and the
existing native honesty guard also applies to persona changes. This bounded
guard is not a complete semantic or policy acceptance test.

Pending saves disable the form. A successful acknowledgement is followed by a
canonical configuration read and server-page refresh. A stale revision or
legacy-writer fingerprint conflict refreshes the form for review. A lost or
malformed reply remains unconfirmed and exposes an explicit retry of the exact
original request. An idempotent retry acknowledges its recorded historical
change, then refreshes current settings; it does not claim the old values are
still current. Session loss clears private editor data and refreshes the parent
page. Account changes unmount the previous editor.

## Credentials and data boundary

The API requires `ONLYFANS_PROCESS_ROLE=api`, an explicit
`ONLYFANS_CONFIGURATION_TENANT` matching the existing active creator binding,
and a separately provisioned `ONLYFANS_CONFIGURATION_TOKEN`. The token must
differ from both read and control tokens. The dashboard uses that same purpose
token server-side with its existing `ONLYFANS_API_BASE_URL`. An absent purpose
token leaves the existing read-only Settings surface intact.

Native routes are GET/POST `/onlyfans/configuration/v1/accounts/{account}` with
`X-OnlyFans-Configuration-Version: of-configuration.v2`. Dashboard routes are
GET/POST `/api/onlyfans-configuration/accounts/{account}`. Every request verifies
the internal operator session and rejects agency/view-as access. POST additionally
checks exact dashboard Origin/HTTP Host, Fetch Metadata and JSON content type;
requests are bounded to 64 KiB. Service requests omit cookies, refuse redirects,
time out after eight seconds, and never retry automatically. Read/control
credentials cannot use the configuration purpose route. Responses are private
and no-store. No provider credential or arbitrary config JSON is projected.

The dashboard derives `op-session:<sha256>` from the verified session and a
stable hashed command identity from actor, account, expected revision,
fingerprint and partial patch. The browser cannot supply an actor or command
key. The shared-password dashboard does not provide individually named staff
identity; this evidence identifies a verified operator session only.

## Transaction and schema

The existing account runtime lock and active binding check serialize short
transactions. The current `AccountAiConfig` row is locked and compared against
both `OfRuntime.configuration_revision` and a fingerprint of all its columns.
This catches writes from the existing native owner even when that writer does
not update the new revision. A missing configuration row is refused, so the
editor cannot create implicit defaults or financial allowances.

The patch, revision increment and `OfConfigurationChange` evidence commit in one
transaction. Evidence stores changed fields with before/after values, actor,
request hash and fingerprints; it is not another current-state owner. Presence
is recorded separately from null for fact and model-override removal. A matching
idempotency key returns the original recorded revision; conflicting reuse is
refused. UPDATE/DELETE of evidence is blocked by PostgreSQL, and startup checks
the exact enabled trigger/function body. Existing 0070–0078 SQL snapshots remain
byte-identical. The forward-only 0079 migration adds only the revision column,
audit table and immutable-evidence protection; it does not modify saved config,
activate accounts, clear holds or reset caps.

Native plans already fingerprint the same configuration owner. An edit makes an
old prepared/queued/admitted plan stale at the existing queue and final one-use
egress checks. It does not regenerate the same input, call another model, erase
known cost, or reopen consumed delivery allowance. Encrypted backup/restore now
also rehearses the new configuration evidence; the independent restore fence
still rejects startup even after the exact current schema head is restamped.

## Verification and continuation

The checkpoint's `VALIDATION.md` and final logs record PostgreSQL edits, separate
process races, strict credentials, append-only enforcement, historical upgrades,
plan invalidation, actual API/Next/Chromium saves, lost-ack retry, session clearing,
mobile layout, full-evidence recovery and the public demo subprocess. Type checks,
read-boundary checks, egress isolation and comparisons with the hash-verified
Git-base native composer and legacy model selector are retained separately.

The public offline demo remains:

```bash
ONLYFANS_TEST_PG_URL=postgresql://test_user@127.0.0.1:5432/postgres \
  .venv/bin/python service/tests/onlyfans_demo.py --dashboard ../twin-dashboard --smoke
```

It requires installed local test dependencies and an explicitly disposable local
PostgreSQL server. It creates a random owned database, fresh separate purpose
credentials, actual local API/Next/browser processes, and removes its own state
on completion. All models, accounts, histories and transactions are synthetic.
Selecting a catalogue model in this demo does not invoke that provider.

This package closes the current offline configuration milestone. It is not a
live release or completion of all consolidation work. Catalogue uploads/pricing
management, curated pack resolution, full native semantic/policy acceptance,
real model/provider/session and delivery/payment reconciliation, authoritative
legacy import, external writer retirement, production monitoring/capacity and
staged cutover remain open gates. No Git commit/push, remote database migration,
paid model/provider call, real account connection, deployment or cap reset was
performed. Continue from the existing handover and ownership register; do not
replace the native Fastt owner or the common delivery authority.


## Shared Twin facts continuation

Revision `0085_shared_creator_facts` adds a bounded read projection of Twin's
existing creator row. It does not create another creator, Supabase project,
onboarding service, prompt compiler, fan memory or model call. Existing
`AccountAiConfig` remains the native configuration owner. The existing pinned
fact composer renders the mapped typed facts; native calls retain complete
bounded limit lists while legacy callers retain their 240-character default.

`ONLYFANS_TWIN_FACTS_ENABLED=1`, `TWIN_FACTS_BASE_URL` and the existing
`TWIN_ADMIN_API_KEY` enable source reads through Twin's authenticated admin API.
Its bearer and loopback/Tailscale network checks remain in force. The worker
never forges forwarding headers or receives a Supabase service key. Only the
server-bound creator ID and explicitly admitted typed columns are requested.
A two-row limit avoids Twin's conservative `truncated = rows == limit` marker
for this single unique creator row; exactly one owned row is still required.

A leased GET runs outside the database transaction, at most once per 30 seconds
per account, with a five-second timeout and 64 KiB response budget. Effective
facts must have been checked within 120 seconds before AI planning and again
at final dispatch. Unavailable/stale facts hold AI replies without making a
model call. Manual messages and operator-authored campaigns remain independent.
Once an account has a source projection, disabling the flag does not silently
restore a stale independent copy.

Explicit typed DOB derives adult age on the UTC date; cover_story precedes the
retired occupation field. Hometown and current city retain their labels.
Unknown relationship, tattoos, kids and family are not invented. Limits and
preferences map from their existing typed fields. Existing OF facts are
adopted as explicit overrides. An edited field overrides Twin for OF; null
clears it for OF. `inheritCreatorFacts` removes the override. Settings shows
source state, inherited/overridden status and a Use Twin value action. Source
changes use the existing immutable configuration audit and invalidate old plans;
a freshness-only renewal does not generate another reply or invalidate a plan.
