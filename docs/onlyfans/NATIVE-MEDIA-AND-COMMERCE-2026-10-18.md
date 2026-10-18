# Native existing-media and commerce checkpoint, 18 October 2026

Offline schema: `0077_native_media_sales`. This extends the saved 0076 memory
checkpoint. Execution uses synthetic model/payment evidence and a fixture-owned
HTTP listener. No live account, model provider, remote database, deployment,
allowance reset, Git push or Telegram runtime was used or changed.

## Native preparation and policy ownership

The same native composer/model response can contain `reply`, `facts`, `profile`
and `sale`. `sale` is null or a listed integer item ID plus an exact quote from
the current inbound. The worker supplies all account, fan, source and price
references. Extra keys, invented identifiers or unsupported output refuse the
whole preparation; its known model expense remains recorded. Memory continues
through the existing Fan/FanProfile owners and source audit in that transaction.

Only the explicit delivery `media_enabled` policy revision and saved true
`pack_send_enabled`, `pack_on_ask_enabled` and `qualification_gate_enabled`
permit the new capability. Existing policies migrate with media disabled.
Text continues when the shelf is empty or selling is held. There is no second
model call for matching, captioning, extraction or verification.

`sell_lane.fan_brake`, `money_brake` and `qualify_snapshot` are extracted native
decisions used by both the legacy lane and this canonical snapshot adapter.
The existing async readers delegate through `AsyncSession.run_sync` to the same
session-based queries. Configuration merging, `_quote_item`, native price
bounds and seeded quote arithmetic retain their owners. Shared query adapters
do not start the legacy scheduler or call its delivery functions. Dedicated
roles continue to reject the legacy provider and paid-model entrypoints.

The current subset is a direct content request and an enabled, described
catalogue single (`script_id IS NULL`) containing existing photos/videos.
Candidates inspect at most 48 rows, retain at most 12 offers and use at most
16 KiB of snapshot space. Missing/removed files, invalid IDs/previews, video
previews, free/legacy items and already-owned hero media refuse. Native spend,
open-offer and qualification brakes remain in force; queued intents also reserve
space under those ceilings. Identity questions do not acquire a sale.

This does not yet port the curated vault pack resolver, all legacy configuration,
proactive sellers, customs, free rewards, negotiations or campaigns. Model choice
within the bounded shelf still needs semantic quality acceptance. A quote proves
source location, not an independent semantic match. No extra adversarial model
is hidden in this path. The deterministic caption discloses the saved description,
actual media count/formats and USD quote before the guarded conversational reply.

## One media delivery authority

`TextAuthority` remains the sole intent/admission/one-use/receipt authority.
Its historical name and text payload hashes remain compatible. Media fields
extend the existing Dispatch and intent, rather than introducing another sender.
Manual internal preparation uses the catalogue sticker; automated preparation
requires its exact native plan and recorded native quote. No public command or
provider transport is enabled by this checkpoint.

The persisted terms bind exact media/previews, price, catalogue description and
vault identity/version metadata. Scope, owner, input, control, policy, native
cost, ownership, catalogue and current sales brakes are checked before queueing,
admission and the final committed one-use marker. The exact bytes cross the
owned-loopback test protocol without retries, substitutions or price rewrites.
The protocol remains a local rehearsal, not an OnlyFans adapter.

Before wire, obsolete terms cancel and release the delivery reservation. An
uncertain response retains its unit and shared hold, and cannot be retried.
Accepted evidence commits first. Its repairable transaction projects the exact
Message, an open ContentOffer, unpurchased VaultSend rows and the existing native
LadderQuote. Native rung advances once on accepted projection. Repeated dispatch
or projection creates neither another offer nor another quote. Catalogue edits
after acceptance cannot rewrite what the receipt proves was sent.

An accepted offer is not a purchase. It creates no Transaction, model call,
paid ownership or revenue. The current ownership reader prefers actual recorded
message files over a subsequently edited catalogue; its legacy fallback is now
account-scoped. A newly edited file cannot become owned through an old message.

## Payment and reversal intake

`CommerceIntake` accepts only the explicit worker-role synthetic event type.
This rehearsal contract supplies an event key, provider transaction ID, positive
source revision, scoped accepted message, full gross USD amount, kind, timestamp
and evidence reference. It is not a live provider event parser or an assumption
that a future provider supplies the same sequencing contract.

Only already-accepted, projected media terms can be paid. The account lock
serializes intake. Existing Transaction, Message, ContentOffer, VaultSend,
LadderQuote, Fan spend and LadderState projections commit atomically with
append-only `of_commerce_evidence` provenance. There is no parallel financial
balance store. Pending payout may represent a paid purchase; later payout
clearing does not charge spend twice or reopen a manually stopped seller.

Exact duplicates are idempotent. Older source revisions remain evidence and
cannot resurrect a refunded purchase. Refunds/chargebacks revoke only this
message's ownership evidence and stop selling while preserving conversation
mode. Another independently purchased copy remains owned. The gross financial
row is retained with refunded/chargedback status. Partial refunds and unrelated
transaction kinds are unsupported and refuse.

Conflicting event keys/revisions, payload identity or canonical transactions
retain their conflicting evidence without overwriting the prior payment state.
They set attention and hold further media sales for the account. A newer event,
control change, policy revision or restart cannot clear that hold. Text controls
do not act as financial reconciliation. Operations exposes only the bounded
`commerce_evidence_conflict` reason, not transaction IDs, event bodies or tokens.

PostgreSQL rejects UPDATE/DELETE on payment and memory audit evidence. Startup
checks the required enabled triggers and exact rejecting functions through both
sync and async drivers. Encrypted restore retains all commerce owners and audit
rows and remains quarantined by the independent restore fence.

## Verification and remaining gates

Source tests cover native one-call selection, actual local HTTP, immutable media
terms, catalog/ownership/control changes before wire, lost acceptance, exact
repair, purchase/reversal ordering, competing processes, rollback during
projection, independent ownership, audit protection, populated upgrades and
encrypted recovery. The real authenticated FastAPI/Next/browser path checks the
payment hold, operator-only scope, mobile overflow, session loss and zero external
UI requests. Exact-base comparisons exercise 5,600 native sales decisions, 38
database/config reader cases and 360 legacy-default composer cases.

The checkpoint evidence records final counts and earlier corrected test-fixture
failures. Full native policy/semantic acceptance, authenticated operator command
integration, live provider/model and credential lifecycle, writer retirement and
import reconciliation, fleet capacity and release/cutover remain open.
