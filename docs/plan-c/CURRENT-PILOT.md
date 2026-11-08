# Current Plan C development scope

Updated 8 November 2026. This is the current scope reference. The older `PILOT-CONFIG.md` and `HANDOFF.md` preserve historical decisions and results; their one-fan, $6/three-media and per-message approval values are superseded.

## Approved behavior

- One creator, Malie, with one durable account/binding and exactly two verified immutable fan IDs. Display names and login emails do not establish those bindings.
- One approved photo at **USD 3.00**, with no bundles, discounts, customs, proactive campaigns or timer-based reopening of declined sales. The selected local photo must still be independently bound to the correct provider media/version before live use; fixture IDs are not a substitute.
- `authorization_mode=autonomous`: routine replies and the approved card use explicit durable machine-policy authorization through the existing delivery authority. A named administrator manages configuration, stop and recovery; there is no per-message human approval requirement.
- One qualified DeepSeek model and upstream, with fallback disabled. Exact current model availability, provider identity, routing and rates still require separately authorized qualification. Synthetic responses do not establish model quality.
- The same approved creator profile must be used for writing, validation, snapshots and authorization. The approved identity answer is: “I'm an AI assistant helping with Malie's messages.”
- Memory is creator/fan scoped. Manual corrections and clears take precedence; selected learned evidence retains uncertainty, source and relevant dates. Historical requests and unresolved actions are context, not new send or purchase authority.
- Destination, price, media, purchase/access evidence, ownership, revisions, limits, duplicate protection and unknown-send holds remain enforced by the application. A paid card, provider acceptance, unlock/access and financial settlement are distinct facts.

## Configuration and startup boundaries

The authoritative contract is `service/conversation_engine/pilot_scope.py` (`validate`, `configure`, `set_status`) and the worker builder in `service/model_transport/pilot_supervised.py`. Do not copy the old provisional configuration into those APIs. No real account configuration or credentials are supplied by this document.

The worker remains default-off. Its existing environment flag is still named `TWIN_PLAN_C_SUPERVISED`; that name does not override the durable authorization mode. A worker built while the scope is stopped retains evidence in intake-only mode. Activating the durable scope does not silently transform that process into a sender: a separately controlled restart is required.

`/healthz` reports process liveness. `/readyz` reports loop progress, mode and bounded counts for unresolved work. A healthy loop is not proof that every conversation can send. `workHeld` and the counters expose retained barriers; malformed account evidence also makes `sendingReady` false. Counts cap at 101 with `countsCapped=true`. Fan-scoped holds do not authorize retries or erase the other fan's state.

Administrator session revocation removes dashboard control access. The engine's durable policy is separate: stopping/revising that policy invalidates affected unsent authorizations. Before live operation, verify the intended account-offboarding/revocation procedure across the dashboard and worker; do not assume revoking a browser session stops an already-authorized autonomous policy.

## Qualification and retained gates

Use one exact backend/dashboard commit pair and the corresponding immutable image digest. The additive `0126_plan_c_ingress` migration installs a safe ingress metadata parser; old images must not be used with a newer schema. Do not infer compatibility merely from older `0125_plan_c_scope` revision labels, which were reused during early development. Every test uses an isolated database. Preserve evidence and matching images; never force a downgrade to erase scope, delivery or unresolved records.

The current offline work uses synthetic model/provider transports on Work Server. No deployment, live messages, purchases or metered provider/model calls are authorized by this scope reference.

Before any separately authorized live qualification, resolve real creator/fan/media bindings, model/provider details and spend caps; verify every competing sender and existing provider queue without cancelling it; establish quiescence for previously admitted work; and qualify backup/restore and compatible rollback. Malformed signed-event holds need positive authoritative evidence and audited resolution. They remain held while that evidence/workflow is unqualified. Do not fabricate proof, clear unknowns, or resend them under a new action key.

Broader Moxy/VIP progression, multi-product sales strategy and new incentive policies are deferred. They are not part of completing this one-photo pilot.
