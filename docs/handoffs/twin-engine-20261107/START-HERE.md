# Twin conversation engine — technical handoff

Prepared 7 November 2026 for Jesse's requested handoff. This is the new isolated conversation-engine build, not the older Moxy repair release.

## Current result

The saved application has a connected offline journey and tested durable state, scoped identity, corrections, offers, operator controls and recovery. The latest work adds Fans API protocol mapping, a configurable HTTPS primitive and a structured model-output contract.

**The offline implementation is still incomplete.** An actual conversational model and actual network sender are not wired into the new engine's durable authorization and settlement path. Full-project TypeScript validation failed twice under the agreed limit, and generated Next types were absent. No real model/provider qualification or deployment has occurred.

Do not interpret this handoff, its GitHub publication, or the passing fixture tests as production readiness or fulfilment of all twelve requirements.

## Exact source and access

| Component | Tested source commit | Repository |
| --- | --- | --- |
| Backend | `2688f3924f3902d00801cf98c8a02ba80d8f1738` | [twinagency/twin-onlyfans-backend](https://github.com/twinagency/twin-onlyfans-backend/tree/codex/twin-engine-claude-handoff-20261107) |
| Dashboard | `0fdb8b8c7a3796930688261f0f670350c4d9c466` | [twinagency/twin-dashboard](https://github.com/twinagency/twin-dashboard/tree/codex/twin-engine-claude-handoff-20261107) |

Both use the handoff branch `codex/twin-engine-claude-handoff-20261107`. The handoff branches add documentation-only commits above the source commits listed here. The offline images correspond to the source commits, not those later documentation commits.

Both repositories are private. Claude or another reader needs an authenticated GitHub connection with access to both repositories. A private URL by itself does not grant access. No collaborators or public visibility were added.

The dashboard was imported as a preserved source snapshot: its local root is `c5f1915`, whose commit message references upstream `0cab2aee`. This snapshot history has no common ancestor with the GitHub main revision inspected during packaging. The handoff branch preserves it as-is; it is not a merge-ready claim and must not replace main without a separately reviewed integration.

## Read in this order

1. This document and [COMPLETION-LEDGER.md](COMPLETION-LEDGER.md).
2. [Latest protocol/model-seam checkpoint](evidence/FANS-PROTOCOL-CHECKPOINT.md), including the final-image and full-TypeScript sections.
3. [Protocol capability matrix](evidence/FANS-PROTOCOL-MATRIX.md).
4. [Unchanged original twelve-rule specification](evidence/FINAL-TWELVE-RULE-SPECIFICATION.txt).
5. [Architecture and stage order](evidence/ARCHITECTURE-AND-TERRA-HANDOFF.md).
6. Earlier checkpoint reports for historical evidence only. Their test counts and next-stage wording belong to their recorded commits and dates.
7. [MANIFEST.json](MANIFEST.json) for source/image identities and evidence hashes.

The original specification is preserved as user-supplied requirements. Unsupported or unqualified clauses remain explicit gaps. This handoff does not direct bypassing any model/provider safeguard or claim that a different assistant can satisfy every clause.

## Where to continue

Jesse asked to finish the current assigned block, wrap up and hand over. Continue from the saved state after verifying it; do not restart architecture work, discard the existing implementation or quietly change the specification.

The next concrete engineering work is described in detail in the final checkpoint's “Engineering handoff for the unfinished authority integrations” section:

1. Add a durable engine-owned model invocation linked to the existing account-scoped registry and `GrokCall` / `GrokDailyCost` accounting owners. Bind request, account/tenant, creator/fan, owner/binding epochs, revisions, selected model, snapshot hash, lease and one-attempt authorization. Reserve and commit before a provider call. Do not fabricate a legacy Moxy or native-plan owner to bypass the current adapter check.
2. Wire the model invocation and usage settlement with injected mock HTTP, cancellation and stale-context fences. Preserve unresolved usage after ambiguous outcomes. The existing adapter currently accepts legacy owners or explicit qualification calls, not this engine's owner.
3. Bind actual provider sending to the committed one-use `TextAuthority` dispatch authorization. The new HTTPS primitive is not itself a safe application sender. Enforce identity/epoch again at the wire boundary; retain ambiguity and reconcile without blind replay.
4. Connect history and transaction observations through scoped read/ledger owners and coverage markers. Real purchase/access/reversal mappings require authoritative semantics; raw events do not become ownership by assertion.
5. Resolve complete dashboard validation without omitting production files or weakening types. If resource limits prevent completion, present the measured minimum isolated allowance for Jesse's decision. Generate and validate Next types before claiming a complete Next build.

These are remaining implementation items, not merely permission to run live tests. Preserve the original stage dependencies. A task requiring an unresolved business or model decision must name that dependency; do not invent a decision or skip it and mark the stage complete.

Natural-language interpretation, creator voice, initiative, broader independence after unknown sends and model/content compatibility remain unqualified. The current independent-reply proofs cover narrowly established arithmetic and AI-identity cases, not arbitrary changes of topic. Creator-fact conflict rules, memory retention/access, offer/remedy definitions, provider permissions and authoritative financial coverage still need explicit resolution.

## Important source entry points

Backend paths are relative to the backend repository:

- `service/conversation_engine/engine.py`, `worker.py`, `model_contract.py`: orchestration and fixture/structured proposal boundary.
- `service/conversation_engine/memory.py`, `history_scope.py`, `catalogue.py`, `offers.py`, `sequence.py`, `independence.py`, `operator.py`: current state and controls.
- `service/fans_api_adapter/protocol.py`, `https_transport.py`, `protocol_lab.py`, `intake.py`: documented protocol mapping and fixture integration.
- `service/onlyfans_runtime/text_delivery.py`, `provider_model.py`, `store.py`: existing submission, model authority and accounting integration points.
- `service/db/onlyfans_engine_models.py` and additive migrations: durable engine schema.
- `service/tests/conversation_engine/`, `service/tests/test_fans_protocol_lab_pg.py`, and the existing engine PostgreSQL tests: relevant regression coverage.

Use the dashboard repository's current OnlyFans inbox/operator components, `lib/onlyfans/` and API routes. Preserve existing facade permissions and uncertainty semantics. The [route matrix](evidence/contracts/dashboard-route-matrix.md) records qualification scope.

## Verification and images

Final source checkpoint evidence:

- 12 protocol/model-seam unit tests passed.
- 28 PostgreSQL regression tests passed.
- Each of the two final offline images passed four packaged journey/recovery tests.
- The generic vault image passed two additional vault tests.
- Both images passed fresh isolated schema checks at `0119_identity_independence`.
- The final full-project TypeScript retry exhausted the 1200 MiB V8 heap after approximately 212 seconds under a 1.5 GiB container cap. It did not pass. Smaller source checks and test-only declarations are not an equivalent full-project pass.
- Earlier browser/dashboard checks used simulated actions and retain only their stated revision coverage.

Final offline images, retained on Work Server:

- Main `twin-engine-offline-main:20261107-r6`: `sha256:a5ad3fed44dce93743484f013cf9b517a52b99f19e01bf18876fff6b801f09db`.
- Generic `twin-engine-offline-generic:20261107-r6`: `sha256:8d83a70bcb081136ea9be95c551db8e06b87a401b49e921e0ec8be3ee2d23bef`.

Older 0118 images must not be mixed with the 0119 schema. The original base worker image also predates the engine schema. Preserve existing images and databases.

The evidence directory includes the exact isolated qualification runners, image IDs, logs and historical failures. Some scripts reference the original Work Server paths and existing local base images. Review their assumptions before reuse. In particular, `qualify-protocol-final-images.sh` uses a uniquely named database; reusing its already-created name without adaptation will not be a fresh rehearsal. An empty log is not evidence of a pass.

## Work Server and operating boundaries

- Verified server: `work-server`, hostname `ubuntu-s-4vcpu-8gb-syd1`.
- Original development project: `/srv/projects/twin-conversation-engine-20261107`, with `backend/`, `dashboard/` and `evidence/`.
- Publication-only checkouts: `/srv/projects/twin-conversation-engine-handoff-20261107`. These hold the documentation commits; the original development checkouts were not reset or changed by packaging.
- Run heavy builds, tests, installations, databases and caches on Work Server. Do not silently move them to the laptop.
- Preserve the existing CPU/memory limits, separate lab databases/queues/ports and credential boundaries. Do not stop existing bots, clean up shared infrastructure or connect tests to live databases.
- No live provider/model calls, account reads/writes, messages, purchases, spending, production migration, deployment or activation are authorized by this handoff. Those require concrete separately scoped approval.
- Moxy/Moxie's paused two-creator development remains paused. Prior repair/pilot approvals do not authorize this build's live actions.
- Public documentation reads and authorised isolated offline engineering can continue. Never treat receipt uncertainty as permission to retry with a new action ID.

GitHub contains source and review evidence, not credentials, customer database dumps, running services or container image binaries. Server access and private base-image availability are separate from repository access. Keep secrets in the existing approved secret system; do not paste them into chats or GitHub.

## Suggested opening instruction for the receiving developer

Read this handoff and the pinned source commits first. Verify the source, environment and current permissions. Continue the first unfinished technical dependency in the existing stage order, starting with the durable model-invocation and sender-authority integration described in the final checkpoint. Keep the original requirements intact, identify unresolved compatibility/business decisions explicitly, and make no live calls or deployment without the corresponding authorization. Report implementation evidence separately from real-service qualification.
