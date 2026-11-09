> Chronological necessity review. Proposal/pending statements below describe the pre-decision checkpoint; the final entry records the approved and implemented boundary.

# Provider-linked model accounting: decision boundary

Confirmed user decision: require provider evidence tied to the specific model call; administrator cost attestation is insufficient. Unprovable costs remain reserved. No reconciliation implementation is authorized by the existence of an old library setter alone.

Demonstrated missing capability: model_transport/openrouter.py discards the provider response generation ID; ChatResult and OfEngineModelCall have no generation-ID field. model_ledger.note_identity retains model identity in a structured log, not a provider billing correlation. The operator v2 route has no provider-authenticated billing reader, and accepts no model-call settlement command. The existing reconcile_unknown library function accepts a supplied integer, lacks actor/evidence audit and changes status to settled; exposing it directly would not satisfy the confirmed evidence rule and could change polling/retry eligibility.

Existing owners: OpenRouterClient / ChatResult, OfEngineModelCall and ModelLedger, existing operator authorization and OfLaunchCommand audit. Unknown delivery/action identity remains owned by its separate sender and is not settled by model cost evidence.

Primary provider documentation: https://openrouter.ai/docs/api/api-reference/generations/get-generation . Generation metadata is queried by the returned ID and includes total_cost. Documentation read only; no credentialed/billing/model request was made.

Smallest proposed completion (needs operating-boundary decision): retain a bounded provider generation ID on the existing model-call ledger at response recording, with immutable identity and uniqueness. A manually invoked reconciliation command executed by the existing worker credential owner would authorize the current named operator/scope, perform one authenticated metadata lookup through the configured model adapter, match the exact saved generation/model/account context, record provider evidence and cost in the existing ledger/audit, and preserve any unknown output/identity/retry hold. No automatic job, new store, API model credential, cost guess, model-generation call or retry permission. Calls lacking a captured provider ID remain reserved.

Alternative: leave this capability blocked for real master to choose a trusted provider-evidence execution path in the next stage. The API process must not acquire writer credentials as an incidental repair; unsigned administrator-provided billing JSON is not provider proof.

No implementation of either new execution path has been made. The decision affects this accounting capability only; independent offline repairs and final qualification can continue. Existing reservations and unknown outcomes remain unchanged.

Decision received from Jesse: use the existing worker owner for the manual reconciliation command. Implement only that path. No API model credential, automatic polling, administrator-supplied cost or provider-ID attachment, or release of unknown outcomes. The implementation and tests remain uncommitted/unqualified until recorded below.

Approved implementation checkpoint: existing-worker manual command and additive correlation migration are implemented, not exposed to the API or polling. Intermediate model-billing-v2 passed 121 affected checks, including late-cost overwrite fencing; final committed-image qualification remains required. No real provider metadata call was made.


## Canonical model evidence for future calls

The approved worker-owner command can record an authenticated provider mapping before any new model reservation:

    python -m tools.plan_c_reconcile_model --pin-canonical --tenant TENANT --account ACCOUNT --actor ACTOR --reason "provider mapping qualification" --expected-pilot-revision REVISION

Run it using the existing worker role and credential configuration, with real identifiers and the current approved revision. It reads provider metadata and records an immutable scope-bound audit; it does not make a generation call or authorize sending. Each subsequent reservation snapshots that mapping in the same database transaction. Reconciliation may accept a differently named provider model only if the exact saved generation's authenticated billing record agrees with that pre-reservation snapshot. Missing, late, conflicting or ambiguous evidence keeps the reservation. A current catalogue mapping cannot retrofit an earlier call. Existing unknown-output and sending holds remain unchanged.

Pilot preflight calls made before a usable per-call mapping remain reserved unless separate call-specific evidence supports settlement. Their full reservation must be included in the ceiling. The AWS isolated preflight currently holds $0.20 outside the application ledger, so the app's independent maximum must be set to at most $4.80 and actually enforced before activation; this operating condition is not satisfied merely by this note.
