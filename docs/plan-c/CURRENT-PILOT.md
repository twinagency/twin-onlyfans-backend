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

Administrator session revocation removes dashboard control access. The engine's durable policy is separate: stopping/revising that policy invalidates affected unsent authorizations. The existing explicit procedure is stop the pilot, disable delivery policy while retaining counters and unresolved records, verify the worker boundary, retain intake-only evidence processing, then revoke owner/session access. See `OFFLINE-OPERATING-PROCEDURES.md`. No automatic offboarding rule is added; the actual deployed sequence still requires separately authorized qualification.

## Qualification and retained gates

Use one exact backend/dashboard commit pair and the corresponding immutable image digest. The current head is `0128_model_billing_evidence`: `0126_plan_c_ingress` retains safe ingress parsing, `0127_offer_grant_lineage` preserves immutable authorization issuances, and `0128` retains provider call correlation. Old images must not be used with a newer schema. Do not infer compatibility merely from older `0125_plan_c_scope` revision labels, which were reused during early development. Every test uses an isolated database. Preserve evidence and matching images; never force a downgrade to erase scope, delivery or unresolved records.

The current offline work uses synthetic model/provider transports on Work Server. No deployment, live messages, purchases or metered provider/model calls are authorized by this scope reference.

Before any separately authorized live qualification, resolve real creator/fan/media bindings, model/provider details and spend caps; verify every competing sender and existing provider queue without cancelling it; establish quiescence for previously admitted work; and qualify backup/restore and compatible rollback. Exact retained documented counter events have a narrow audited backend classification command. Malformed, material, unknown or conflicting events remain held; this is not a general clear control. Model costs require provider proof tied to a saved call and can be reconciled only by the manually invoked worker-owner command; uncorrelated costs stay reserved and unknown outcomes stay held. Do not fabricate proof, clear unknowns, or resend them under a new action key.

Broader Moxy/VIP progression, multi-product sales strategy and new incentive policies are deferred. They are not part of completing this one-photo pilot.
