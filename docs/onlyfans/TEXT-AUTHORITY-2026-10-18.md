# Synthetic text delivery authority — 18 October 2026

This is the preceding checkpoint. Continue with
[ONE-USE-TRANSPORT-2026-10-18.md](ONE-USE-TRANSPORT-2026-10-18.md) for the current
0073 head and final network-consumption boundary, which tightens the admission
semantics described below.

This is the next **offline milestone 3 slice**, built on the durable foundation
checkpoint. It proves the plain-text/manual intent, admission, receipt and
repair boundary using a synthetic transport. It does not complete milestone 3,
approve a live adapter or activate an account. The dedicated API and dashboard
remain read-only. The separate native/Telegram migration is unchanged.

The source bases remain backend `b3a7c0015b3691c34de0bd1c059795f924f518ad`
and dashboard `a6539d2ca9f2ad3932cc02444a6b99e9140c5c9a`. No commit, push,
deployment, provider request, real-account write or model call occurred.

## Boundary and source ownership

Read [SENDER-POLICY-REGISTER-2026-10-18.md](SENDER-POLICY-REGISTER-2026-10-18.md)
before connecting a transport. It records 26 fan-send origin groups, 11 other
write groups and 80 public legacy mutators, including hidden restrict-ack
messages, native auto-welcome, remote schedules, carrier posts and raw multipart
uploads. Original code/config coverage was verified against GitHub blob hashes.
The register describes the published source boundary, not the state of deployed
jobs, remote queues, installed clients or the unaccepted native worktree.

`onlyfans_runtime/text_delivery.py::TextAuthority` reuses the existing Store
connection pool, account lock, binding, fan, control and canonical history.
There is no second conversation generator or revenue ledger. The synthetic
transport contains no network implementation; dispatch rejects any other class
or account scope. Its callbacks and result types are explicit test seams, not
evidence that a real provider accepted anything.

`OFClient.__init__` now refuses whenever the `ONLYFANS_PROCESS_ROLE` environment
key exists, including empty/invalid values, before reading session headers or
creating an HTTP session. Legacy deployments without that key retain their
normal behavior and still need retirement/cutover. Constructor isolation is not
proof that all external writers are stopped. The old client must not be reused
unchanged: its proxy and co-tag fallbacks can retry writes or change content.

## Persisted lifecycle and acceptance contract

| Transition | Required behavior |
|---|---|
| Prepare → queued | Immutable text, command identity, actor, scope, binding, control/input/policy revisions and optional planning claim are saved. Same key/same request returns the same intent; changed request refuses. No outbound message or receipt is fabricated. |
| Queued → dispatching | In one account-serialized transaction, recheck scope, owner, exclusion, fresh complete recovery, pending/quarantined history, input/policy versions, expiry, retained unknown receipts, cap and throttle; reserve one unit, install the shared hold and commit. |
| Dispatching → accepted | Require a strictly typed, scoped synthetic receipt matching attempt and exact wire hash. Save the proof first. Canonical transcript/receipt projection is a separate idempotent transaction. |
| Dispatching → not_accepted | Only explicit, validated synthetic negative proof can release the reservation, once. A timeout or ordinary exception is not negative proof. |
| Dispatching → unknown | Timeout, lost response, malformed success, missing identity, expiry or worker loss retains allowance and the hold. No replacement worker sends the attempt again. |
| Unknown → accepted | A late matching receipt may settle the original attempt. No new attempt or transport call is created. A previously retained valid acceptance cannot be overwritten by a rejection or another acceptance. |
| Accepted → projected | Repair creates/recognizes canonical provider identity and a `sent` receipt. Identity/payload/media/receipt conflicts retain evidence and the account hold; they never overwrite history. |
| Queued → cancelled/refused | A stale snapshot, stop, exclusion, disabled account or exhausted cap prevents transport and releases only the queued count; there was no send reservation to refund. |

One unresolved active attempt per account is the deliberate concurrency bound
for this slice. Busy or throttled queued work remains queued. Both origins use
the same cumulative cap and account spacing. Increasing spacing tightens the
existing gate; policy changes cannot reset consumed units, holds, queue counts
or the previous throttle. Admission and preparation use PostgreSQL time after
acquiring the account lock. No transaction spans transport or model work.

Manual intents require explicit current human control. Automated intents require
a current, unexpired planning claim; saving the intent consumes that claim.
Claims capture input revision before planning. New canonical inbound, outbound
or imported messages advance that revision transactionally; duplicate or
quarantined inputs do not. Admission also checks pending input and recovery, so
undrained arrivals cannot be treated as unchanged history. A pre-admission
control change wins; a post-admission stop cannot recall that already admitted
request and blocks later work.

Command fingerprint and exact wire-payload hash are separate. The latter binds
account, fan, exact text, empty media and zero price; it is recomputed before
dispatch and projection. Dispatching payloads are not editable. The internal
actor and policy methods are trusted service interfaces; public command routes
must later derive actor/permissions from authentication rather than browser
fields. No such write route is exposed by this checkpoint.

## Storage and migration map

The explicit lifecycle head is `0072_onlyfans_text_delivery`. Use the existing
`python -m service.db.schema bootstrap|upgrade|check` commands and explicit
`ONLYFANS_MIGRATION_URL` described in the foundation note. Supported declared
revisions are 0064, 0070, 0071 and 0072 after version-aware structural checks.
Checking 0071 now validates its foundation tables rather than skipping them.
Critical intent uniqueness is checked as well as named indexes/constraints.

| Existing fact | Treatment in 0072 |
|---|---|
| Accounts, bindings, fans and provider identities | Retained; new records reference the existing scoped owners with restrictive foreign keys. |
| Canonical messages and receipts | Retained. Nullable receipt `origin` adds reliable `ai`/`human` attribution for new accepted actions; old inference remains. |
| Conversation control and unresolved attempts | Retained. New `input_revision` starts at zero and advances for subsequent canonical writes. Retained unresolved receipts block even if the old control pointer is absent. |
| Durable intake, work and recovery | Retained. First reconnect/backpressure scans reopen completed windows while preserving incomplete lower pages. Claims/completion reject stale input versions. |
| New delivery policy | Absent by default. A policy row has a zero limit, disabled authority and synthetic-only transport. Changes are versioned/audited. |
| New text intents | Queued/dispatching/unknown attempts live outside canonical history until accepted identity exists; unique scoped command, attempt and provider keys prevent duplicate ownership. |
| Existing live allowance/caps | **Not imported or reset.** No new default represents permission to reopen the exhausted native/live allowance. Production import must preserve consumed budgets and unresolved attempts from their accepted source. |
| New indexes | Scoped unresolved receipt lookup and account/provider-message identity lookup avoid history scans in admission/projection. |

The migration is forward-only and creates no policy, intent or activation rows.
The 0070/0071 frozen snapshots are unchanged from the prior checkpoint. Unknown
legacy shapes require a separately rehearsed import; type affinity and named
constraint checks are not certification of arbitrary schema semantics.

## Read path and reproducible evidence

The contract remains `of-preview.v2`; no new browser write capability is enabled.
Accepted canonical receipts render `Sent` and the recorded operator/AI origin.
Unresolved intent holds render an incident without inventing a provider message
ID or sent bubble. Independently scoped receipt lookup also surfaces older
unresolved receipts outside the latest 100 messages, without read-time writes.
Provider acceptance never increments purchase or revenue records.

With the disposable PostgreSQL setup and dependencies in the foundation note:

```sh
python -m pytest service/tests/test_onlyfans_text_delivery_pg.py service/tests/test_onlyfans_foundation_pg.py service/tests/test_onlyfans_role_boundary.py -q
python service/tests/test_onlyfans_read.py
python service/tests/test_onlyfans_migration_pg.py
python -m pytest service/tests/test_onlyfans_http_pg.py -q
```

The tests include competing processes, admission/receipt/projection crash
boundaries, late receipts, immutable conflicting evidence, no-contact/takeover,
input changes and an expiring claim waiting on an actual database lock,
policy/cap/throttle changes, replay, account isolation and canonical conflicts.
The real HTTP/browser rehearsal drives authority-produced PostgreSQL records
through FastAPI and the actual Next handlers/pages: a manual accepted reply is
attributed correctly, timeout displays an incident without a fake bubble, and
session loss removes private content. Transport remains synthetic throughout.

## Next gates

Independent code review found and repaired concrete admission/evidence/recovery
defects in this slice. Final pass counts and logs are in the saved checkpoint.
That review is scoped to these offline paths, not a production approval.

Still required before live text: accept the shared native source/ownership and
complete source-to-target data map; preserve actual consumed caps and unknown
attempts; implement verified connection/session lifecycle and one-request live
transport with trustworthy proof; route or refuse every legacy writer; reconcile
external scheduled work; add authenticated operator command/evidence-resolution
APIs; and rehearse production migration, restore and cutover. A restored ledger
must remain send-disabled until newer provider activity is reconciled.

Native planner extraction, PPV/media dependencies, purchase reconciliation,
production load/soak and authorized live cohort acceptance remain later work.
Do not expose an active Send/Resume UI or construct a live adapter from the
presence of passing synthetic tests.
