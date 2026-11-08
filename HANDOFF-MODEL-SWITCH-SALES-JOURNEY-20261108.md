# Plan C — paused model-switch handoff

**Pause is explicit. Do not resume implementation or tests until Jesse sends a continuation instruction after switching models.** This handoff records unfinished work; it is not a completion or live-readiness claim. Prepared 8 November 2026, Brisbane. The server checkpoint was recorded at `2026-11-08T08:02:52Z` (18:02:52 Brisbane).

## Ownership, scope and restrictions

This chat is the sole implementation owner. “real master” owns planning and scope. Do not message/task other chats or start agents. Keep the selected model/settings unless Jesse changes them.

The authorized block is the complete instruction at:

`C:/Users/conta/Documents/ChatGPT/TWIN CLONE CREATION/output/SLAVE-C-SALES-JOURNEY-COMPLETION-20261108.txt`

Read it before continuing. Also read the backend's `docs/plan-c/CURRENT-PILOT.md` and `docs/plan-c/OFFLINE-COMPLETION-BLOCKERS.md`. Older one-fan, $6/three-media and per-message approval instructions are superseded.

Approved scope remains one creator, exactly two permitted fans, one approved photo at USD 3, autonomous authorization through the existing delivery authority, one DeepSeek model/upstream, fallback disabled. No new products, discounts, campaigns, timer-based reopening, replacement memory/profile system or second sales engine. Routine human approval is not being reintroduced.

Heavy work belongs on Work Server via `ssh work-server`, expected hostname `ubuntu-s-4vcpu-8gb-syd1`, address `134.199.145.233`. No deployment, pushes, production changes, real messages, purchases, live provider/model calls or metered tests. Do not reset/clean/discard work or apply old patches over newer work. The historical-memory block is already complete at the baseline below; preserve it.

## Exact source checkpoint

| Checkout | Path | Branch | HEAD |
|---|---|---|---|
| Backend | `/srv/projects/twin-engine-completion-20261107/codex-plan-c-supervised-20261107` | `codex/plan-c-supervised-20261107` | `cdae299d101a6eb59c4c9221b6db022fa3acd91b` |
| Dashboard | `/srv/projects/twin-engine-completion-20261107/codex-plan-c-dashboard-20261107` | `codex/plan-c-dashboard-20261107` | `3cedc8f5c6d3c9f493d6ef35f4162a5207b5b589` |

The dashboard is clean and unchanged. No sales-journey commit was made. The integrated patch remains uncommitted because acceptance coverage and final combined qualification are unfinished. Do not commit it as a completed block merely because the current focused tests pass.

Backend tracked modifications (paths below are relative to the backend checkout):

- `service/conversation_engine/commerce.py`
- `service/conversation_engine/engine.py`
- `service/conversation_engine/journey.py`
- `service/conversation_engine/memory.py`
- `service/conversation_engine/model_output.py`
- `service/conversation_engine/offers.py`
- `service/conversation_engine/pilot_scope.py`
- `service/conversation_engine/real_worker.py`
- `service/conversation_engine/writer_context.py`

Before adding this handoff, the tracked diff was 398 insertions and 42 deletions across those nine files. The new, untracked regression file is `service/tests/test_plan_c_sales_journey_pg.py`. This handoff is an additional untracked documentation file when saved in the backend root.

All ten runtime/test file hashes were checked against the source hashes recorded for the completed 22-case run. Exact hashes and status are in:

`/srv/projects/twin-engine-completion-20261107/codex-plan-c-qualification-20261107/SALES-JOURNEY-20261108/MODEL-SWITCH-checkpoint.json`

The combined source-hash-map SHA-256 is `bb7bd3a5edc3b8e30e0f32d55d8f2daada842b63664c9009b767c087f977f29e`. The tracked patch SHA-256 is `a7f475a730b3c2a19e0b49910c04fe36a4067f9f51c875e7eef6dd59fc70da10`. The untracked test file SHA-256 is `407390e2c6c2a9d18f79e8f45f8bc537d41e851dd677bdd8b7c477c0c23cad4f`.

Preserved recovery copies, in the same evidence directory:

- `MODEL-SWITCH-working-tree.patch` — tracked diff from HEAD; evidence only, **do not reapply it over the current checkout**.
- `MODEL-SWITCH-new-test.py` — exact current untracked test file.
- `baseline-source/` — source copies before the first implementation edits.
- `edited-source-hashes.json` — source edit history/current hashes.
- `baseline.json` — clean starting checkouts, image, job inventory and original lab settings.

Small local editing mirrors are under `C:/Users/conta/Documents/ChatGPT/TWIN CLONE CREATION/output/plan-c-sales-journey-20261108/edit/`; the test mirror is in its parent directory. The server is canonical. Reverify hashes before using any mirror to avoid overwriting newer legitimate work. All builds/tests ran on the server, not the laptop.

## What changed and why

### Offer preparation — implemented, only partly proven

The tracked caller search found no ordinary application provisioning path for `approve_catalogue_offer`; existing callers were tests/live-proof/simulation code. The baseline could send a card only when the test explicitly seeded a per-fan grant.

`pilot_scope.py` now accepts an **optional** `offer_coverage` member in the existing durable pilot configuration. It serializes the existing `VaultCatalogueCoverage` inputs: `account_id`, `binding_id`, `complete=true`, `evidence_ref`, `valid_until`, `snapshot_hash`. It requires caller-supplied `vault:` evidence, an explicit naive-UTC ISO expiry, and the current catalogue snapshot hash. There is no application-generated reference, default lifetime, silent refresh or new table. The test's 30-minute coverage is a fixture value, not a selected production renewal policy.

Old configurations omit this field and retain their canonical JSON shape. Existing explicitly approved unused grants remain usable subject to existing checks. Automatic preparation without such a grant requires explicit standing coverage; missing/stale/expired coverage holds the same request visibly. The new API/config input and its compatibility still need documentation/review before this block is complete. Actual live vault provenance remains a separate qualification gate.

`commerce.prepare_pilot_offer` is called by the normal `RealConversationWorker.turn` path, including a safe hold-recording path after owner selection is refused. It uses the same request lifecycle, existing commerce approval/verification, history gate, ownership and durable pilot controls. `_approve_catalogue_offer` is shared by explicit setup and automatic preparation. A nested insert handles a unique-slot race by rereading and validating the winning grant. The unique constraint is preserved. Used/expired grants and changed prior terms are not automatically renewed or replaced.

`verified_terms` checks the optional standing coverage again, so generation alone is not send authority. No price, purchase or duplicate-send bypass was added.

### Refusal → renewed interest — implemented, only partly proven

`journey.pilot_purchase_scope` recognizes a bounded set of direct current requests for the one approved photo or its exact label. It deliberately does not interpret quotations, reported old requests, hypotheticals or unresolved pronouns as current buying authority. This is not a claim to recognize every natural-language formulation.

`live_current_request` checks current-binding applied non-import ingress, latest live input, activation time and conflict exclusion. `JourneyService._reopen_current_purchase` uses the existing `reopened_scoped` owner transition **before** model context is constructed. It does not globally open sales or alter contact/takeover state. It checks active policy/model/profile and existing hard barriers. The normal preparation path then uses that same state. Judge and engine backstops reject a model's `reopen` signal without the current explicit scope.

The same-turn path passes with an existing grant and with no grant. Ordinary chat and seven noncurrent/ambiguous cases stay paused. The remaining stop/takeover/policy/profile/media/price race coverage has not been written or run on this patch.

### Existing-card context — implemented, only partly proven

`offers.match(..., include_cards=True)` adds a bounded facts projection while preserving the old reference matcher and its default API. Unclear references still return no matched offer. Frozen terms/grant identity and accepted receipt projection support sent-card facts; unknown/dispatching states remain distinct. Latest scoped access evidence is separate from financial settlement. Coverage/truncation information is retained.

`engine.py` and `memory.py` include this projection in existing reference freshness fingerprints. `memory.current` excludes the current action's own admission from the prior-card projection to avoid invalidating itself; changes to other relevant card/access facts are intended to invalidate stale unsent claims. Those negative freshness cases remain unproven on this patch.

`writer_context.request_context_from` renders card facts within the **existing** request-context budget, including reference/send/access/financial distinctions and omission/incomplete-coverage notices. Memory budgets and the completed historical-memory logic were not changed. The unresolved-action quota is reduced when card facts compete for the same budget; its existing fairness/budget regressions still need rerunning on the final candidate.

## Completed tests and exact evidence

Evidence root:

`/srv/projects/twin-engine-completion-20261107/codex-plan-c-qualification-20261107/SALES-JOURNEY-20261108`

All new probes used the baseline dependency image `twin-planc-offline:bounded-cdae299`, ID `sha256:7c67dc7137219d99f335898fcbbcdc6121f72961e5ec5a7293f457ff561ad259`, with the server checkout mounted read-only over `/app`. External HTTP services were mocked, the containers shared the network-none PostgreSQL lab namespace, and each PostgreSQL case used its own disposable database.

**The green results test dirty source mounted over that image. They do not qualify the committed cdae299 image as containing this patch, and no image has been built from the sales-journey changes.** Exact commands and file hashes are in each command JSON.

| Run | Result | Source boundary | Evidence files under the root above |
|---|---|---|---|
| `red-v1` | **4 failed**, 47.60 s, expected reproductions | Unchanged cdae299 runtime plus new reproduction file | `red-v1-command.json`, `red-v1-test.py`, `red-v1.log`, `red-v1-result.json` |
| `joined-v1` | **4 passed**, 57.69 s | Nine modified runtime files plus the then-current four-case test | `joined-v1-command.json`, `joined-v1-tracked.patch`, `joined-v1-test.py`, `joined-v1.log`, `joined-v1-result.json` |
| `joined-v2` | **22 passed**, 132.96 s; exit 0 at `2026-11-08T07:42:47.039904Z` | Same modified runtime files plus the expanded test whose hash is recorded above | `joined-v2-command.json`, `joined-v2-tracked.patch`, `joined-v2-test.py`, `joined-v2.log`, `joined-v2-result.json` |

The 22 cases prove:

- Incoming signed HTTP purchase request → actual `ModelRuntime`, judge, durable decision, autonomous authorization, dispatcher and one USD 3 single-photo card, with no seeded per-fan grant; duplicate input and restart do not resend. Duplicate identical unlock evidence creates one access record and completes that original request.
- Refusal → ordinary chat stays paused; refusal → explicit photo request completes in the same turn, with and without an existing grant.
- Verified unopened card facts survive outside recent history while the current reference remains unclear, without a replacement grant/card.
- Missing standing coverage/history, removed/wrong media, changed price and changed version produce durable holds without a model/provider call. Disallowed fan is rejected.
- Seven ordinary/ambiguous/hypothetical/quoted/wrong-media utterances do not reopen sales. A malicious model attempt to reopen from a quoted old request is rejected and left as durable human work.
- Two workers plus restart converge on one request/grant/decision/card.
- Crashes after grant persistence and after decision persistence recover the original request without new authority or a second model/provider action.

No current focused failure is known. The red failures are preserved intentional baseline evidence, not remaining failures. No new broad backend suite or authenticated dashboard journey was run for this dirty revision.

Historical baseline qualification remains at:

`/srv/projects/twin-engine-completion-20261107/codex-plan-c-qualification-20261107/BOUNDED-COMPLETION-cdae299/FINAL-REPORT.txt`

It records 432 focused, 605 suite A, 362 suite B passes and the authenticated dashboard journey for committed cdae299/3cedc8f. Counts overlap. Those backend passes **must not be credited to the current patch**. Unchanged dashboard build/type-check/47-test evidence can be reused where valid when final qualification is eventually performed.

## Running jobs and lab settings

At the model-switch checkpoint there were **no owned test jobs or test/browser containers running**. `joined-v2` finished normally. Its detached launcher PID was 784134; it is historical, not a process to resume. The earlier red and joined-v1 launchers also finished. The old interactive SSH session 90567 disconnected; do not rely on that session ID.

Retrieve results by reading the three `*-result.json` and `*.log` files above. `run-repro.py` and saved command JSON remain for reproducibility, but **do not launch them until Jesse resumes the task**, and do not rerun successful checks without a new change or unresolved concern.

The temporary PostgreSQL CPU increase was restored from 1 CPU to the original **0.5 CPU**. Container `codex-planc-pg-20261107` is the same instance:

- ID `0ae9e79fa4ca67b5a2d8ba6daf21a19a35b11c6722ee7be8befb16c4883d9b62`
- Started `2026-11-07T12:49:55.632878952Z`
- Memory 536870912 bytes (512 MiB); network `none`; no restart.

Proof is `MODEL-SWITCH-lab-restored.json`, also embedded in `MODEL-SWITCH-checkpoint.json`. No temporary lab setting from this step remains to restore. Other worktrees and database containers were untouched.

## Master instruction status and remaining work

| Instruction section | Status at pause |
|---|---|
| Baseline/source/job verification | **Complete for this checkpoint.** Reverify before continuation. |
| 1. Offer preparation | **Incomplete — repaired and focused proof exists.** Remaining: invalid/stale binding coverage, expiry/consumed-grant and prior-terms recovery cases, both allowed fans/ownership/unresolved-delivery combinations, provider-acceptance and receipt-processing crash boundaries, final combined qualification. The defensive losing-insert reread branch is not directly forced by the current serialized two-worker case. |
| 2. Refusal → renewed interest | **Incomplete — core connected path and negative interpretation cases pass.** Remaining: explicit existing-card/purchase variants; newer refusal, stop, takeover, policy/profile and media/price changes during generation and after a dispatch grant; stop followed by greeting; durable completion/hold assertions across those cases. No timer-based reopening was added. |
| 3. Existing-card context | **Incomplete — unclear-reference/old unopened-card case passes.** Remaining: wrong-fan/old-binding exclusions, unknown/dispatching and unlocked/revoked states, unlock/reversal/card changes during generation and before send, omission/budget pressure, unrelated same-price transaction remaining unlinked. Do not claim freshness invalidation is proven merely because its code is present. |
| Joined final qualification | **Not started.** No sales-journey commit, exact new image, broad suites, or new dashboard journey yet. |
| Final documentation/report/pause | This handoff completes the requested model-switch checkpoint only. The original sales-journey deliverable remains unfinished. |

There is no currently identified need to change approved business scope. The optional standing-coverage input, narrow input recognizer and fingerprint compatibility need careful review; they are not independently reviewed or broadly qualified. Do not invent a grant renewal/reoffer rule, extend expiry, assume offboarding behavior or change financial evidence semantics to close a test. If the remaining work requires one of those decisions, pause for Jesse/real master.

Existing live gates remain open: authoritative malformed/unknown-event resolution without a blanket clear/retry control; administrator/account offboarding decisions; actual deployed-writer and queued-work isolation; real creator/fan/photo/version bindings; actual model/provider/rates/quality qualification; and fresh restore-tested backup/compatible rollback. The current fingerprint shape changes can invalidate older unsent drafts conservatively; deployment/rollback compatibility has not been qualified.

## Exact next step after Jesse's continuation

1. Read this handoff and the complete master instruction. Verify the two heads, working-tree diff and checkpoint source hashes. Preserve any newer legitimate changes; do not apply the saved patch over current files.
2. Review the existing patch, especially explicit coverage provenance/expiry, scoped reopening guards, and existing-card freshness/self-intent exclusion. The server files are canonical. There is no reason to repeat the already completed memory implementation.
3. Add the remaining acceptance tests first. Prioritize during-generation and post-dispatch-grant changes, existing purchase/unknown-send behavior, and card/access/reversal freshness. Extend the same ordinary application path; mock external services only, and retain the actual judge/authorization/dispatcher controls. Run only newly affected checks initially; do not immediately repeat the successful 22 cases unchanged.
4. Repair only demonstrated defects within scope. Once the combined change is coherent, document API/compatibility implications, commit it, build an image from that exact commit, then run affected focused checks, the broad backend suites and authenticated dashboard journey against that image. Reuse valid unchanged dashboard build evidence. Preserve all old results under their original identities.
5. Record final hashes/commands/exit codes, restore any newly changed lab limits, report remaining live blockers and pause again.

**For now: remain paused. No implementation or test run is authorized until Jesse sends the continuation instruction after the model switch.**
