# Scoped operator controls — offline checkpoint, 18 October 2026

Schema `0078_operator_controls` adds authenticated local conversation controls
to the existing state owner. It follows the media/commerce checkpoint 0077.
This work uses synthetic identities, disposable PostgreSQL and loopback HTTP.
It does not connect accounts, enable a paid model, deploy or authorize a cutover.

## Behavior

| Action | Canonical result | Holds retained |
|---|---|---|
| Pause replies | `paused`, revision incremented, old work invalidated | All delivery, account and money holds |
| Resume replies | `automated`, only from paused with no takeover or unresolved delivery | Account policy, mute, model budget and money holds |
| Take over | `human_controlled`; effective if quiescent, requested while delivery remains unresolved | An in-flight send retains its receipt obligation |
| Hand back | `automated`, only from effective human control with no unresolved delivery | Account policy and every independent hold |
| No further contact | `no_contact`, old work invalidated | Cannot be reversed through these controls |

These controls do not need a verified provider connection. They operate on
local ownership and stops. Manual sending, delivery-evidence decisions, account
reconnect, pricing, limits and financial reconciliation are not exposed.
Resumption does not rearm an old blocked work item or recreate an old reply.

`Store.control` remains the transactional owner. Runtime/binding locking
serializes commands with delivery admission and receipt projection. Every
command requires the current control revision; matching retries acknowledge
the original immutable event. A later control makes old work unusable. Work
that already crossed the one-use wire boundary remains unresolved until proof.

Accepted receipt projection or proved rejection can finish a requested takeover
when no other unresolved pointer or retained unknown/dispatching receipt remains.
The same transaction records a separate `complete_takeover` audit event and
revision. It never resumes replies, clears attention, or overrides a later
no-contact decision. Uncertainty alone cannot finish the handover.

## Credential and HTTP boundary

The dedicated API mounts POST only under
`/onlyfans/control/v1/accounts/{account}/conversations/{conversation}`.
The existing `/onlyfans/api/v1` router remains GET-only.

The API requires `ONLYFANS_PROCESS_ROLE=api`, an independent
`ONLYFANS_CONTROL_TOKEN`, and an explicit single `ONLYFANS_CONTROL_TENANT`.
Missing configuration, deployment-wide command scope, or reusing the read token
disables controls. The active server-owned binding must match the command tenant
on every request and retry. A revoked binding cannot replay audit evidence.
The read capability projection advertises controls only for this configured
tenant; provider and delivery capabilities remain unavailable.

API startup opens one existing synchronous Store pool for controls when
configured (two connections, no overflow), alongside the existing async read
pool. Blocking transactions run off the event loop. Startup only verifies the
schema; migrations are explicit, and shutdown disposes both pools.

Each request requires `X-OnlyFans-Control-Version: of-controls.v1`, JSON at most
4 KiB, exact keys, a bounded integer revision, an allowed action, a 64-hex command
key and an `op-session:<sha256>` actor. Duplicate JSON keys are refused.
The API takes no cookie, caller-selected tenant, consent override or arbitrary
audit reason. It cannot invoke a provider or model.

The dashboard mounts a separate `/api/onlyfans-controls` POST route. It verifies
the operator session, refuses agency/view-as identities on every request, and
checks exact Origin against HTTP Host/protocol plus Fetch Metadata and JSON.
A reverse proxy must preserve the public Host/protocol. Forwarded Host is not
an alternate grant. The write token stays in the dashboard server; service
requests omit browser cookies and refuse redirects.

The dashboard derives the actor from the verified session token and hashes
actor/account/conversation/action/revision for the command key. A browser cannot
choose the actor or turn a read credential into write access. Existing admin
authentication uses a shared password: this is session attribution, **not a
claim that a named individual was authenticated**. No reusable session token
is stored in the audit.

## Acknowledgement and UI

The acknowledgement uses `of-controls.v1` and identifies the recorded command
and revision. It does not claim to be current conversation state. The dashboard
refreshes authorized accounts and canonical conversation reads after an actual
command, including refusals and uncertainty. It does not change modes
optimistically. Sequence/scope checks discard late results from another view.

A lost reply or malformed acknowledgement is visibly unconfirmed. The explicit
retry uses the original action and expected revision, deriving the same key
under the same verified session. It cannot create a duplicate event. There is
no automatic HTTP retry. Session loss fails the fresh account read and removes
private conversation caches and actions. Preview commands retain their explicit
simulation result; the live adapter never falls back to fixtures.

## Evidence and remaining gates

The checkpoint's validation manifest records PostgreSQL control/concurrency,
populated upgrade, immutable audit, legacy delivery/recovery, native media and
commerce regressions, plus actual API/Next/Chromium acceptance. The browser test
keeps normal web security enabled, deliberately loses a committed acknowledgement,
retries it once, and verifies the final audit count and unchanged send allowance.

Frozen 0070–0077 SQL is unchanged. Migration 0078 adds nullable action/handover
evidence to existing audit rows and an append-only trigger. Existing evidence,
revisions, caps, reservations and stops are preserved. Startup refuses missing,
disabled or weakened control-evidence protection.

Open gates include curated vault packs, full native configuration/style and
semantic acceptance, paid model/provider credential lifecycle, authoritative
legacy writer retirement/import/reconciliation, fleet capacity and cutover.
These controls do not satisfy or authorize those gates. Preserve both repository
histories and unrelated Telegram work. No commit, push or live migration is
part of this checkpoint.
