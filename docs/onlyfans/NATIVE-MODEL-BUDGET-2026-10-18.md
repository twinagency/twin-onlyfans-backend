# Native model budget — 18 October 2026

Historical 0075 checkpoint. The current extension is
[NATIVE-MEMORY-2026-10-18.md](NATIVE-MEMORY-2026-10-18.md), schema 0076.

Current offline schema: `0075_native_model_budget`. This extends the bounded
native text and operations checkpoint. All execution uses synthetic model
responses, synthetic transports and disposable local PostgreSQL. No paid model,
real account, provider session, remote database, deployment or Telegram change
was used or authorized by this checkpoint.

## One existing cost owner

Native preparation now uses the existing `llm_client` cost owner and its
`grok_calls` / `grok_daily_cost` tables. It creates no parallel cost ledger.
The native account lock serializes concurrent reservations across fans.
The immutable plan and pending call link commit before model execution; every
transaction closes before generation. The account's saved daily allowance is
checked per provider and UTC day, preserving existing spend, call counts and
sticky cap evidence. An absent, invalid, zero or exhausted allowance cannot
silently reopen a budget.

The legacy `cost_cents` columns store **USD millicents**: 100 units per cent,
10,000 units per USD. A conservative fake reservation uses the repository's seed
catalogue prices, bounded prompt bytes and the 20,000-byte output limit. This is
an offline quote, not a verified live price or a live tokenization guarantee.
The explicit fake response reports known cost in the same units. Known cost
settles once against the original account/provider/UTC day, including generation
that becomes stale or whose output guard refuses delivery. Missing usage,
timeout, failure or worker death retains the reservation and its uncertainty.
The same canonical input cannot invoke another model to retry the expense.

The existing legacy asynchronous model entrypoint stays refused in a dedicated
OnlyFans role. These synchronous helpers accept only a PostgreSQL worker scope
and explicitly synthetic audit records. They do not resolve credentials, select
an endpoint, perform HTTP or register a paid adapter.

## Saved configuration and delivery proof

The existing native model selector now also accepts an already-read config
snapshot. Native text requires the saved `ai_chatter` model choice, or the saved
base model when that purpose is absent. Missing, retired, malformed, null or
duplicate-purpose choices refuse before expense or generation. There is no
native fallback to a house model. The legacy selector keeps its existing
fallback order. The same native composer receives the saved voice and language;
deliberate typo injection remains disabled.

Schema 0075 adds seven nullable cost-evidence fields to `of_native_plan` and a
foreign key to the existing call audit. Queueing and final one-use egress both
require the matching settled call's account, fan, model, provider, day, status,
synthetic endpoint and cost. An older plan without cost evidence cannot wire.
Changing cost evidence after admission also blocks consumption. The existing
shared text authority remains the only send path, and accepted receipts remain
the only source of canonical outbound messages.

Frozen 0070–0074 snapshots stay unchanged. Populated 0073 and 0074 upgrades retain
unknown deliveries, model reservations, disabled state, controls and allowances.
They add no activation, refund, automatic reconciliation or cap reset.

## Operations contract

The existing private scoped GET now returns nested `of-operations.v2` within
the unchanged outer `of-preview.v2` envelope. `modelCosts` contains the UTC day,
literal unit `usd_millicents`, today's recorded cost plus reservations, the
per-provider allowance, call count, number of provider rollups, and all-time
pending/unknown call count for this account. It exposes no prompts, credentials,
provider names, call IDs or another account's spend. Aggregation is performed
in SQL rather than loading all history into memory.

The existing Twin Operations page displays exact USD values to four decimal
places and controlled holds for uncertain cost or an exhausted recorded
provider allowance. It describes cost and reservations, not settled provider
billing. Strict TypeScript parsing rejects older/future snapshots, invalid days,
wrong units, extra fields and malformed counters. The positive internal operator
session and account/creator scope gates still apply; session loss removes cost
and delivery counters.

## Remaining integration

This is bounded synthetic text and cost evidence, not completion of Milestone 4
or production model billing. The accepted full style/context configuration,
summary and corrected fact persistence, real provider/model lifecycle and paid
usage reconciliation remain open. Unsupported OpenRouter choices refuse; no
OpenRouter routes, keys or fallback policy were invented. No existing exhausted
native/Telegram or global pilot allowance is imported, reset or reopened.

Existing photo/video selection, offers, payment evidence and media delivery must
still be integrated through the same authority. Authenticated command flows,
writer retirement/import, capacity/soak and release/cutover gates also remain.
The checkpoint's validation report distinguishes current reruns from retained
historical evidence. Git commit/push and external activation remain separate.
