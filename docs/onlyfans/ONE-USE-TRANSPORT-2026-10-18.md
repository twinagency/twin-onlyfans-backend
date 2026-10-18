# One-use egress and local HTTP rehearsal — 18 October 2026

This extends the shared text authority with a durable one-use network boundary,
actual HTTP failure tests against a local fixture, and call-time refusal of
legacy provider clients in dedicated roles. It is an offline milestone 3
slice. No real account, provider session, model, deployment or native/Telegram
migration changed. The API and dashboard remain read-only.

## The final send boundary

The preceding checkpoint granted dispatch admission once, but a copied grant
could still be passed directly to the synthetic transport. This checkpoint
requires both supported transports to consume that grant through the database
before any simulated send or HTTP client/socket construction.

| Stage | Durable behavior |
|---|---|
| Prepare | Save immutable command and wire hashes, owner/input/policy versions and text. No canonical outbound message exists. |
| Admit | Reserve allowance and install the attempt hold. Return an immutable dispatch grant; this alone does not permit network access. |
| Consume | Under the account lock, validate the exact stored grant/token/payload, DB deadline, active binding, control/input/policy revisions, current history, cap and attempt ownership. Commit `wire_started_at` exactly once. |
| Egress | Perform one request outside the transaction. No replacement worker can consume the grant again. |
| Receipt/repair | Persist accepted proof before idempotent canonical projection. Ambiguity retains the hold and reservation; repair never invokes transport. |

The consume transaction is now the final stop/takeover boundary. A control,
revocation, policy, cap or input change that wins before it prevents egress.
An eligible current-version attempt cancelled there can release its reservation
once because its network marker was never committed. A change after consumption
cannot recall an admitted request and blocks subsequent work.

Expiry, process loss and migrated older attempts remain unknown/charged; they
are not automatically refunded or requeued. The network marker is never a lease
that another worker can reclaim. A valid late receipt can still settle its
historical scoped attempt, including after binding revocation. Throttle is
refloored at network start and first settlement so a delayed handoff cannot
consume the entire spacing interval before a POST actually occurs.

## One-attempt HTTP transport

`onlyfans_runtime/loopback_transport.py` targets only an explicitly owned
`ThreadingHTTPServer` bound to literal `127.0.0.1`. Its internal fixture handle
has account scope and the captured policy revision. The destination path is
fixed; no URL, hostname, proxy, cookie, captured session or credential can be
supplied by API/environment configuration. No public route constructs a handle.

The local wire contract is `of-wire-rehearsal.v1`, **not a verified OnlyFans
API contract**. It preserves exact UTF-8 text and the existing scoped wire hash.
The envelope adds intent/attempt identities and fixture policy revision. The
database dispatch token, actor, tenant credential and cookies are not sent.

Each attempt uses a new HTTPX client with zero connection retries, redirects
and authentication disabled, no environment proxy configuration, HTTP/1.1,
one connection and no keepalive reuse. An absolute cancellable asyncio deadline
(at most five seconds) covers headers and body, in addition to phase timeouts.
Raw response bytes are capped at 64 KiB; compressed responses are refused before
decompression. Response and client resources close on success, protocol error,
timeout and cancellation. This synchronous worker interface can be called from
async code through `asyncio.to_thread`; it refuses direct use inside an active
event loop.

Accepted/rejected proof requires an exact schema, scope, attempt, hash, fixture
generation, aware timestamp, evidence reference and strictly typed identities.
Extra/duplicate keys, NaN, invalid UTF-8, bool/float IDs, oversized responses,
unrecognized encoding or wrong peer headers are refused as proof. HTTP errors,
redirects, disconnects and timeout remain unknown. Only the explicit validated
synthetic rejection contract can refund; a status code alone cannot.

Fixture handles are trusted internal test data. Frozen fields, trusted pickle
roundtrips and the peer header do not authenticate malicious local Python or
untrusted pickle input. The durable consume operation is the actual send gate.
Policy revision is a conservative **fixture generation**, not verified provider
session health or a credential-generation authority. Real session verification
and a signed one-request provider adapter remain separate release work.

## Legacy writer isolation

The shared `onlyfans_runtime/role_guard.py` refuses any presence of
`ONLYFANS_PROCESS_ROLE`, including empty and invalid values. It now runs at
construction, before session-factory file reads, before signed HTTP, before
every proxy-retry callback and before independent multipart Session/PUT attempts.
Previously constructed/cached clients and dynamically registered automation
kinds cannot bypass these tested boundaries. The legacy server also refuses
at its earliest import boundary using key presence.

The absent-role legacy path retains positive behavior tests. This does not
retire a legacy deployment that runs without the role key, stop external
provider schedules, or sandbox arbitrary Python/network libraries. The source
register's unconverted writers still require cohort-specific cutover. Import-hook
tests independently refuse legacy server/client/automation/session/model modules
from the dedicated app and runtime closure; all public API routes remain GET.

## Schema and migration

Head is `0073_one_use_egress`. The frozen forward-only migration adds only:

* `of_text_intent.wire_version`, integer, not null, default zero;
* `of_text_intent.wire_started_at`, nullable timestamp.

Current preparation explicitly creates version-one intents. Migrated older
rows remain version zero: queued work cancels before allowance reservation,
and dispatching/unknown attempts remain held for reconciliation. No policy,
cap, receipt, history, activation or unresolved evidence is reset. The frozen
0070–0072 snapshots are unchanged.

Explicit `schema bootstrap|upgrade|check` supports declared 0064/0070/0071/0072/
0073 after version-aware structural checks. The populated 0072 rehearsal retains
cap/audit/unknown/dispatching/history/control data exactly and performs no send.
Unknown historical schema semantics still require the separate import gate.

## Evidence and continuation

Run against a disposable PostgreSQL server using the existing test setup:

```sh
python -m pytest service/tests/test_onlyfans_text_delivery_pg.py service/tests/test_onlyfans_egress_pg.py service/tests/test_onlyfans_loopback_transport_pg.py -q
python -m pytest service/tests/test_onlyfans_role_boundary.py service/tests/test_onlyfans_egress_isolation.py -q
python service/tests/test_onlyfans_read.py
python service/tests/test_onlyfans_migration_pg.py
python -m pytest service/tests/test_onlyfans_http_pg.py service/tests/test_onlyfans_foundation_pg.py -q
```

Evidence includes actual PostgreSQL competition and actual loopback HTTP POSTs,
lost responses/restart, slow headers/body, malicious response shapes, exact
Unicode/whitespace, hostile proxy settings, redirects, cookie isolation, policy
rotation, prewire stop/revoke/input/cap races, conservative legacy handling and
resource closure. The actual Next/browser receipt path is rechecked after the
new consume boundary. Final counts and logs are in the checkpoint validation
report. Screenshots are internal and excluded from the deliverable.

Remaining live gates are unchanged: accepted shared native source/ownership,
consumed-budget and unknown-attempt import, verified connection/credential
lifecycle, trustworthy real-provider request/proof semantics, retirement of
all competing writers and external schedules, authenticated command/resolution
APIs, restore/cutover rehearsal and separately authorized provider acceptance.
No live send capability or native planner is enabled by this checkpoint.
