# Offline engine continuation — Work Server checkpoint

Completed 7 November 2026. This is a connected **offline simulation**, not a deployment, live-provider qualification or full twelve-rule/model acceptance.

## Saved work

Project: `/srv/projects/twin-conversation-engine-20261107` on `work-server` (`ubuntu-s-4vcpu-8gb-syd1`). Backend HEAD: `78745b32b3213b97ade0f7f97376f6a996b52dc5` (implementation commit `a4453fcb0f9829d780afe8840ee1987e72d09f41`, followed by the loopback boundary test fix). Dashboard HEAD: `0fdb8b8c7a3796930688261f0f670350c4d9c466`. Both working trees were clean at qualification. Earlier commits, evidence and existing services were preserved.

The new path uses mechanical account, contact, ownership, purchase and duplicate-send controls without invoking the legacy conversation detector or sales policy. A direct, unambiguous stop-contact request is audited and blocks further contact. Unknown sends remain recorded; a narrowly proven independent response can proceed without replacing the uncertain action. Memory uses the same source-backed correction projection as the sidebar, with binding and revision fences. Catalogue search uses approved aliases, recent scoped context and explicit coverage/uncertainty; outstanding offer matching retains the original price and refuses ambiguous references. Multi-part replies are ordered, corrections cancel affected unsent parts, and a proven-not-sent later part can retry its existing action. Operator responsibility and resolution commands are scoped, revision checked and audited.

The local HTTP fixture commits signed webhook intake before acknowledging, uses bounded loopback-only processing, resumes durable events on restart or queue saturation, and separately recovers expired attempts and saved receipts. Its provider and model are simulated. The dashboard displays the recorded request, receipt, delivery uncertainty and ordered-part progress; operator actions have authenticated, same-origin and backend scope checks. No provider credentials, external calls, live messages, purchases, metered model calls or deployments were used.

## Evidence

- `continuation-unit-regressions.log`: 48 passed.
- `continuation-postgres-initial-fixture-failure.log`: 60 engine/HTTP cases passed, then the run stopped because a committed generic-vault fixture was not mounted into the test image. The test runner was fixed to mount `backend/compat` read-only; `continuation-postgres-followup.log` then passed the 27 remaining vault, schema, legacy-control and delivery cases. Together these runs passed 87 relevant PostgreSQL cases; the initial invocation was not a clean single-run pass.
- `backend-dev8-targeted.log`: 6 later-part retry, contextual catalogue, control-race, queue-saturation and HTTP cases passed. The final PostgreSQL runs included these areas as well.
- `continuation-dashboard-regressions.log`: 14 contract tests, both rendered synthetic journeys, 116 read-boundary checks, and the offline OnlyFans UI, history and Telegram checks passed.
- `continuation-browser-regressions.log`: 19 engine-journey assertions at desktop and mobile widths using the same backend-generated message/request projection, plus 267 assertions in the broader synthetic OnlyFans UI sweep. Browser operator actions used a simulated handler; the actual backend operator boundary and scope were exercised separately by the PostgreSQL/API tests.
- `dashboard-full-typescript-continuation.log`: the full single-process dashboard check exhausted its 1200 MiB Node heap inside the 1.5 GiB container cap and **did not pass**. Nine bounded checks covered 1,257 source roots, including a rerun of group 03 with test-only standard Next.js references and group 08 after the browser fixture update. See `tsc-shards-continuation-summary.log` and `tsc-shards-continuation/part-{03,08}-rerun.log`. These separate checks use test-only declarations and do **not** establish a full-project single-process pass; generated `.next` types were not present. No full dashboard build was claimed.

## Images and schema

Fresh offline-only images were built from backend HEAD and have no automatic service start:

- `twin-engine-offline-main:20261107-r3`: `sha256:13d993c6a0fc217a6249ff6cb985d74dd2a056b89aa276713f07192e3751328c`
- `twin-engine-offline-generic:20261107-r3`: `sha256:dcb478ec08b4054b40eb5ead321d1d030494f3f5be9f625978dce8bdf5459e22`

A new isolated, network-none lab database `twin_offline_qualification_0118_20261107_r3` was bootstrapped at `0118_engine_operator`; both images passed schema checks. Each passed three image-resident journey/recovery tests, and the generic image passed two tests against its actual imported vault module. See `continuation-image-schema-checks.log`, `continuation-{main,generic}-image-journeys.log` and `continuation-generic-vault-tests.log`. The inherited base has historical overlay labels; the qualification images replace `/app/service` with the cited new source and carry its revision label. Older images remain separate and must not be used against the new schema.

## Boundaries still requiring decisions or later qualification

- The independent-send proof is deliberately limited to application-verified arithmetic and a narrowly classified noninterfering pending action. General natural-language independence and the full conversation behavior require approved dependency rules, creator policy and a suitable model; the synthetic writer does not establish them.
- The HTTP listener and provider transport are loopback-only fixtures. Real Fans API behavior, operating permissions, credentials, event semantics, idempotency retention and provider recovery remain unqualified. The simulator's acceptance lookup is not a claim that the live provider offers one.
- Memory is withheld when binding/source provenance is incomplete. Catalogue absence is only called confirmed within a scoped, complete synthetic coverage record. Operational coverage, staff routing/escalation and live ownership/access evidence still need approved definitions.
- Deployment, production access/migration, live messages, purchases and metered calls each require separate approval. The existing Telegram and Moxy services were not modified or restarted.

The capped lab PostgreSQL container was left running with its isolated temporary data so evidence is not discarded. Detailed logs and the earlier handoff/status files remain in `/srv/projects/twin-conversation-engine-20261107/evidence`.
