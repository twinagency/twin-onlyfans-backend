# Fan memory and model billing repair, 2 November 2026

Goal: improve the meaning and evidence of fan memories and account for model failures accurately, within the saved daily caps.

Source implementation: c9da4d75081f0631dab1ba689a3ff59708307b6a, based on 766977a68fc0c43f8b35dfcaac70ff7acf94fb73. Existing rate and history changes remain in PR #77. Apply the focused overlay to the current engine rather than deploying this historical branch wholesale.

## Behavior

- Normalize a single explicit preference question plus yes/no answer into a concise statement, preserving both exact messages. Other paraphrases require a source excerpt. Negative and compound questions retain their original context.
- Give distinct confirmed activities distinct topic keys. Keep temporary work context separate from occupations. Exclude the specific joke name “Bond James Bond” from automatic identity facts. Preserve operator corrections.
- Rebuild affected materialized memories from valid archived observations without paid extraction, retaining receipts, scheduling, error state and archived evidence.
- Save HTTP status, request/generation IDs, reported usage/cost, latency, finish reason and failure details. A paid but unusable output retains its charge and its processing failure.
- Settle OpenRouter calls using reported cost when supplied, otherwise explicitly identify a token-rate estimate. Release undispatched requests or provider-reported zero-cost calls. HTTP failure or timeout alone does not prove zero billing.
- Add account cooldowns for HTTP failures/missing usage, daily retry delays after repeated model errors, and automatic generation-metadata reconciliation every five minutes. Each reconciliation lookup has a ten-second timeout and at most five recorded attempts; each tick handles at most five calls. Unknown calls without provider identities remain reserved.
- Provide a scoped accounting report separating active holds, unresolved holds, provider-reported cost and token estimates. Reconciliation locks the original UTC day ledger and call and only accepts matching provider generation evidence.

## Verified deployment

Deployed 2 November 2026 at 02:19:31 Brisbane over the exact existing API/worker images, under both release locks, with unchanged schema, read policy and daily caps.

Worker/importer/learner: sha256:1b823e21ee9fd308ee5f86959c99d2417e84d63334e423ff1e43670175fc791c

API: sha256:d95d300aad9dc7352b3aaa664437db62e9f2d43a6cb0aed3a1301f8e8deded0c

All four services running with zero restarts after deployment. Reply supervisor remains stopped. Candidate source hashes matched committed source. A private profile projection snapshot preceded the repair. Eighteen affected profiles were rebuilt across six stored accounts, including one paused account's stored data. This is a targeted repair count, not complete memory coverage.

## Validation

123 tests and four subtests passed against disposable PostgreSQL plus existing SQLite rehearsals in 144.15 seconds. Covered successful and malformed output, incomplete output, timeouts, HTTP rejection, zero reported cost, undispatched calls, missing usage, evidence/polarity, distinct topic keys, subject and temporal attribution, manual corrections, source withdrawal, queue locking, original-day settlement, mismatched generation rejection and repeat-safe reconciliation. Existing datetime deprecation warnings remain.

## Billing limitations

The six importing accounts still have 1,874 legacy unresolved calls reserving US$29.7141. None retained generation IDs. Their 2,611 completed calls have US$6.2703 of token-rate estimates, which are not invoice-verified charges. An additional paused account has US$0.9088 in unresolved reservations and US$0.6815 in token estimates, outside the original six-account figures.

The two saved shared OpenRouter inference keys returned daily usage of US$2.426356107 and US$0.3251021 at inspection. These are provider key-wide figures, potentially covering other workloads, not per-creator or per-call attribution. Both returned HTTP 403 from historical analytics, which requires management access. No legacy reservation was released or marked zero without supporting evidence. Resolving those old calls requires historical billing access/export and a defensible match. Fresh memory extraction remains constrained by existing account holds/caps; daily allowance rolls over at UTC midnight (10 a.m. Brisbane).

Full message history import remains incomplete. This release does not demonstrate 90-minute onboarding or perfect semantic memory. The normalization deliberately covers supported cases rather than arbitrary inferred rewrites.
