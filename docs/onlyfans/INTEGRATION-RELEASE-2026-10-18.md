# Twin OnlyFans operator integration checkpoint

This checkpoint continues the existing Twin dashboard and Fastt native engine.
It does not replace the planner, create another fan-memory store, add a model
call to manual messages/campaigns, or run the legacy Fastt sending workers.
Schema head is `0086_vault_edits`.

The current remaining-work plan is [DELIVERY-PLAN-2026-10-19.md](DELIVERY-PLAN-2026-10-19.md). The original checkpoint below remains historical verification evidence.
The native ledger continuation adds a default-off, bounded payment/refund reader
into the existing canonical records. It has not been validated with a real provider.

## Implemented paths

| Operator workflow | Implementation |
| --- | --- |
| Accounts | Existing Twin creator lookup; account registration initially disabled; AES-GCM session/model credential storage; worker identity verification; separate activation, pacing/allowance and pause controls. |
| Inbox | Effective human takeover; manual text and an optional saved paid item; durable queue acknowledgement; provider acceptance projected into one canonical sent message; exact lost-ack retries. |
| Campaigns | Explicit/all-saved audience snapshot, scheduled start, optional frozen paid content, recipient delivery status, pause/resume/cancel, immutable command audit and shared delivery authority. |
| Content | Native vault-sync jobs; paged account-scoped vault metadata; curated catalogue item creation with exact ordered media/previews/USD cents; metadata/price/enabled editing with revision and fingerprint conflicts. |
| AI | Explicit account-specific model adapter using the existing model catalogue, planner, memory and cost owner; persisted one-call evidence, conservative cost reservation and no hidden model fallback. |
| Runtime | Separate API and worker; encrypted connection refresh, ongoing bounded message polling and vault jobs; explicit schema migration/check commands; no automatic activation on startup. |

Native HTTPS and model adapters have been exercised with mocked HTTPS and owned
local HTTP peers. Database checks use disposable PostgreSQL. These establish
the tested contracts; they do not establish real OnlyFans acceptance.

## Start the dedicated stack

Use an isolated deployment directory and the paired dashboard branch. Keep the
existing Telegram/Fastt deployments running separately. The dedicated API binds
to loopback port 8788 by default, to avoid the legacy relay's usual 8787 port.

1. Copy `deploy/onlyfans.env.example` to a private file outside the checkout.
   Replace every placeholder. Supply six different tokens and retain the
   connection-encryption key separately from the database backup.
2. Set `ONLYFANS_ENV_FILE` to that file's absolute path and
   `ONLYFANS_DB_PASSWORD` to a generated URL-safe database password in the
   deployment environment. Set `ONLYFANS_API_PORT` if port 8788 is occupied.
3. Build and start only the new PostgreSQL service:

   ```sh
   docker compose -f deploy/onlyfans-compose.yml build
   docker compose -f deploy/onlyfans-compose.yml up -d database
   ```

4. For a genuinely empty new database, bootstrap explicitly:

   ```sh
   docker compose -f deploy/onlyfans-compose.yml --profile ops run --rm migrate bootstrap
   docker compose -f deploy/onlyfans-compose.yml --profile ops run --rm migrate check
   ```

   For a supported populated schema, take a backup and rehearse `migrate
   upgrade` on a copy first. Never bootstrap over existing data or stamp an
   incompatible legacy database. The supported revisions and structural checks
   are defined in `service/db/schema.py`.

5. Start API and worker:

   ```sh
   docker compose -f deploy/onlyfans-compose.yml up -d api worker
   ```

6. Set the dashboard's server-only `ONLYFANS_API_BASE_URL` to the API origin
   reachable from that dashboard process; `localhost` inside another container
   is not this API. Copy the matching six `*_TOKEN` values and
   `ONLYFANS_MANUAL_ENABLED=1` into the dashboard environment. The encryption
   key and provider/model credentials belong to the API/worker, not the
   dashboard browser. Use a private container network or authenticated TLS
   route; the compose file deliberately exposes only loopback.

7. Check authenticated `/onlyfans/api/v1/ready` using the read token and
   `X-OnlyFans-Schema-Version: of-preview.v2`. Health alone checks liveness.
   Register the selected creator in Accounts and save that creator's current
   capture, signing material/hash, fixed proxy choice and optional model key.
   Saving credentials leaves outgoing messages disabled. Verify the selected
   account first, inspect recovery, then explicitly enable its chosen limits.

API/worker startup performs no migration. Pausing or stopping a service retains
history, receipts, campaign definitions, unresolved-send holds and used limits.
Do not delete the database volume as a rollback. No account has been enabled
or contacted by building this checkpoint.

## Verification and remaining live work

The resumed checks include 85 provider/runtime cases, 31 vault PostgreSQL cases,
60 provider/content/worker PostgreSQL cases (including a repaired historical
upgrade fixture), the read-service and historical PostgreSQL migration
rehearsals, TypeScript checking, the production Next build, frontend command
contracts, and an actual authenticated
Next-to-FastAPI-to-PostgreSQL HTTP integration covering campaign scheduling,
paid terms, exact retries, pause/resume/cancel and account isolation.
The earlier interrupted browser suite reported a campaign-action 502. The
same production routes pass the new HTTP regression; that does not establish
that the entire browser scenario has passed. Retain that distinction when
reporting this release.

A live pilot still requires a reachable deployment and a selected creator's
current session/signing/proxy contract, plus a model credential for AI replies.
No real provider connection, real paid model call, real fan send or production
database was used for this checkpoint. Validate one account's reads, delivery,
and purchase/refund evidence before calling this production-ready.

The current parser deliberately refuses unsupported history shapes. Synthetic commerce tests do not establish real payment/reversal acceptance.
The native ledger reader additionally requires explicit provider transaction
evidence and retains unlinked PPV holds; live linkage remains a release gate. Native media upload/deletion, provider bulk queues/unsend, posts/stories,
full roster import, campaign reply/open/revenue attribution and full Fastt/
OnlyFans feature parity remain outside this checkpoint.

The hosted private Site is a sample-data presentation of the product components.
The Python worker/database are not hosted behind that static URL. Its writes
are disabled and it must not be presented as a live account-connected CRM.
