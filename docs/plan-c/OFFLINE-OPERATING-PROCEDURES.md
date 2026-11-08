# Current offline operating procedures and compatibility

These are reviewable development procedures, not authorization to run them against real accounts. Real master owns planning and scope; this chat is the sole implementer. No deployment, pushes, production changes, actual messages/purchases, metered/live provider/model calls or permission changes are authorized. All implementation and heavy qualification use Work Server and isolated databases with scripted external services.

The pilot stays Malie, exactly two verified allowed fan IDs, one approved photo at USD 3, autonomous policy authorization, one configured DeepSeek/upstream and fallback disabled. No extra product, price, campaign, custom promise, automatic expiry extension or timer reopening is introduced.

## Source, schema and evidence

The implementation branch is codex/plan-c-offline-completion-20261108, based on reviewed 9eb8d8f2054dd399e67b29ac2bb268840335403f. The original cdae299 dirty builder and independent audit snapshot are preserved. Dashboard 3cedc8f5c6d3c9f493d6ef35f4162a5207b5b589 is unchanged; its existing parser displays unknown unresolved kinds as review-only. No UI modification or duplicate dashboard is necessary.

Current migration head is 0128_model_billing_evidence. 0127 adds immutable issuance/predecessor/reason/authorization evidence to the existing grant table, preserving old rows as initial issuances and retaining uniqueness at the database boundary. 0128 adds nullable provider generation/endpoint correlation to the existing model-call ledger. Old calls are not assigned guessed IDs. Readiness checks verify actual schema structure, guards and uniqueness; an old image must not be used on a newer schema. Downgrade refuses after new issuance/correlation evidence exists. Development migration tests are not a production backup or rollback qualification.

Every final qualification must identify the committed source, immutable image and dashboard revision. Earlier image results remain historical evidence. Intermediate tests on a read-only source overlay are explicitly not final exact-image qualification. Authoritative commands, hashes and exit results are in ../codex-plan-c-qualification-20261107/OFFLINE-COMPLETION-20261108-v1; the final consolidated report identifies the exact files.

## Durable HTTP intake and recovery

The signed HTTP route calls handle(..., ingest_only=True). It authenticates and commits evidence and receipt-time barriers before 202. It performs no model/send work and creates no request-local background coroutine. Existing polling owns planning, autonomous authorization and delivery. A crash after acknowledgement is recovered from durable rows; a persistence failure returns failure, and invalid signatures create no trusted evidence. Stops remain effective when followed by greetings, including after a dispatch grant.

Direct library handle keeps its previous default for existing callers. Stopped processes retain signed intake in IntakeOnlyPilot. Restarting a stopped process does not enable a sender; a separately controlled restart after explicit activation is required. Existing open requests from this pilot can resume after relevant repair; historical/pre-activation intake without such a request remains inert. Unchanged held offers reuse their existing dependency record instead of consuming model calls. Current repaired authority wakes the same request; controls and uncertainty still recheck.

## Stopped setup and explicit renewal

Use the existing authorized configure/status/policy APIs with current account/binding, named actor, expected revisions and fresh command keys. Stop the pilot first; disable the delivery policy before changing configuration. Read the current product with pilot_scope.offer_coverage_snapshot and an explicitly supplied vault evidence reference and UTC expiry. This reader creates no permission or grant. It produces snapshot_kind=pilot-product.v1 for the current approved product; the subsequent configure command authorizes and audits the exact coverage change. Complete the existing guarded FansBackfill through the configured allowed client; do not seed completion or copy the fixture's 2015 coverage date into production.

The pilot fingerprint includes the approved product/binding, fixed amount/currency, frozen contents/previews/state/description, current meaningful aliases and referenced media version/provenance/removal. It reuses aliases_for, so obsolete aliases already excluded by catalogue retrieval do not invalidate permission. General catalogue completeness still uses the original whole-catalogue fingerprint. Legacy offer_coverage without snapshot_kind keeps its old semantics; it is never silently reinterpreted. Unknown versions are refused. Under the existing frozen terms contract, even a timestamp-only referenced-media sync changes the version and requires explicit review/refresh; provenance checks are retained.

For expiry renewal, provide fresh current coverage through the stopped, disabled, authorized configure path. Its audit records exact before/after coverage. A generic restart, stop/start, elapsed time or unchanged configuration does not renew permission. Re-enable only through the existing activation prerequisites and controlled restart. Normal preparation issues one usable successor only when the expired predecessor was unused or conclusively never dispatched, current eligibility passes and explicit renewal evidence exists. Unchanged duplicate configuration commands do not bury the real approval. The old expiry and used intent remain unchanged; all spending/uncertainty is retained. Changed frozen commercial terms are not silently reauthorized under stale permission.

For a corrected cancelled card, the existing cancellation and final-wire fences must prove it never dispatched and cannot still dispatch. A distinct successor records its predecessor, reason, current policy/evidence and cancellation proof, while old authority remains immutable. Queue accounting is preserved and the new action has a new identity. Accepted, unknown, unfenced or owned actions cannot receive replacement authority. A writer-facing grant must pass current eligibility; context reads create or consume no authority. Final authorization and dispatch revalidate independently.

## Exact retained-event classification

The existing backend operator route POST /onlyfans/control/v2/accounts/{account}/unresolved/{id}/reconcile supports exact provider_event items. GET unresolved exposes eventEpoch, eventKey, payloadSha256 and scopeRevision with a binding-derived item ID. The private operator API requires its existing bearer/schema header and authorized named actor. Its 8KB command limit is unchanged.

Body fields are commandKey, actor, evidenceRef, scopeRevision, eventEpoch, eventKey, payloadSha256 and rawBody. Original bytes must hash to the retained authenticated digest and match the account/epoch/binding. Only one of the five documented strictly shaped nonnegative account counters can be classified irrelevant. The existing immutable command audit records that exact classification; the original digest is never edited. Different reviewers/duplicate commands converge on the same canonical resolution. A later conflicting payload re-holds even a previously classified event. One resolution clears no other digest or provider projection.

The dashboard shows these items as review-only; its generic evidence-note button is not a clearance path. The extended classification command is a backend administrative operation. Unknown posts.new is not listed in the provider's documented event list and stays held. Deletions, financial/access/account events, malformed bytes, conflicting keys or missing original payloads cannot be dismissed by this route. Their authoritative reconciliation remains an operational blocker, not a reason to add a blanket clear/retry.

The 10,000-row lifetime insertion cap is removed without deleting or compacting evidence. Digests and normal events preserve duplicate/conflict checks across both paths, including legacy digest pairs with missing old safety flags. Queue, readiness and send barriers use the same unresolved view. Storage growth/backup monitoring and any future retention deletion policy remain operational concerns; no retention policy was invented here.

Provider reference: https://docs.onlyfansapi.com/webhooks/available-events ; delivery contract: https://docs.onlyfansapi.com/webhooks/delivery-and-retries . Documentation was read without credentialed calls.

## Manual provider-linked model accounting

Jesse confirmed that administrator cost attestation is insufficient; evidence must tie to the specific call. Jesse then approved a manually invoked command under the existing worker credential owner. service/tools/plan_c_reconcile_model.py accepts tenant, account, call-id, actor, command-key, expected-pilot-revision and reason. It accepts no cost and no provider ID. It is never invoked by startup, polling or the API role and introduces no new queue/service/API writer credential.

The model adapter captures a provider-issued generation ID plus exact endpoint fingerprint when available, including a provider-reported partial-error envelope. The manual command uses the configured worker adapter to make one authenticated generation-metadata GET outside all database locks, verifies the saved ID/model/endpoint and terminal cost evidence, then records cost and audit atomically under existing budget/runtime/call lock order. No generation or send occurs. Failed, incomplete or mismatched evidence changes nothing. Replayed commands do not fetch again; concurrent readers can make two metadata reads but only one cost settlement/audit succeeds.

Costs use the existing nearest-microUSD ledger unit and retain exact reported USD in audit evidence. Provider-proved zero is permitted; absence of data is not zero. Actual over-bound liability remains counted and blocks further spend through existing caps. Verified usage cannot be overwritten by late completions or database updates. Unknown output/identity remains held even after cost becomes known, and the command grants no retry/send authority. A known verified response can settle its missing usage without changing its decision/approval. Old or timed-out calls with no captured provider correlation remain reserved and unresolved; no ID is attached on administrator assertion.

Reference: https://openrouter.ai/docs/api/api-reference/generations/get-generation . Qualification uses httpx.MockTransport only. Running this command with real provider credentials is live provider activity and is not authorized by this offline block.

## Existing explicit account offboarding procedure

Administrator/dashboard access and durable autonomous policy are separate authorities. Session revocation alone is not proof that a running policy stopped; no automatic offboarding rule is added.

The supported sequence is: identify the current named operator/account/binding and revisions; explicitly stop the pilot using the existing status API; disable the shared delivery policy without resetting counters/records; verify stale workers cannot plan/send; restart only in intake-only mode as appropriate so receipts/stops/financial observations remain durable; then revoke the administrator's access through the existing account/session system. Retain unknown model and send records and their original identities. Already admitted or uncertain provider work is not assumed recalled; inspect actual competing writers and provider queues during the separately authorized cutover stage.

Offline browser qualification tests this sequence against the actual Next app, API and named-owner SQL with a simulated RPC/provider/model. It also checks that revoked owner access is denied and that unknown sends/spend counters survive. Until its final result exists, the prepared browser script is not a passing result. Real session infrastructure, access procedures and deployed isolation still need live-stage verification; deleting accounts or sessions is not performed in this block.

## Live-readiness gates retained

No fixture establishes the real creator/fan/media identities, actual approved durable Malie profile, provider subscription/permissions, model quality/rates/routing, deployed writer isolation, legacy/provider-side queued work, source-incident replay, or restore-tested backup/compatible rollback. Verify the approved factual/private profile representation before live use; do not infer adulthood, personal facts or new product/contact permission. Bounded rendered memory deliberately omits material and reports omissions; historical/cancelled/expired memories are not current facts or new action authority.

Independent review materials and the complete normal/failure-path matrix belong in the final consolidated report. No offline pass authorizes activation or promises universal natural-language/provider behavior.
