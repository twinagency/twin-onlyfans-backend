# Continued OnlyFans release: operator runbook

Prepared on 2 November 2026 (Australia/Brisbane). Implementation source: `5760c3cfedb9893e06862903bd50d99bc0b58cd5`, branch `codex/of-original-completion-20261102`. This is a preparation document, not release permission. No production action was performed by this continuation.

## Blocking compatibility facts

The repaired original-release source has schema HEAD `0108_commercial_progress`. The separate onboarding work records `0110_recent_history`. Its graph joins both real branches, rather than renaming either:

* `0104_contextual_item_scope` -> `0105_operator_cases` -> `0106_read_lane_fairness` -> `0107_active_selling_quote` -> `0108_commercial_progress`.
* `0104_contextual_item_scope` -> `0105_earnings_totals` -> `0106_parallel_history_reads`.
* Both tips -> `0109_history_release_merge` -> `0110_recent_history`.

The original-release candidate is therefore **not an installable replacement for an 0110 deployment**. Do not downgrade or stamp production to 0108 to make it start. A reviewed integrated source must preserve the onboarding graph, newer worker behavior, metadata ancestry and existing production overlays. Keep the separate Moxy redesign out of that integration.

The isolated compatibility checkout successfully upgraded populated 0108 evidence to 0110 twice. It preserved 155 original tables, stopped runtimes, accepted and unknown intents, and proxy evidence. Its read-only recovery API retained account isolation and refused command access. The latest retained onboarding/scheduler run passed 26 tests. These are migration/source compatibility checks, not exact deployed-source or immutable-image parity.

The prior `fceb624b814435f36be33d0938c3a5b46494a8e5` recovery source works in read-only API mode on 0108 and **fails** the same recovery test on 0110. The reconciled 0110 source passes read-only API recovery, but no corresponding immutable recovery image has been qualified. The still older designated `d6811b1` source already refuses 0108. None is an approved live rollback image for the current system.

## Required read-only preparation

1. Use the existing `deploy/final-plan-20261101/r0_host_snapshot.sh` and `r0_readonly_snapshot.py` with the designated production operator. Capture current role names, running/stopped status, source and immutable images, schema, configuration revisions/fingerprints, binding/connection revisions, ownership, holds, ledger coverage, inventory versions/previews and unresolved dispatch/receipt state. Include the deliberately stopped supervisor. Read settings by allowlist; do not dump secrets.
2. Recover the five source overlays identified by the existing source-parity report through the authorised repository. Their owner committing the exact files is the smallest alternative to the previously rejected production-source transfer. Do not repeat that transfer, overwrite unpublished files or assume today's worker matches yesterday's image.
3. Build exact API/worker/importer/learner/supervisor images from the reconciled source without installing them. Record immutable digests and dependency/OS evidence. Preserve the separate 30-day onboarding work and intentional pauses.
4. Qualify the matching encrypted backup/restore tools for the observed schema on a separate disposable target. A restored target remains quarantined until positive reconciliation. Measure migration lock duration on representative populated data; a small synthetic rehearsal is not a production lock-time bound.
5. Re-run the affected integration checks and final applicable acceptance on that integrated source. Model quality, native provider allowance, the failed heavy pacing workload and staffed handover remain separate gates.

The resumed laptop's default AWS identity check returned `NoCredentials` (exit 253). The configured `jesse-gpt` profile was then verified as account `323149985112`. Initial transport timeouts were resolved. The bounded read-only snapshot completed at `2026-11-02T13:31:40Z` (23:31 Brisbane), SSM command `b8ad119e-1948-4d27-ac5c-3d3b1d9d293a`, response 0. It confirmed schema `0110_recent_history`, API image `sha256:87bc296eb275e2c220c3daa2726d34c5e9d8c4faa77455b5bf7fd77e8c00f439`, and worker/importer/learner image `sha256:6afd1ad6bfc1030cbb0c7cd72d675753cde487b9de64bf0e333b02d89b4831f2`, labelled `9d6756b20063dc7d481f59acfb1f26c65336c357`. Other work advanced those roles from the earlier observed `91c260b` during qualification; this continuation did not deploy either version. The supervisor remained created and never started. All eight saved policies were gap/20 seconds, and all eight new-conversation automation settings were false. Five current worker files still have no matching blob in the authorised repository, including the newer profile worker blob `a0ef73b23cb6107c9fc453d36ff1f79c5db3612b`. Image labels therefore do not establish source parity. Preserve the new workers and reconcile the recorded hashes; do not install the original 0108 candidate over them.

## Migration commands, only for a separately approved exact target

Supply the approved disposable target securely as `ONLYFANS_MIGRATION_URL`. Verify its exact starting revision before running anything. From the appropriate reconciled source:

```bash
PYTHONPATH=service:. python -m db.schema upgrade --url-env ONLYFANS_MIGRATION_URL
PYTHONPATH=service:. python -m db.schema check --url-env ONLYFANS_MIGRATION_URL
```

For the original 0108 candidate these commands only qualify its supported original graph. For an 0110 target, use the reviewed source retaining the onboarding merge and `HEAD=0110_recent_history`; the 0108 candidate must refuse that target. Do not use `bootstrap` on populated storage, fabricate a revision, or replace the graph with a numeric-prefix comparison. The CLI retains its 5-second lock timeout and 30-second statement timeout.

The native pacing table has its own existing upgrade owner. Its repair widens only the recognised 25–90 check to 25–180, preserves every stored schedule, rejects unknown constraint definitions, and is idempotent. Run the populated pacing-upgrade test before any separately approved runtime change. Do not reduce the constraint back to 90 while 180-second schedules remain.

## Prepared configuration diff

No settings below were saved to production. Apply any future change through the existing revision-checked, scoped configuration API and retain the before/after audit.

| Setting | Existing/default | Prepared change or retained limit |
|---|---|---|
| Ordinary pacing | Existing enable switch; maximum defaults to 90 seconds | `nativeReplyDelayMaxSeconds=180`, stored as `native_reply_delay_max_seconds`, only when separately approved; 90 remains supported |
| Delivery mode | Retain each saved mode | Concurrent for the expressly approved pilot only |
| Preparation/send bounds | Existing bounded machinery | Proposed pilot preparation 8, sending 4; no extra sender fleet |
| Provider budget | Source defaults, not a provider guarantee | Retain 1 request/second, burst 5, max concurrent 4 until native allowance is verified |
| Contextual mode | Existing off/shadow/live | Shadow before separately approved live evaluation |
| Commercial controls | Standard; existing per-creator values retained | Prepared active profile: first window [2,6] raw bubbles, repeat [3,6] grouped replies, subject to review |
| Customer/account permissions | Existing audited controls | Preserve Off states, new-fan exclusion, spend exclusions, holds, takeover and separate on-request permission |
| Auto-resolve uncertain absence | Existing safety setting | Keep `FASTT_AUTO_RESOLVE_ABSENT_AFTER_S=0`; silence cannot authorise replay |

Changing the pacing maximum invalidates waiting schedules through the existing settings authority and uses the original input timestamp. Tests require overdue work not to receive another artificial wait. The 25–180 implementation is configurable; it does not make the overall latency gate pass automatically. No 10 requests/second production permission is implied by the faster synthetic diagnostic.

The legacy dashboard keeps its strict response shape. Commercial configuration fields require `commercial=1`, the pacing maximum requires `replyTiming=1`, vault continuation fields require `progress=1`, and folder covers require `covers=1`. These are wire-contract negotiations, not permission changes. The paired dashboard test-only connection-warning correction is retained in `paired-dashboard-qualification.patch`; it is not an update to the currently deployed onboarding dashboard.

## Recovery sequence awaiting release review

Do not start a deployment until an exact schema-compatible recovery image and staffing are recorded. If a later authorised pilot hits an abort condition:

1. Use the already-approved runtime/conversation stop authority for the affected pilot. Preserve ownership, active holds and intentional account pauses. A contextual-mode toggle alone does not revoke independent send permissions.
2. Stop admitting new pilot work through the existing controls. Preserve in-flight intents, attempts, receipts, purchases and financial records. Do not clear unknown delivery or resend because a response was lost.
3. Reconcile positive provider receipts and local projection for the exact obligation. Keep ambiguous work held and owned by its staffed operator.
4. Use only the reviewed immutable image proved compatible with the actual advanced schema. If none is recorded, retain the pause and recover forward; no older-image fallback is authorised by this document.
5. Restore backups only into a separate empty quarantined target. Compare all retained evidence and obtain the explicit reconciliation/release decision before restoring any send permission.

Abort criteria remain wrong recipient, unauthorised action, duplicate send/charge request, inconsistent price/assets, broken stop/takeover, serious identity/consent failure, lost receipt/ownership/fulfilment evidence or unresolved delivery.

## Exactly two R9 delivery/unlock checks

The current revision-2 execution brief requires **two named approved test accounts**, but supplies no account/fan/media/amount tuples. The separately named private `OF-R0-R9-EXECUTION-PLAN-2026-11-01.txt` remains unavailable. Do not substitute known test fans or infer permission from historical receipts.

| Required tuple | Account/fan | Assets and previews | Integer amount/currency | Authorisation |
|---|---|---|---|---|
| Original R9 check 1 | Not recovered | Not recovered | Not recovered | Separate explicit delivery and applicable unlock approval required |
| Original R9 check 2 | Not recovered | Not recovered | Not recovered | Separate explicit delivery and applicable unlock approval required |

For **each of those exact approved tuples**, record configuration/ownership revisions and one dispatch intent; confirm provider acceptance and local projection; visually verify recipient, visible card, asset IDs/count/type, previews and canonical price/currency; perform only the separately approved unlock/purchase; confirm purchase history and durable ownership; then replay reconciliation and prove it creates no second send or payment request. An invitation, queued card or model statement is not completion. Verify a real handover reaches its named owner and is acknowledged.

## Bounded pilot proposal, not activation

After the preceding gates and the two checks pass, request approval for one creator (Maile only if current inventory, purchase coverage and staffing qualify), one named operator, an explicit fan allowlist, a fixed start/end window, a maximum message/spend budget and captured initial configuration revisions. A proposed initial engineering observation is no more than ten approved fans for at most 60 minutes, followed by automatic end-of-window operator review; no automatic account expansion. The exact scope still requires a matching isolated latency check and explicit approval. No new timer or automation is created by this proposal.

First obtain separate approval for any required deployment/shadow observation. Review decisions without customer sends; live customer messaging, test purchases and the subsequent pilot each retain their explicit approval boundary. Preserve every other creator's saved state. Reconcile any in-flight obligations at the end rather than replaying them.

For commercial proof, record a comparable human baseline, eligible cohort, catalogue/prices and observation window before activation. Report net collected revenue per eligible fan, refunds, request-to-offer completion, purchases, order value, repeats, product fit, delivery, complaints, handover, model cost and end-to-end substantive latency. Keep test, suspected-test and promotional traffic separate. A short engineering pilot or extra invitations cannot establish chatter replacement or agency readiness.
