# Native workspace reads — offline checkpoint, 18 October 2026

Database head remains `0078_operator_controls`; this checkpoint adds no DDL.
It follows the operator-control checkpoint and adds scoped native Content,
Sales and Settings views to the same Twin product.

## Canonical records and contract

The GET-only read router adds
`/onlyfans/api/v1/accounts/{account}/workspace/{content|sales|settings}`.
The outer envelope remains `of-preview.v2`; its strict nested read model is
`of-workspace.v1`. Existing operator/read-token/active-binding gates apply.
The separate five-action command interface is unchanged.

Each native read uses one PostgreSQL repeatable-read snapshot and a shared
lock on its active tenant/account binding. A concurrent revocation cannot
commit between authorization and the private read. Transaction rows, grouped
totals and open offers cannot describe opposite sides of a concurrent payment
update. Read time never becomes a history synchronization timestamp.

| View | Existing owner | Behavior |
|---|---|---|
| Content | CatalogItem and VaultItem | Singles only; described metadata, exact stored price, media/preview counts, disabled/free/review status; the existing authority checks eligible catalogue terms |
| Sales | Transaction and ContentOffer | Original currency/status groups; signed exact gross and optional recorded net; open offers shown separately, never counted as payments |
| Settings | AccountAiConfig, OfRuntime, OfDeliveryPolicy | Saved persona/facts, model choices, language and explicit sales flags, alongside recorded runtime permissions and consumed allowance |

Content/transaction pages default to 25 records, maximum 100. Opaque keyset
cursors carry their section and account and cannot cross either boundary.
Legacy catalogue script rows remain hidden through `CATALOG_IS_SINGLE`.
Invalid media or price data is not converted into a free sellable item.
Long descriptions and personas are explicitly labelled excerpts.

Sales totals cover the account's recorded transactions, grouped separately by
currency and exact status. There is no currency conversion, combined Telegram
earnings, fee inference or offer-to-revenue projection. Refund, pending and
cleared rows stay separate. Totals are bounded to 64 groups; a larger or invalid
financial projection refuses instead of silently truncating or rounding money.
Open offers show their saved PPV and legacy tip thresholds, with no claim that a
message reference proves delivery. Only the newest 25 open offers are listed;
the count and explicit truncation note cover older offers. No offer is settled
or sent by this view.

Settings expose only an allowlist of saved values. They do not expose provider
credentials, raw model-purpose/configuration blobs, generated creator claims,
transaction source bodies or provider transaction identities. Persona facts
use the existing shared field/parser contract. Model values are labelled saved
choices; the read does not resolve credentials, invoke a model or certify an
effective provider configuration. Missing or malformed explicit flags stay
missing/reviewable, rather than acquiring enabled defaults.

## Native reuse and dashboard

Two tiny leaf contracts now hold the existing creator-fact fields/parser and
native price limits. The original native modules re-export/use them. The
read path can therefore reuse the exact catalogue authority and creator-field
contract without importing the legacy seller, scheduler or model client.
Native prompt/model/quote behavior is preserved; no extra prompt or AI call is
introduced.

The dashboard replaces the planned Content, Sales and Settings entries with
real authenticated server-rendered pages and separate GET routes. All use the
existing Twin cards, palette, account/creator scope and private session gate.
Cross-tenant, invalid/duplicate scope, agency and view-as requests are refused.
There are no inert upload, edit, pricing or payment buttons. This checkpoint
implements the read surfaces, not native vault/configuration management writes.

The HTTP port validates exact nested shapes, scope, versions, counts, dates,
cursors and safe integer money before rendering. Money formatting uses integer
arithmetic. Unknown or malformed responses become unavailable, not fixture data.
Mobile wrapping, account links, pagination and session loss are exercised in the
real API/Next/Chromium path with external browser requests blocked and counted.

## Validation and remaining gates

The checkpoint validation manifest contains the final PostgreSQL workspace,
concurrent-refund snapshot, affected native planning/memory/media, import
isolation, browser, frontend contract and full-dashboard TypeScript results.
Exact Git-base comparisons preserve 360 native-default composer outputs and
384 legacy model-selection/configuration-read cases. Earlier operator controls,
delivery, commerce, upgrade and recovery evidence remains retained separately.

This is offline synthetic acceptance only. No real account/model/provider,
remote database, deployment, Git push or allowance reset was used. Full native
policy/semantic acceptance, curated vault packs, content/configuration writes,
provider lifecycle, delivery-evidence reconciliation commands, authoritative
legacy retirement/import, production capacity and cutover remain gates. The new
navigation entries do not mark all of Milestone 5 or the live rollout complete.
