# Fan memory and model billing repair, 2 November 2026

Goal: improve the meaning and evidence of fan memories and account for model failures accurately, within the saved daily caps.

Latest source implementation: 2c30b6f17f3099cc7c6c28b2ef795fc337b7fc01. Initial accounting/memory implementation: c9da4d75081f0631dab1ba689a3ff59708307b6a, based on 766977a68fc0c43f8b35dfcaac70ff7acf94fb73. Existing rate and history changes remain in PR #77. Apply the focused overlay to the current engine rather than deploying this historical branch wholesale.

## Behavior

- Normalize a single explicit preference question plus yes/no answer into a concise statement, preserving both exact messages. Other paraphrases require a source excerpt. Negative and compound questions retain their original context.
- Give distinct confirmed activities distinct topic keys. Keep temporary work context separate from occupations. Exclude the specific joke name “Bond James Bond” from automatic identity facts. Preserve operator corrections.
- Rebuild affected materialized memories from valid archived observations without paid extraction, retaining receipts, scheduling, error state and archived evidence.
- Save HTTP status, request/generation IDs, reported usage/cost, latency, finish reason and failure details. A paid but unusable output retains its charge and its processing failure.
- Settle OpenRouter calls using reported cost when supplied, otherwise explicitly identify a token-rate estimate. Release undispatched requests or provider-reported zero-cost calls. HTTP failure or timeout alone does not prove zero billing.
- Add account cooldowns for HTTP failures/missing usage, daily retry delays after repeated model errors, and automatic generation-metadata reconciliation every five minutes. Each reconciliation lookup has a ten-second timeout and at most five recorded attempts; each tick handles at most five calls. Unknown calls without provider identities remain reserved.
- Provide a scoped accounting report separating active holds, unresolved holds, provider-reported cost and token estimates. Reconciliation locks the original UTC day ledger and call and only accepts matching provider generation evidence.

## Verified deployment

Initial deployment 2 November 2026 at 02:19:31 Brisbane; temporary-work follow-up deployed at 02:27:19 Brisbane over the exact existing API/worker images, under both release locks, with unchanged schema, read policy and daily caps.

Worker/importer/learner: sha256:f728669d70d9fcb89e34ced38e7efe808311293d0b175422b004a46114284fe5

API: sha256:04532a2d90349893407c08c77a883a6cc62c9ed8386d4eb29b21ab3fc9ef9975

All four services running with zero restarts after deployment. Reply supervisor remains stopped. Candidate source hashes matched committed source. A private profile projection snapshot preceded the repair. Twenty affected profiles were rebuilt across six stored accounts, including one paused account's stored data. This is a targeted repair count, not complete memory coverage.

## Validation

123 tests and four subtests passed against disposable PostgreSQL plus existing SQLite rehearsals in 144.15 seconds. Covered successful and malformed output, incomplete output, timeouts, HTTP rejection, zero reported cost, undispatched calls, missing usage, evidence/polarity, distinct topic keys, subject and temporal attribution, manual corrections, source withdrawal, queue locking, original-day settlement, mismatched generation rejection and repeat-safe reconciliation. The final follow-up passed 89 targeted tests plus four subtests in 57.56 seconds, adding four temporary-work wording cases and a concrete-occupation preservation check. Live projection checks confirmed temporary work and the joke alias no longer appear as occupations/identity, while a normalized supported preference remains. Existing datetime deprecation warnings remain.

## Legacy budget recovery, 2 November 2026

At 04:42:19 Brisbane, the six importing accounts' 1,874 legacy calls still lacked provider IDs. An authenticated OpenRouter export supplied 5,937 provider generation records. Metadata comparison found 2,884 ID or unique usage/time candidate matches across seven stored accounts without duplicate provider IDs; raw conversation contents were not used. These candidates were not treated as exact settlement of unidentified failed calls.

A one-time, conservative account-level reservation adjustment restored US$19.1440 of available software allowance. Original unknown reservations of US$29.7141 became US$10.5701 of explicitly unresolved reserves. Every affected call retained unknown status, its original estimate, a nonzero revised reservation, the provider evidence and batch identity. No call was marked uncharged or successful. Existing US$6/account/day caps and non-legacy ledger entries were preserved. Only budget-exhaustion holds/retry schedules were reopened.

The adjustment used authenticated /api/v1/key snapshots at 18:42:18 UTC on 1 November. The two saved keys reported UTC-day spend of US$2.558613983 and US$0.740327601, with zero BYOK usage. Each creator conservatively retained the entire shared key's reported daily spend plus 20% and US$0.10 as its account-level budget floor, including unrelated workloads and repeating the same shared-key floor across its creators. This is an uncertainty reserve based on reported provider evidence, not an allocation of actual per-call charges or a guarantee against later provider corrections. Existing completed-call token estimates remain labelled as estimates.

The six account ledgers immediately after adjustment were US$3.1704 each for Emma, Amelia, Maile, Cristina and Alva, and US$0.9884 for Naomi. They can accrue fresh model charges under the saved caps. The additional paused account's legacy reserves were outside this adjustment.

Validation: 15 tests passed against isolated PostgreSQL and pure allocation checks, covering a dry run, exact ledger/reservation balance, idempotency, retained unknown status, unchanged caps, rejected stale/wrong-scope evidence, credential revisions and changed snapshots. A private before-snapshot and fresh provider proofs were saved before the atomic update under both release locks. No application image or schema changed. The learner and reply controls retain their prior settings.

First verification, 57 seconds after adjustment: 528 model calls completed across all six creators and no active profile-account holds remained. This is model-call progress, not a count of complete fan histories.

Full message history import remains incomplete. This release does not demonstrate 90-minute onboarding or perfect semantic memory. The normalization deliberately covers supported cases rather than arbitrary inferred rewrites.
