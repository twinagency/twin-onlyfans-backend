# User-forwarded repair boundaries v1.1 — 8 November 2026

The full addendum was supplied in this implementation chat after the initial challenge probes. Keep branch codex/plan-c-offline-completion-20261108 and all existing scope restrictions. This document records the operative constraints for continuation and review; it is not test evidence.

## Conflicts recorded before resolution
- The prior register's general expired-authority no-renewal wording must distinguish automatic extension (forbidden) from explicit stopped administrator renewal with fresh valid coverage (required). The original grant stays expired/consumed. No successor/renewal code existed when this addendum was read.
- A second grant cannot be inserted under the old account/fan/item/terms uniqueness alone. Removing that protection alone or mutating used_intent_id is forbidden. The required repair is auditable issuance lineage inside the existing grant system, not a separate authorization service.
- General catalogue completeness must retain its existing fingerprint semantics. The pilot needs a narrowly versioned product permission fingerprint; old whole-catalogue evidence cannot be reinterpreted silently.
- The reviewer observed no runtime changes at 11:20:09 UTC. Since that observation, the separate acknowledgement repair changed plan_c_worker.py and pilot_supervised.py only. Grant, renewal and coverage implementations remain the reviewed base at addendum receipt.

## Grant lifecycle contract
Preserve old terms, expiry and used intent. Identify root and successor issuance, predecessor, reason and authorizing policy/evidence. Enforce one initial grant per scope/terms and at most one successor per predecessor at the database boundary, including concurrent workers. Select only the eligible unused leaf. Accepted, unknown or unfenced dispatch prevents replacement.

Use existing runtime/policy/intent lock ordering. Verify actual retirement and that an earlier worker/grant cannot still dispatch; absence of a receipt or a cancelled label alone is insufficient. Recheck binding, policy, fan controls, current product/price/media, ownership/history, coverage and budgets. Atomically record successor/reason/evidence, preserving accounting; the successor action gets its own identity. An additive issuance/predecessor migration is permitted when necessary and must preserve initial records and uniqueness guarantees. Review every scalar creation/selection/consumption lookup.

Required races: before/after cancellation, admission claim, old worker restart, successor persistence before planning, two preparers, wire entry, acceptance/timeout, changed terms/permission and concurrent ownership.

## Explicit renewal contract
Prove: prepare unused grant; permission/grant expire; no sale; stop and disable; explicit authorized fresh coverage with current evidence/terms/expiry; controlled restart; normal preparation completes one sale. Preserve old authority, all liability and uncertainty. Duplicate renewal command and parallel workers must converge on one successor. Unchanged policy, elapsed time, worker restart, generic stop/start or unrelated config edits are not renewal authority. Accepted/uncertain old sends and ownership still block. No hot renewal or automatic extension. If any behavior beyond this requested controlled unused/proven-never-dispatched scenario needs a business decision, ask before enabling it.

## Coverage boundary
Add only a versioned pilot-product fingerprint in the existing contract. Include product/binding, price/currency, contents/previews/enabled state, meaningful descriptions/aliases and referenced media identity/version/provenance/removal. Preserve independent financial/history/creator-permission checks and scope revisions. General catalogue completeness is unchanged. Old mismatched evidence stays unavailable until explicit refresh. Test unrelated edits, every relevant dependency and sync-only metadata timestamps under the existing evidence contract.

## Acknowledgement and availability
Commit authenticated input and receipt-time stop/human/commercial barriers before success. Use existing projections/ingress/polling; no in-memory enqueue or request background coroutine. Preserve ordinary/digest cross-path conflicting-key checks and stopped intake.

Writer availability must use non-creating/non-consuming eligibility checks without recursive preparation or lock-order inversion. Commercial unavailability must remain accurately represented while ordinary permitted chat can progress. Hiding invalid grants and suppressing held work is insufficient: relevant repaired evidence must wake the same request without another message, including after restart.

## Other limits
Classify supported/known-irrelevant/material/unknown events based on documentation. Resolution requires exact item/account/binding/revision, actor, evidence, idempotency and retained audit; one resolution clears no other hold. Digest-cap repair discards no evidence or conflict detection without an approved retention decision. Model reconciliation reuses the ledger with a verifiable cost-evidence contract; missing cost remains uncertain, never invented zero or released merely on lease expiry. Existing stopped offboarding/configuration procedures need qualification, not hot updates or a new administrator model.

The original complete final-image focused/broad/browser and independent-review matrix remains required. Decisive normal-path tests do not preseed missing grants, history completion, decisions/approvals or purchases. Real model quality/provider behavior/bindings, deployed isolation and restore/rollback stay pre-live gates. No deployment, push, paid/live calls, other-chat tasking or agents.
