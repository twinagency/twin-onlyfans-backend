# Managed IPRoyal ISP assignment

This is a separate private broker. It reads purchased IPRoyal ISP Dedicated
stock and assigns one fixed IPv4 address to each scoped creator + OnlyFans
handle. Free and paid handles receive separate assignments. There is one
inventory authority for an IPRoyal account; do not start independent inventories
for the same purchased stock.

The broker imports only confirmed, non-test ISP Dedicated orders. It never
orders, pays for, renews, rotates, or changes credentials at IPRoyal. Inventory
exhaustion is a visible refusal; it never shares an assigned IP, changes an existing
assignment, chooses datacenter/rotating stock, or falls back to direct traffic.

Existing encrypted OnlyFans connections are inspected inside the broker to
reserve their IPs before any new assignment. Existing accounts are not modified.
If an existing proxy cannot be identified/decrypted, inventory admission fails
closed. DNS-based legacy proxies require an explicit inventory review. Provider
credential changes quarantine an assignment instead of replacing its network.

## Configuration

The service listens on port 8791 on an internal Docker network, with host access
bound to loopback only. It requires these private runtime variables:

- `IPROYAL_API_TOKEN`: IPRoyal Settings > API access token.
- `ONLYFANS_MANAGED_PROXY_TOKEN`: a separate generated server-to-server token.
- `ONLYFANS_MANAGED_PROXY_TENANT`: the current OnlyFans connection tenant.
- `ONLYFANS_MANAGED_PROXY_DATABASE_URL`: the existing PostgreSQL database.
- `ONLYFANS_CONNECTION_KEY`: the existing connection encryption key.

Run `python -m managed_proxy.api --init-schema` explicitly once, then run
`python -m managed_proxy.api`. Initialization creates the isolated
`onlyfans_proxy` schema only. It does not change the runtime Alembic revision,
public account controls, session records, delivery counters, or FASTT processes.
No schema is created or repaired at normal service startup.

The dashboard uses `ONLYFANS_MANAGED_PROXY_BASE_URL` and the private
`ONLYFANS_MANAGED_PROXY_TOKEN`. It never receives the IPRoyal API token.
Assignment URLs travel only from broker to dashboard server to hosted sign-in.
Browser requests contain the handle/country selection, never managed proxy
credentials. Captured network fingerprints and signed-in handle checks apply
before normal enrollment. Existing reconnect uses the saved network unchanged.

## Operating stock

Buy a pool of dedicated ISP IPs in the required countries in the platform's
IPRoyal account. Keep renewal enabled for live assignments; do not refresh IPs
at renewal. The platform assigns an unused IP during Add OnlyFans account.
Agency users need no IPRoyal account and no proxy credential copying.

A dashboard country is a preference for new assignments, not a restriction. If
that country has no eligible unused stock, another purchased country is used.
Retries retain the same assigned IP even if the dashboard preference changes.

An assigned IP is retained across retries and is never automatically released or
recycled. Management can recover a demonstrably unused exact reservation through the
audited broker operation below. Saving a
connection starts normal imports but does not activate AI or outgoing messages.

## Verification

`managed_proxy_tests/test_managed_proxy.py` tests provider filtering, credential
redaction, exact product selection and encrypted identity binding.
`managed_proxy_tests/test_managed_proxy_pg.py` uses real disposable PostgreSQL
databases for concurrent claims, same-owner retries, free/paid and tenant
separation, existing account reservation, expired inventory and quarantine.

Provider API authentication and stock were verified with read-only requests on
25 October 2026. Live assignment requires an unused purchased IP. Synthetic
assignment tests are not evidence of a fresh OnlyFans login or message delivery.


## Audited unused-reservation recovery

Provision ONLYFANS_MANAGED_PROXY_MANAGEMENT_TOKEN only to the broker and the
management operator. It must contain at least 32 characters and differ from
ONLYFANS_MANAGED_PROXY_TOKEN. Never expose it to browser, creator or ordinary
agency sessions. With no distinct management token these operations refuse.

Use management-authenticated GET /v1/reservations to obtain the creatorId,
addressHash and exact reservationId. POST /v1/reclaim accepts exactly those
three values plus commandKey, actor and reason. commandKey is a stable 64-hex
idempotency key; actor is the authenticated management session digest in the
form op-session:<64-hex>; reason is nonempty and at most 500 characters.
Use the identical body/key when retrying an uncertain acknowledgement.
A changed body under the same key refuses. Never enumerate and release stock
as a batch or replace a refused command with a fresh key to bypass a refusal.

The broker retains the audit record and provider credentials. It refuses a
changed owner/reservation, prior browser use, account binding, enrollment or
any saved connection including revoked connections. Undecryptable or ambiguous
saved network evidence refuses the proof. Concurrent enrollment/credential
writes refuse immediately; assignment and browser admission share the locked
inventory row. No expiry or cancellation automatically releases stock.

Run the explicit --init-schema command with the corrected image before normal
startup to install recovery-upgrade.sql. Back up both public and onlyfans_proxy
schemas first. Ordinary startup checks the schema and restore quarantine fence;
it never upgrades or clears the fence.

Hosted sign-in must reach the same broker at http://managed-proxy:8791 on the
private deployment network, using the normal private broker token. Verify that
network alias before switching the sign-in role. /v1/begin-use marks first use
before a browser starts; once marked, that reservation cannot be reclaimed.
Standalone creator first sign-in uses a server-derived pending owner and the
same preparer as management. Retries retain that network; verified capture binds
the observed handle. Existing-account reconnect continues on its saved network.
