# Proactive engagement contribution — 23 October 2026

Assigned branch: `codex/onlyfans-proactive-selling-20261023`.
Backend starting commit: `dbd09b06dace2c24829ded6bc778d7e6d2ded4f0`
(Claude's feature pin, based on `88e79248e4b504cdaad6cb6c998fad79423e971d`).
Dashboard reference pin: `927b2d75105f6180c37f2759ed119f89521b979d`
(based on `244271a36031a9eaf10b1d0ea64f41d97babd80a`). No dashboard edits.

## Ownership and current boundary

Only this directory and `service/tests/test_onlyfans_launch_engagement*.py`
are changed. These names were absent at the backend pin; no competing owner was
found in the available local handoff documentation. No applicable AGENTS.md or
CODEOWNERS was found in the workspace ancestry or pinned backend; CLAUDE.md was
read. Existing native_sales, campaigns, media_delivery, TextAuthority, Store,
catalogue models, ownership, shared sales brakes and tests were inspected first.

This package implements scheduling and transactional delivery validation. It
does not register routes, alter the shared schema, run a worker, create provider
clients, call a model, send messages or activate an account. The pinned
TextAuthority correctly rejects `origin="engagement"` until Claude integrates
the changes below. Do not masquerade as a manual or campaign message to bypass
that boundary. The shared integration is a release blocker, not an additional
approval checkpoint. Claude coordinates the single combined 50% checkpoint.

## Implemented behavior

Settings default to off. The defaults below are conservative implementation
defaults, not a claim that product timing has already been agreed. All switches
require explicit saved configuration and existing account automation permission.

* `offer`: one existing fixed-price item, selected from at most 48 eligible
  catalogue entries. Rank tag overlap with saved fan preferences first, then
  proximity to the most recent paid PPV amount, then item ID. History affects
  rank only. No price is calculated, negotiated, discounted or overwritten.
  The caption uses the existing deterministic native caption function.
* `follow_up`: neutral operator-authored text after a still-unpaid canonical
  offer. It does not resend the paid attachment or invent a purchase. The source
  message, open offer, catalogue terms and ownership must still match.
* `reengage`: neutral operator-authored text after conversation silence. An
  unpaid selling cooldown does not disable conversation. This module never
  changes shared fan/ladder/offer state to obtain permission.

The latest canonical message must be outgoing and there must be inbound history;
unanswered incoming messages belong to the reactive planner. Default delays are
24h/48h/168h after the last outgoing message, with a 24h expiry window. Windows
are anchored to that message and never extended by worker restarts. Conversation
history older than 90 days is refused. One action per kind/anchor and at most one
active engagement action per fan are enforced by database constraints.

Defaults allow at most 1 outgoing contact/fan/24h, 3/fan/7d, 10/account/hour and
60/account/24h. Counts include outgoing history from all lanes, queued or unknown
reservations and accepted intents awaiting projection. Settings have strict
types, bounds and unknown-field refusal. UTC rolling windows are used, so no
midnight reset permits a burst. Frequency limits are checked during prepare,
admission and final consume, under the common account lock.

Offers reuse native configuration, fan brakes, spend velocity and shared offer
caps. Recent unpaid offers impose a default 72h selling cooldown; older open
records do not occupy this lane forever. Existing tapped TTL is honored through
a read-only projection, with the stricter engagement cooldown. Explicit stops,
companion state, live pauses and minimum fan-message rules remain in force.
No ownership/payment/offer history is rewritten. Recent payoff-media sends are
excluded for 30 days by default; owning/reusing an approved preview alone does
not exclude an otherwise unowned payoff. `catalog_terms` and `assert_unowned`
remain authoritative, including commerce reconciliation holds. Dynamic-price
catalogue items are outside this contribution's proactive offer scope.

Replies, purchases/refunds, fan preference edits, sales configuration edits,
control revisions and settings revisions invalidate the frozen context. Every
check also reads current takeover, mute, automation pause, no-contact, critical
safety flag, recovery freshness, pending ingestion, unresolved delivery and
catalogue/ownership state. A bounded fingerprint covers up to 2,000 transaction
rows per fan; larger histories are explicitly refused pending a shared compact
commerce revision contract. No external provider diagnostics are performed.

## Public service contracts

All methods require a trusted tenant/account scope; Store._runtime enforces the
current active OnlyFans creator binding and existing agency creator allowlist.
Server routes must derive the actor, verify the signed command context and
manager permission for settings/cancellation, and enforce existing same-origin
and strict JSON parsing. Do not expose this internal service directly to clients.

* `configure(tenant, account, expected_revision, command_key, actor, settings)`
  returns the resulting integer revision. It is compare-and-swap and idempotent:
  same command key + same normalized request returns the original revision;
  conflicting replay refuses. Settings revisions have immutable audit records.
  Any revision fences all outstanding actions, even when enabling another kind.
* `schedule(tenant, account, fan_id, kind, command_key, actor)` returns an action
  UUID. The planner supplies a stable 64-character SHA-256 command key. Exact
  replay returns the original action, including terminal actions. Different
  ticks at the same anchor cannot create another action. No intent is made yet.
* `due(tenant, account)` returns one proposal or null. A proposal contains
  `action_id`, `command_key`, `fan_id`, `actor`, `body`, `terms`, `expires_at`,
  `origin="engagement"`. Terms are the unchanged catalogue snapshot or null for
  text. It is safe for competing workers to read the same proposal; only atomic
  prepare+attach consumes it. Frequency refusal defers until the fixed expiry;
  obsolete context/permission cancels an unqueued action. Scan batches are 100.
* `cancel(tenant, account, action_id, actor, reason)` records cancellation.
  Reasons: operator_cancelled, reply, purchase, takeover, pause, no_contact,
  catalogue_changed. Repetition is safe and retains the first actor/time.
  Unqueued work becomes cancelled immediately. Linked intents retain the
  authority's actual state and reservation until it settles them. Cancellation
  never asserts non-delivery after the one-use wire marker.
* `detail(tenant, account, action_id)` returns status, reason, due/expiry,
  intent ID and cancellation-requested flag. It refreshes the durable projection;
  mount as a command-side refresh, or move refresh to the worker and expose a
  separate read-only projection for GET. Do not put a mutating call on a GET.

All internal datetimes are naive UTC to match existing PostgreSQL runtime
conventions. API adapters must serialize timestamps with an explicit `Z`, bigint
fan/media IDs as strings and integer cents with explicit USD. Never return raw
context, source messages or credential/session state to the dashboard.

## Required shared changes — Claude owns all of these

1. **Schema:** create the three tables defined in `models.py`; incorporate
   `schema_contract.sql` (foreign keys and immutable guards), and register their
   columns, indexes, constraints and guards in shared schema readiness checking.
   Keep the single migration head after Claude's unpublished 0089. The private
   metadata is deliberately separate so importing this module cannot change the
   existing schema contract. Never call create_all at production runtime.
   Update the `OfTextIntent.origin` constraint/model to permit `engagement`.
2. **TextAuthority.prepare:** add an `engagement_action_id` argument, include it
   in the immutable command fingerprint, and permit the new origin only with
   that ID and without reactive work/native_plan/campaign IDs. Perform the normal
   `_runtime` authorization, common `_eligibility`, media permission, exact
   catalogue snapshot, `assert_unowned`, queue and policy checks. Within that
   SAME transaction call `validate_prepare(...)`, build the ordinary OfTextIntent
   with exact terms/ordered media/preview IDs and fixed price, then call
   `attach(session, runtime, action, intent, now)`. TTL must be bounded by both
   the normal delivery TTL and the action expiry. Never commit intent creation
   separately from attach. Idempotent retry returns the existing linked intent.
3. **Admission and consume:** allow `engagement` wherever origin is validated,
   including `_validate_grant`, and treat it like automated/campaign ownership
   in `_eligibility`. Call `current(session, intent, runtime, now)` BOTH in
   `admit` and immediately before `consume_dispatch` commits its one-use wire
   marker. Keep all existing policy/revision, payload hash, media, connection,
   reservation, allowance, takeover and transport checks. A false result follows
   the existing obsolete-intent cancellation/release path. This is essential;
   checking once in a scheduler or wrapping `dispatch()` is insufficient.
4. **Receipts:** keep the shared TextAuthority status, reservation, unknown-hold,
   repair and materialization logic. Attribute engagement as AI with a distinct
   `onlyfans_engagement` automation kind; extend `native_sales.record_quote` to
   record accepted fixed engagement offers exactly like automated fixed offers.
   Preserve frozen terms during projection, and never infer purchase from send
   acceptance. Nothing here records ownership or revenue independently.
5. **Worker/planning:** select bounded accounts/fans from existing canonical
   history; call schedule only for enabled kinds and eligible histories. Derive
   deterministic command keys from binding/fan/kind/anchor. Reactive work wins
   over engagement in every cycle. After existing receipt repair, obtain one
   due proposal per account/cycle and pass it through the integrated authority.
   Do not invoke a second model, legacy scheduled sender or provider mass-send.
   Feed reply/purchase/control/catalogue events into cancel where appropriate;
   validation still protects correctness if notification is delayed or lost.
6. **Settings/API/dashboard:** expose the strictly scoped settings, status and
   cancellation contracts above through existing authenticated boundaries.
   Navigation, shared mounting, account switches and permissions stay with
   Claude. Labels must distinguish scheduled, queued, accepted and purchased;
   cancellation-requested/unknown is never displayed as successfully unsent.

Shared writers of message, purchase, ownership, control and catalogue state
must acquire the same runtime lock before final validation can be race-safe.
Existing catalogue/fan/config rows are also read-locked. Claude must retain the
common lock order and verify this across content sync and ledger integration.

## Restart and dispatch semantics

The schedule and immutable context live in PostgreSQL. No in-memory timer,
process lease or regenerated send key is needed. If a worker dies before atomic
prepare commits, both the intent and link roll back. If it dies after commit,
the existing intent is recovered by TextAuthority; this scheduler never creates
another. Linked accepted/not_accepted/unknown states project from that authority.
Unknown and wire-started attempts retain holds even after cancellation. There
is no exactly-once network delivery claim: the guarantee is one durable attempt
identity with conservative reconciliation of uncertain outcomes.

## Verification

Tests use synthetic accounts, catalogue/history/payment fixtures and unique
`of_test_*` databases on a loopback-only disposable PostgreSQL test server.
No provider calls, production reads/writes, activation or fan messages occur.
The PostgreSQL suite bootstraps the pinned schema into its own disposable
template and clones it per case. It installs only the proposed private schema
and rehearses the new origin constraint in those temporary databases. Hooks
are exercised transactionally against real shared models. The pinned worker
and actual integrated dispatch path remain unchanged and require Claude's
combined integration test; passing these tests does not claim live dispatch.

Final exact results are recorded in `VERIFICATION.md` after the test run.
