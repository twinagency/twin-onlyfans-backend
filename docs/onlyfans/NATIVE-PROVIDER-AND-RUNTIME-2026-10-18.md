# Native provider and runtime integration

## Delivered source

`service/onlyfans_runtime/provider_connector.py` adds an explicit, account-bound
HTTPS adapter using the existing signing function and curl_cffi Chrome profile.
The session capture, expected owner, x-hash, fixed proxy and expiry are supplied
by the selected connection. Construction performs no network or file access.
Identity is verified before history reads and before consuming a send grant.
The only operation after the common authority's committed wire marker is one
POST of the frozen message. Redirects, automatic retries, environment proxies,
cookie replacement and legacy co-tag repost fallbacks are disabled.

`provider_model.py` uses the existing saved native model/provider registry,
reasoning mode and saved effort policy. It accepts an explicit account-specific
credential; it does not resolve house keys, environment keys or another model.
The durable native plan and existing GrokCall/GrokDailyCost reservation must be
committed before its single POST. Usage settles the original account/provider
and UTC day. Missing usage or an ambiguous response retains the cost reservation
and blocks the plan. Registry prices remain the existing accounting rates;
they are not independently verified current provider invoices.

`native_context.cost_matches` distinguishes recorded synthetic calls from native
calls using policy mode, model, provider and exact endpoint. A saved fake-model
draft cannot become a native outgoing message after a policy-mode change.
`NativeWorker` accepts no model credential for explicit native accounts so manual
messages and operator-authored campaigns can run without invoking an AI model.
Legacy OFClient and legacy model entry points remain blocked in dedicated roles.

`service/onlyfans_worker.py` is the separate worker process. It requires an
explicit worker role, tenant, PostgreSQL URL and connection-encryption key. It
checks the schema without migrating it. Missing configuration fails before
provider work; startup/database errors produce redacted health and a nonzero
exit. Credentials are refreshed in ordered cohorts of at most 32 candidates,
including advancement past invalid credentials, so account 33 is not starved.
The default native worker handles at most four accounts in a cycle. A normal
cohort completes at least one pass before the next cohort; `--once` runs one
bounded four-account cycle. Stop is honored between credential and worker phases.

`Store.request_poll` schedules a later completed-history cycle for an enabled
account after its freshness interval. It never resets an unfinished scan,
requests repeated scans during recovery, changes a high-water mark or reopens
send/model allowances. The worker requests this when refreshing an active
cohort. This supplies ongoing bounded polling; no native WebSocket integration
has been added. An explicit vault-sync job can use a verified connection without
activating outgoing messages or supplying a model credential.

## Bounds and operational meaning

| Boundary | Current behavior |
|---|---|
| Native response | At most 64 KiB; explicit JSON; no compressed response |
| Model response | At most 128 KiB; one completion; structured output; max_tokens 4096 |
| Native request | Same native HTML formatter; exact frozen text/media/previews/price |
| Credentials | Explicit owner and policy generation; expiry within 24 hours; encrypted connection storage |
| Native I/O | Fixed HTTPS origin and narrow paths; explicit proxy or explicit direct connection |
| Outcome uncertainty | Hold; no automatic send or model replay |
| Model accounting | Existing provider ledger with a conservative reservation and observed-usage settlement |
| Shutdown | Cooperative between phases; a bounded HTTP/DB operation can finish first |

This is a source implementation with local protocol/HTTP fixtures and disposable
PostgreSQL tests. No real OnlyFans account, real paid model call or production
database was accessed to verify this release.

## Live integration limits

The current native parser intentionally supports a strict subset of the old
client's response dialect. It refuses tips, queue-origin/retracted/deleted
messages, unsupported rich HTML, uncertain media identities and paid-history
flags that would otherwise be mistaken for authoritative purchase evidence.
An unsupported message or oversized page holds catch-up; it is not silently
dropped. A real account with one of these shapes may therefore remain held
until the contract is validated and extended explicitly.

Media acceptance requires exact media, preview and price evidence in the native
response. An HTTP success alone does not establish delivery or purchase. No
real payment reconciliation, historical journal import, legacy-writer retirement
or full provider feature parity is established by these adapters. Missing or
ambiguous purchase evidence must not be converted into ownership or revenue.

Polling retains the existing account-wide catch-up fence. Large conversation
histories can delay replies while that bounded recovery completes. The tests
establish correctness for their fixtures, not real-time latency, fleet capacity,
current platform acceptance or production reliability. A selected account,
current session/hash/rules/proxy contract, real-model credential and staged
read/delivery/payment verification remain necessary for a live pilot.

The private static preview is a separate presentation artifact. This worker and
its database are not running behind that preview link.

## Verification locations

- `service/tests/test_onlyfans_provider_adapters.py`: explicit credentials, role
  boundary, fixed target/proxy, bounded response, no retries, model wire contract
  and bounded vault-read path; HTTP clients are replaced by local test doubles.
- `service/tests/test_onlyfans_provider_pg.py`: committed model/send markers,
  real ledger rows, receipt projection, unknown holds, credential reuse across
  distinct inputs, no-model manual/campaign operation and synthetic/native mode
  separation, using mocked native HTTPS responses.
- `service/tests/test_onlyfans_worker_entry.py`: process configuration, redacted
  failure status, signal restoration, cohort fairness, empty accounts and stops.
- `service/tests/test_onlyfans_worker_entry_pg.py`: real schema refusal, owned
  process SIGTERM, empty configuration and a later fan input discovered through
  the periodic poll and answered once through owned native-shaped HTTP.
