# Persistent native OnlyFans connection

The private sign-in flow previously assigned a six-hour local credential
deadline. The worker marked a verified connection expired and disabled its
runtime at that deadline without asking the provider whether it was still
valid. Signing in again was the only renewal path.

The existing worker now verifies previously verified connections at most once
every fifteen minutes during normal operation. A successful account-identity
GET renews the encrypted connection lease for six hours. This can repeat
without a connection-age limit. The fifteen-minute sign-in attempt timeout,
short-lived in-memory transport credentials, account identity, fixed network
configuration, encryption and all existing delivery gates remain unchanged.

An elapsed local lease is usable only for a bounded identity probe. No history,
media, AI or messaging operation receives that probe. Only successful identity
verification permits the saved lease to advance. A never-verified expired
capture, corrupt credentials, a revoked binding or a paused connection cannot
be renewed. HTTP 401/403 and a different returned account stop the connection.
Rate limits, provider errors and transport failures retain the saved activation
intent and retry no more than once per minute. Work remains blocked if the
unchanged verification lease expires during an outage.

Renewal compares the original encrypted credential and command revision after
the provider call, so a concurrent pause or replacement wins. It does not change
the account's activation setting, connection revision, delivery-policy revision,
allowances, queued work, model choice or manual conversation controls. A
connection disabled by the previous timer can regain verified status, but this
change does not turn its messaging runtime back on.

This renews the application's verified-use lease, not provider-issued cookies.
Provider logout, credential revocation, security challenges and incompatible
provider signing changes can still require a new sign-in. No password is saved,
no CAPTCHA is bypassed and no provider session is asserted to last forever.

Acceptance covers repeated renewal, expired previously verified credentials,
disabled accounts, transient failures and retry pacing, rejection and account
mismatch, concurrent pause/replacement/revocation, and invalid captures. Tests
use an isolated PostgreSQL database and mocked provider I/O.
