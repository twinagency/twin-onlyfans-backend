# OnlyFans earnings release — 26 October 2026

Production now uses **0092_ledger_history**. The additive migration adds of_ledger_history; canonical money remains in transactions and append-only of_ledger_evidence.

**Any replacement API, worker or FASTT supervisor image must include the 0092 schema definition and model. A 0091 application binary intentionally refuses this database at startup. Merge this change into older feature branches before replacing those services.**

Financial code: 9332f943354fea4c2592912ee4ddf4c58478cfb1. Source, migration and release tooling are on codex/full-earnings-history-20261026, PR #28. The image overlays preserve each observed runtime's unrelated code, configuration, mounts and delivery scope, including automatic content imports, vault progress and in-flight-message handling.

| Service | Deployed immutable image |
| --- | --- |
| API | sha256:ce1ba4221a97b5a7faa4e438050aeffbae6086aa984d69877e8a520d27a1bf09 |
| Worker | sha256:f4788f7b1bb7a2723e4ddc6e30ec05945c7c4213aadc83491ed661478c133965 |
| FASTT supervisor | sha256:f8bbf5e7703311b6190555a98db2d85b94a6311f0a824d191205892c6c264a3d |

Private host receipts: /var/tmp/of-ledger-history-20261026/release/plan.json and deployed.json. The verified logical backup is under /var/backups/twin-onlyfans/earnings-history-9332f94/. Do not publish those private configuration/backup files.

## Behavior

A separate LedgerWorker polls due recent scans independently of chat/AI work. Recent scans request another pass after 300 seconds; the existing shared provider budget and backoff remain enforced. Lifetime imports have their own durable connection-bound cursor, reuse canonical deduplication, resume bounded pages from an epoch lower bound, and revisit older payment statuses daily. Old imported payments do not cancel current engagement.

Optional includeHistory=true adds scoped import status and reconciled account/source breakdowns; existing response contracts remain unchanged without that flag. The OnlyFans Overview offers All time, 90, 30 and 7 days, and refreshes visible pages every 30 seconds.

The combined All time headline must use an epoch lower bound. The first Telegram payment is only a Telegram query optimization and must not exclude older OnlyFans payments (dashboard PR #292).

## Verified result

Both Twin accounts completed the provider history walk: 520 records per account, 1,040 total. Of these, 1,030 cleared/pending payments remain in recorded revenue; reversed payments are excluded. Verified totals: USD 14,858.26 gross; USD 11,886.34 net after provider fees, including USD 4.80 pending. Moxy has zero authorized OnlyFans bindings and its agency scope exposes no Twin records. Both recent scans were current in the financial API verification.

Validation includes 137 PostgreSQL ledger/provider/worker/earnings checks, the additive 0091-to-0092 retention rehearsal, successful lifetime/agency CI, TypeScript, overview/lifetime contracts and Linux runtime route checks. GitHub subsequently blocked new jobs for its billing/spending-limit issue. Dashboard builds were staged against the existing Linux dependencies and verified before cutover. Temporary PostgreSQL acceptance containers and their network were removed after testing.

Follow-up: the source/category loop now has separate amount variables so optional breakdowns cannot overwrite cleared totals. A multi-kind plus pending regression check passed against the actual API image. Flirt received the earnings view in dashboard PR #297, based on its live 4a192c6f media/onboarding source; verification targets dashboard.flirtaiagency.com directly.
