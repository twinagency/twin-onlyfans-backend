# OnlyFans agency access and conversation handover — 20 October 2026

## Scope

Agency members use Twin's existing verified login, `agency_memberships`, and
`agency_creators` assignments. Read-only members can review assigned OnlyFans
accounts, conversations, content and sales. Managing OnlyFans also requires the
existing agency management policy and the server-owned membership `can_manage`
flag. No new role system, membership grants, fan import, provider enrollment or
production data changes are introduced.

OnlyFans account bindings must use the assigned Twin creator UUID. An agency
with no connected creators sees an empty account list. View-as sessions, creator
sessions and the existing prospect-preview identity cannot use this agency path.
Connections, operator diagnostics, Telegram access and combined earnings keep
their existing access rules.

## Trust and deployment order

The dashboard derives a stable member actor and creator UUID list after verifying
the session with Auth and reading current membership/assignment rows. It never
accepts scope or actor identity from browser input. Every enabled JSON and binary
request carries that narrowing to `/onlyfans/agency/v1/...`, in addition to its
existing purpose bearer credential. The backend validates it and holds the
current creator binding while accessing private account data.

Deploy the native backend first, then the dashboard. A version preflight and the
`X-OnlyFans-Access-Version: of-agency.v1` response header confirm support. The
separate namespace prevents an older backend from executing agency writes even
during a rolling deployment. If support is missing, agency requests fail closed.
Internal operator routes retain their existing contract.

These changes are stacked on `codex/native-media-20261020` in both repositories.
They add no database migration. The earlier media work still requires its own
0088 migration and release procedure; this document does not mark either change
as deployed or activate native media/outgoing delivery.

## Handover

Ownership comes from the existing immutable control audit, using the first
request in the current takeover. A member's actor is tied to their verified user
ID, so signing in again preserves ownership. Agency manual replies and release
require that owner at preparation, admission and dispatch. Existing revision
checks serialize competing takeovers, and unresolved delivery still prevents
completion/resumption. Internal operators retain recovery authority.

The dashboard shows the owner as you, another team member, a Twin operator or
unknown, and the latest 30 control events. Member UUIDs and operator session
hashes are removed before data reaches the browser. Ownership refreshes with the
conversation; unavailable or mismatched ownership disables staff reply/release.
Mobile inbox tabs expose conversations, chat and controls without stacking three
long panels. Read-only access is visible and enforced by the server.

## Validation

Use synthetic identities and disposable PostgreSQL only. No real creators,
fans, memberships, messages, invitations or provider accounts are needed.

- Native PostgreSQL agency suite: scope parsing/isolation, current creator
  reassignment, two competing staff takeovers, ownership enforcement, actual
  authenticated read/control routes, read-only and actor-spoof refusal.
- Existing controls/text-delivery PostgreSQL regression suite, with corrected
  fixtures for a real 0077 upgrade and valid oversized JSON.
- Standalone native read contract checks.
- Dashboard scope/read/control/channel checks and TypeScript compilation.
- Real Next HTTP and fresh Chrome harness with loopback identity/native fixtures:
  manager and read-only sessions, revoked membership, foreign-account refusal,
  old-backend refusal, private history, encoded conversation IDs, media Range/HEAD,
  read-only upload refusal and desktop/390/320 px mobile navigation.
- Production dashboard build and `git diff --check` before review.

The browser harness tests application behavior against synthetic services. It is
not evidence of a completed real OnlyFans login, provider upload or fan send.
