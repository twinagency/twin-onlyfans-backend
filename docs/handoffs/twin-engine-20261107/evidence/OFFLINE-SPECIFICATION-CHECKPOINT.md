# Offline specification checkpoint — 7 November 2026

This is a partial, reviewable offline checkpoint. It is not full implementation or qualification of the twelve rules. Work Server project `/srv/projects/twin-conversation-engine-20261107`; backend commit `66b649a1a67c5ebdd19dbb06f1e70146dad8e77b`, dashboard remains `0fdb8b8c7a3796930688261f0f670350c4d9c466`. Existing work and services were preserved.

Changes: the new engine's prepare path no longer unconditionally imports the legacy conversation action classifier, and rejects a new-engine action carrying legacy decision/work identifiers. Existing account, transaction, delivery and prompt-placeholder safeguards remain. A new application-verified `identity.v1` independence proof allows an exact factual AI-identity answer after a narrowly classified noninterfering unknown greeting; model assertions alone cannot authorize it. Duplicate or conflicting pending identity claims remain held. Revision `0119_identity_independence` adds the proof type, preserves evidence on downgrade and validates the actual database constraint. A new integration test checks that corrected fan memory and history do not cross either creator accounts or fans.

Evidence: `identity-policy-regression.log` has 28 passing PostgreSQL cases including the existing policy and receipt regressions; `isolation-workflows-regression.log` has 14 passing memory, catalogue, operator and populated-upgrade cases; `identity-schema-final.log` has 3 passing schema/identity/isolation cases; `identity-two-unknowns.log` verifies both unknowns survive and the remaining unknown is promoted after the first is reconciled. The prior checkpoint's 48 unit tests, 87 PostgreSQL cases across two runs and browser/dashboard checks are recorded in `OFFLINE-CONTINUATION-STATUS.md`. Those earlier runs are not tests of every current change.

## Twelve-rule map

| Rule | Offline evidence and remaining gap |
| --- | --- |
| 1 priorities | Shared action/identity and transaction gates are present; approved authoritative creator-fact sources and conflict policy remain unresolved. |
| 2 voice | Deterministic synthetic responses only; no selected model or qualification of creator voice or permitted sexual conversations. |
| 3 follow-through | Durable requests, corrections and named offers are tested in fixtures; general model interpretation is not qualified. |
| 4 initiative | Fixture sales-signal handling exists; natural initiative and relevant offers require model evaluation. |
| 5 readability | Ordered multipart sends and correction fences are tested; variation and writing quality are unqualified. |
| 6 sales refusal | Durable pause/scoped reopening are exercised by simulated proposals; natural-language classification and approved commercial policy remain open. |
| 7 memory/isolation | Shared correction projection, stale-data fencing and cross-account/cross-fan tests pass; retention/deletion/access policies still need approval. |
| 8 catalogue | Scoped aliases, context, stale/incomplete search and matching are fixture-tested; real provider coverage and ownership completeness remain unverified. |
| 9 purchases/access | Separate synthetic offer, purchase, access and revocation evidence is projected; real provider evidence and approved remedies remain open. |
| 10 uncertainty | Same-action recovery and unknown preservation are tested; independent arithmetic and one verified AI-identity case are supported. Other natural-language responses remain held unless independence can be established. |
| 11 ordering | Multipart ordering, cancellation, retry and unknown preservation are tested in simulation; external sender integration remains unqualified. |
| 12 operations | Scoped audited handovers and dashboard states are implemented; approved operating definitions, service levels, model evaluations and live qualification remain open. |

The full single-process TypeScript check remains unresolved: the previous attempt exhausted a 1200 MiB Node heap under the 1.5 GiB cap. Nine bounded source groups passed with test-only declarations, but that is not a full-project pass. No new full-project pass is claimed.

The current loopback listener and HTTP adapter are real local HTTP code restricted to synthetic dependencies. There is no production Fans API adapter or live model implementation qualified by these tests. The concrete next qualification step is to choose the supported model and approve creator boundaries, provider permissions, authoritative purchase/access evidence, retry/idempotency and operational limits; then authorize a separately scoped credentialed sandbox test. No live calls or spending are authorized by this checkpoint.

The new backend schema head is `0119_identity_independence`. Earlier r3 images are for `0118_engine_operator` and must not be used against the new schema. Fresh offline-only r4 images were built from the saved backend commit and passed bootstrap/schema checks against a new isolated 0119 lab database. Each passed three image-resident journey/recovery tests, and the generic image passed two vault tests. They are not deployed or qualified for live use. Main image: `sha256:cc4b4f855401c47265169ebb78e2772fda3788850b25bebc7089486efbc86855`; generic image: `sha256:c54665a7c12d52c1faad6abcd9a05ebc34f46a3215fb82e1955126c68caf0c92`. See `specification-image-schema-checks.log`, `specification-{main,generic}-image-journeys.log` and `specification-generic-vault-tests.log`. Preserve all existing bots and the old database/image state.
