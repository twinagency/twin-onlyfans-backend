# Twin conversation engine: offline implementation status

Recorded 2026-11-07 01:55 UTC on Work Server `ubuntu-s-4vcpu-8gb-syd1`.
This supersedes the earlier `BUILD-STATUS.md`; that earlier report is retained unchanged.

## Review locations

- Project: `/srv/projects/twin-conversation-engine-20261107`.
- Backend: `/srv/projects/twin-conversation-engine-20261107/backend`, branch `codex/twin-conversation-engine-20261107`, commit `ee4c062fe7a3c9b4234ba42146c88d22aacaa1be`. Vault input was merged at `019f5d5c8a5a031a3ceaf951fb6657e505986e54`.
- Dashboard: `/srv/projects/twin-conversation-engine-20261107/dashboard`, branch `codex/twin-conversation-engine-20261107`, commit `392f7d5e868b609ba993f9eeeff45be119439084`.
- Baselines preserved: worker `fa7f1387267b43fafc1b34a8c44690c4ac579c2d`, dashboard `a39e34a524b74bdf3bb2e5571453e56401796864`, vault `91be062064b3071fea1a11509ed90d81ecb0075e`. Both isolated branches are clean; original working trees were not reset or modified.
- The governing specification and architecture handoff are the two supplied files in this evidence directory. The specification SHA-256 is `5364224b0fd63d36ae53c479e0691a48e89e929db621a02bc1af3cd3f044e155`.

## Connected offline behavior

A signed synthetic incoming webhook is authenticated, scoped to the creator connection, recorded and deduplicated in PostgreSQL, applied to the canonical inbox, and passed to a deterministic in-process model. The durable request and immutable decision attach to the existing text submission authority and its one outgoing intent. A synthetic provider response becomes a canonical receipt and message, then the existing authenticated read API and dashboard inbox render it. A second event delivery and a restarted worker reuse the same action and do not send again.

The same lab path exercises a frozen paid offer, a separately evidenced purchase, independently evidenced access, and a later access revocation. The dashboard labels those states separately. Exact synthetic proof can repair a lost acceptance after a restart without another send; missing or conflicting proof remains unknown. A proven-not-sent retry preserves the original intent, payload and idempotency key. Connection changes are refused while sends are unresolved. Request ownership, sales pause and accepted human handover are durable. The writer receives scoped pending actions, requests and bounded-history truncation markers.

The vault integration preserves the 1,500-photo/700-video selection window, two-hour heads and daily reconciliation. Human description corrections bind to the reviewed content version; stale or concurrently changed content cannot silently renew the correction or reapprove a product. Equivalent checks were exercised against the generic vault compatibility variant.

## Tests and image qualification

- `final-connected-postgres-tests.log`: 31 passed, including new engine journeys, financial distinctions, existing manual/automatic send regressions, vault migration and preservation, legacy description correction, ambiguous sends, crash repair and concurrent dispatch. After the bounded-history change, `final-history-recovery-tests.log` has 4 relevant passing cases, including both truncated and untruncated history. `two-fan-concurrency-test.log` confirms a second fan can proceed under the existing opt-in concurrent mode while the first fan remains unknown.
- `final-unit-regressions.log`: 47 passed. `final-dashboard-regressions.log`: 10 dashboard contract tests, both rendered journey fixtures, 116 read-boundary checks, the existing OnlyFans UI checks and offline Telegram/history checks passed.
- Focused TypeScript checking of the changed inbox/status and rendering files passed using `tsconfig.dashboard-engine.json` and a test-only CSS module declaration. The full dashboard TypeScript check ran out of its 1.2 GiB Node heap under the 1.5 GiB container limit; it did not pass. A full dashboard build and browser test were not run.
- Offline-only main image `twin-engine-offline-main:20261107-r2`: `sha256:e9331e82d7f669928758d016353d6d440affc7cd840c8e7dff54588313a163ef`. Offline-only generic-vault image `twin-engine-offline-generic:20261107-r2`: `sha256:bd9180dbe0d3872e86cfc8fa06cb0c67eb874da80b149108345b2ebf3de85665`. Both embed the backend commit and have no automatic server start. The main image bootstrapped schema `0113_conversation_engine`, both images checked that schema and imported their worker/vault modules, and each passed three image-resident journey tests. The generic image additionally passed two vault correction tests using its actual imported module. See `offline-image-schema-checks-r2.log`, `offline-image-journeys.log`, and `offline-generic-image-vault-tests.log`.
- The original base image expects `0111_moxy_owner` and rejects `0113`; it must not be mixed with the new schema. The two offline test images are not proof that every existing release image or deployment is compatible. The first image build exposed a non-root file-permission problem; the `-r2` builds corrected it and the earlier images were left intact.

To reproduce the source-level offline checks on the same server, with the dedicated lab PostgreSQL container already running:

```sh
ssh work-server '/srv/projects/twin-conversation-engine-20261107/evidence/run-connected-offline-tests.sh'
```

Each test runner uses a maximum of 0.75 CPU; Python containers use 768 MiB or 1 GiB, the focused dashboard check uses 1.5 GiB, and the dedicated no-network PostgreSQL container uses 0.5 CPU, 512 MiB and a 256 MiB data tmpfs. No test connects to an existing application database. The lab container was already present and is left running to avoid destroying its tmpfs; it publishes no ports. Existing Moxy/PostgreSQL containers were not restarted or changed. No network provider, model, purchase, message, production database, deployment, paid resource, or existing Telegram bot was contacted or modified.

## Material remaining work and gates

This is a connected synthetic prototype, not completion of all twelve product rules and not a release candidate.

1. The shared authority still applies legacy conversation safety/policy checks to the new engine. Full separation and an inventory/fence of direct legacy, scheduled and external senders remain necessary before any real account can be selected. The simulation has no real HTTP listener or production Fans API client.
2. For the same conversation, an unresolved action still broadly holds subsequent sends. The request is retained and shown as needing review, but a proven-independent reply is not yet admitted. Other fans can proceed in tested concurrent mode; the default gap mode retains its account-wide gate. Dependency-aware admission and recovery must be implemented and tested without losing any unknown action.
3. Full creator-fact and correction memory, authoritative catalogue search/aliases and completeness, ambiguous offer matching, multi-part ordering, scheduling/escalation, and the full set of operator workflows remain incomplete or unqualified. The snapshots are bounded and disclose truncation; they do not establish complete history.
4. Actual platform permission, exact permitted model/host, creator-approved boundaries and operational definitions, provider identity and ownership-coverage guarantees remain unresolved. No explicit sexual-content generator or safeguard bypass was implemented; the full requirements in rules 2 and 12 have not been met or qualified. The synthetic acceptance lookup is a fixture, not a claim that the real provider offers that operation.
5. Live provider/model calls and spending, purchases, live messages, production access/migration, deployment and activation each remain subject to separate approval. Moxy/Moxie two-creator development remains paused.
