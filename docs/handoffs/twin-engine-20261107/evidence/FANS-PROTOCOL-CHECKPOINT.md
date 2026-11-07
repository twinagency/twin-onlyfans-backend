# Fans protocol and model-seam offline checkpoint — 7 November 2026

Work Server: ubuntu-s-4vcpu-8gb-syd1, /srv/projects/twin-conversation-engine-20261107. Backend: 2688f39 (protocol work 07e7614 plus opaque-marker correction). Dashboard: 0fdb8b8c7a3796930688261f0f670350c4d9c466. This is an offline checkpoint, not completed provider or model qualification. Preserve earlier commits, evidence, images and databases.

## Additional work

The credential-configurable Fans API protocol mapper now covers scoped account identity, message history and continuation, send serialization and idempotency, transaction pagination, chargeback observations and authenticated webhook observations. An explicitly enabled HTTPS primitive uses one attempt, fixed provider host, no redirects, disabled environment proxy/trust settings, timeouts and bounded response bytes. It is tested using httpx MockTransport, never registered with the application sender and never called against the provider. Connecting a live HTTP sender must enforce the same one-use durable delivery authority and cannot be inferred safe from this primitive alone.

The protocol-shaped synthetic transport only accepts the in-process fixture provider and carries the mapped send through the existing text authority. It checks account, creator and epoch. Durable webhook observations reject retired epochs and retain duplicate/conflict evidence; non-message events do not create a verified purchase, access, refund or ownership. A structured JSON proposal contract and in-memory structured fixture exercise the engine's existing proposal validations and revision fences. No actual model invocation is wired to the new engine; the existing registry/model adapter and cost ledger must be connected through a durable, scoped engine-owned invocation and settlement path before qualification.

## Specification map

The evidence below includes the preceding checkpoint OFFLINE-SPECIFICATION-CHECKPOINT.md and OFFLINE-CONTINUATION-STATUS.md; previous results refer to their stated commits, not all code in this new commit. New tests do not demonstrate general model quality.

| Rule | Evidence and boundary |
| --- | --- |
| 1 priorities | Previous policy/identity regressions test the new mechanical route and retained account, transaction and delivery gates. Approved creator-fact sources/conflict policy and a real model remain open. |
| 2 voice | Structured and deterministic fixtures are exercised. Creator voice and model behaviour are unqualified. |
| 3 follow-through | Durable requests, corrections and offer fixtures exist; the model's natural interpretation is not tested. |
| 4 initiative | Synthetic sales signals and offer matching are tested; natural initiative is unqualified. |
| 5 readability | Multipart order, stale fences and fixture replies are tested; actual output quality is unqualified. |
| 6 refusal | Durable pause and scoped reopening use simulated proposals; real language classification remains unqualified. |
| 7 memory and isolation | Prior memory correction, binding fences and cross-account/cross-fan regression evidence remains applicable to its recorded commit; retention/deletion/access policy is unresolved. |
| 8 catalogue | Prior scoped aliases and coverage-aware search tests exist; real provider coverage and ownership completeness are unverified. |
| 9 purchase/access | Synthetic purchase/access/revocation fixtures exist. Protocol observations remain unlinked; canonical real-provider financial, reversal and access semantics need evidence and qualification. |
| 10 uncertainty | Arithmetic and a specifically proven AI-identity reply can proceed with earlier unknowns; other actions remain held unless independence is proven. Unknown receipt and reconnect cases are tested. General conversational independence is not established. |
| 11 ordering | Prior multipart/retry/correction tests and current synthetic protocol recovery tests cover ordering and unknown preservation. A real provider sender is not integrated or qualified. |
| 12 operations | Prior scoped audited operator workflows and dashboard/browser fixtures are recorded. Real operating rules, service levels and live qualification remain unresolved. |

## Verification and limits

protocol-dev7-unit.log: 11 tests passed before the opaque-marker addition. protocol-dev7-postgres.log: 28 tests passed, including structured JSON through the synthetic protocol/authority/receipt journey, reconnect isolation, delayed transaction observation, uncertain-send recovery, ordering and controls. The final-commit image and regression evidence is in protocol-final-* logs.

dashboard-full-listfiles.log resolved 2,490 files within the cap. dashboard-full-typescript-continuation.log records the full check exhausting a 1,200 MiB Node heap under the 1.5 GiB container limit. Nine bounded checks with test-only declarations are not equivalent to a full-project pass; generated .next types and next-env.d.ts were absent. No full TypeScript pass or complete dashboard build is claimed. Existing browser evidence is unchanged and uses simulated actions.

No live provider/model calls, purchases, customer messages, deployment, or changes to existing services were performed. This checkpoint does not authorize any such action.

## Concrete next work and qualification

Complete the durable new-engine model invocation linked to the existing account-scoped model registry and cost ledger, including a committed call record, one-attempt authorization, settlement, stale-context fencing and simulated failures. Connect real provider operations to the existing sender/read/ledger authorities with evidence-backed financial and recovery semantics; preserve unknowns and unlinked observations. Resolve the full TypeScript check without weakening it or obtain a separately agreed isolated resource allowance. Only then select/confirm the actual account model and operating definitions and request separately authorized, bounded credentialed sandbox qualification. Real conversational-model quality requires an evaluation on representative approved examples; fixture success cannot establish it.

## Final-commit image evidence

Backend commit: 2688f3924f3902d00801cf98c8a02ba80d8f1738. Working tree clean after commit. Dashboard commit: 0fdb8b8c7a3796930688261f0f670350c4d9c466. Schema head: 0119_identity_independence. New isolated qualification database: twin_offline_qualification_0119_20261107_r6 in the network-none capped lab PostgreSQL container.

- Main: twin-engine-offline-main:20261107-r6, sha256:a5ad3fed44dce93743484f013cf9b517a52b99f19e01bf18876fff6b801f09db.
- Generic vault: twin-engine-offline-generic:20261107-r6, sha256:8d83a70bcb081136ea9be95c551db8e06b87a401b49e921e0ec8be3ee2d23bef.
- protocol-final-image-schema-checks.log: fresh bootstrap and both image checks succeeded at 0119.
- protocol-final-main-image-journeys.log and protocol-final-generic-image-journeys.log: 4 passed in each.
- protocol-final-generic-vault-tests.log: 2 passed.
- protocol-final-unit.log: 12 passed, including opaque transaction marker handling.
- protocol-final-postgres.log: 28 passed. Warnings were recorded in the log.
- qualify-protocol-final-images.sh is the reproducible isolated runner. It creates a uniquely named database and must not be rerun with the same database name unless explicitly adapted.

Existing r3 images target 0118 and must never be mixed with 0119. Previous r4 and r5 images and databases remain intact. These images are offline qualification artifacts, not deployments.

## Engineering handoff for the unfinished authority integrations

1. Add an engine-owned durable model invocation linked to the existing GrokCall / GrokDailyCost owner. Bind account, tenant, creator binding, fan, engine request, owner epoch, current input/control revisions, selected model, prompt/snapshot hash, lease and one-attempt state. Reserve and commit the budget before any provider call; do not fabricate an OfNativePlan or MoxyEvent. NativeProviderModel.generate currently refuses the new engine because it accepts only those owners or an explicit qualification call.
2. Add an engine worker adapter around the existing registry client which proves that committed engine owner, consumes one call attempt, uses the selected account's exact configuration, settles actual usage or retains the reservation as unknown, and applies the existing post-generation stale snapshot/owner/connection fences before persisting a decision. Test with injected mock transport only: scope/model mismatch, uncommitted or expired lease, duplicate attempt, cancellation, malformed response, missing usage, cost cap and concurrent correction. Preserve the offline-only default.
3. A real FansApiClient.send is a low-level primitive: it must not be admitted as a worker transport on its own. Implement an authority-bound transport that requires the already committed one-use TextAuthority dispatch and validates connection/creator epoch at wire time; persist receipt and ambiguity without automatic replay. Tests must show direct/unowned submission is refused, identity replacement cannot send, uncertain acceptance remains unknown, late matching evidence resolves without a second wire call, and unrelated replies need application-verified independence.
4. Integrate provider history and transaction observations through existing scoped read/ledger owners and coverage markers. Validate provider-specific purchase, unlock and reversal semantics before converting observations into entitlement or ownership; never infer it merely from amount, fan or timing. Reconnect epochs and conflicting webhook payloads must remain independently retained.
5. Fix full-project TypeScript validation under the agreed cap without omission, weakened type options or fixture-only declarations. If a complete check cannot fit, retain the failure and propose a specifically bounded isolated higher-memory run for separate approval. Generate/verify Next types in an isolated environment before claiming a complete build.

No selected model, provider credentials, billing changes, production registration or live tests are implied by these steps.

## Full TypeScript retry

dashboard-full-typescript-memory-tuned.log: a fresh full tsconfig.json check with --incremental false, --extendedDiagnostics, Node --max-old-space-size=1200, --max-semi-space-size=4 and --optimize-for-size, under the same 1.5 GiB container / 0.75 CPU cap, exited 1 after V8 heap exhaustion at approximately 212 seconds. This second failure does not establish a full-project pass. The run used the checkout as present, which lacks generated .next types and next-env.d.ts; even a compiler success in this state would need a separate generated-type check before claiming a complete Next build.
