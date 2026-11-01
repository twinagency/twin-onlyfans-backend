> Current continuation: [QUALIFICATION-CHECKPOINT-20261102.md](QUALIFICATION-CHECKPOINT-20261102.md). PostgreSQL testing and repairs are underway; earlier environment/coverage totals below are historical and superseded. Release qualification remains open.

# Original OnlyFans qualification continuation, 2 November 2026

**Status: NOT release-qualified.** No merge, deployment, production migration, setting change, account activation, customer message or real-model call was performed. The separate Moxy redesign remains excluded.

Code/test-environment candidate: `bf16b6766bc70078e449c74c1fe07dbf34375f25`, tree `ec055cf6d286bcfb755c1cafd6e5a8766e2cc30e`, on `codex/of-original-completion-20261102`. Its parent is the supplied `eadd802ef29bde8015e0a056ce8d20c57fdf3d88`. The subsequent evidence commit changes documentation only. Use the final branch head reported with this package for review; no product runtime file changed during this pass.

## Changes and preserved work

- Recovered `service/tests/helpers/twin_bridge_server.mjs` byte-for-byte from archived local work at `d2b5185014b569f0238a8ca217ee8ce19e7ca84f`. SHA-256: `5de0dcd9630d5a7dc3d92a2da7ca00d0730d324993636f14f61d6e9b0e664ab3`. The preservation manifest independently names the same source and hash. This uses the actual Twin adapter, with only its model factory isolated.
- Supplied the absent `service/run_tests.cmd`, preserving the existing legacy script-runner contract: a zero Python exit and the success marker are both required. This is a new compatible runner implementation, not a claim that the original missing batch file was recovered. Its native Windows acceptance remains unrun.
- Added `.github/workflows/original-release-qualification.yml`: disposable PostgreSQL 16; pinned dependencies; Python 3.12 matching `Dockerfile.onlyfans`; additional Python 3.13 CI coverage; native Windows runner checks. It has read-only repository permission, no production secrets, no deployment step and no customer-sending operation. Existing push triggers were inspected before publishing.
- Added a qualification-only Python socket guard outside the production service import path. It rejects non-loopback connections during the test jobs. Dependency installation is a separate step. It is not a production network control or a substitute for the product's authority checks.
- Preserved all existing runtime files, Claude's repairs, both R5 amendments and existing commercial regressions. No business policy was changed.

## Verification actually completed

The inherited environment first reproduced 137 passing modules, four prerequisite failures and 139 unrun PostgreSQL modules. Its dependency metadata differed from the pins, so it is not used as production dependency evidence.

A fresh isolated environment then installed `requirements-onlyfans-test.txt` and passed `pip check`: Python 3.12.14, pytest 8.3.5, FastAPI 0.136.1, SQLAlchemy 2.0.35, Alembic 1.13.3, asyncpg 0.30.0 and psycopg2-binary 2.9.10. The final local run checked the candidate Git tree before execution. Its local Git metadata still names the reconstructed parent; the indexed candidate tree exactly matches the remote code candidate. The reconstructed checkout lacks 23 unrelated desktop/icon/graph artifacts, so this is not a complete packaging/build qualification. Unit modules used four separate interpreter processes; timing/load modules ran sequentially afterward. All child processes received a restricted environment and the non-loopback socket guard.

| Check | Actual result |
|---|---|
| Final pinned service run | **137 passing modules, 4 failed modules, 139 PostgreSQL modules not run** |
| Commercial-state regression | **43 passed** |
| Full orchestration scenarios | **26 passed**, real orchestration/persistence/authorities, synthetic model and provider HTTP |
| OnlyFans egress / shared-product isolation regressions | Passed; no product runtime file changed |
| Python 3.13 / native Windows hosted qualification | No jobs started; unrun |
| Populated PostgreSQL chain, upgrade/rollback, locks, concurrent workers and receipt recovery | Unrun on PostgreSQL; existing synthetic/SQLite checks are not substituted |
| Real-model quality | Zero model calls and zero independently scored generations |
| 180-second response target at intended production delivery mode | Not qualified |
| Production parity, inventory, staffing and real unlock | Not verified |

Exact executed module commands, exit codes, durations and test-source SHA-256 values are in `evidence/qualification-20261102.json`. The exact session runner is retained in `qualification-20261102-runner.py`; its workspace paths are intentionally the original session paths. The fresh dependency freeze, 26 scenario records and prerequisite logs are adjacent. The hosted equivalent command is:

```bash
python -m pip install -r requirements-onlyfans-test.txt
python -m pip check
python scripts/run-service-tests.py --postgres --module-timeout 600 --report qualification-evidence/service-results.json
```

That last command requires an explicitly owned disposable PostgreSQL server and the release/rollback sibling checkouts configured by the new workflow. It was **not executed successfully** here. The new workflow does not pretend that the missing Twin sibling is present.

### Four failed modules and precise prerequisites

| Module | Observed blocker | Smallest required resource |
|---|---|---|
| `test_ledger_date_filters.py` | Database-dependent cases have no `ONLYFANS_TEST_PG_URL` | Owned disposable PostgreSQL with create/drop-database permission |
| `test_twin_bridge_contract.py` | Real helper now present; matching sibling/tsx absent | Authoritative Twin sibling with `src/channels/{identity,http,store}.ts`, its matching configuration and locked dependencies |
| `test_twin_bridge_roundtrip.py` | Same missing real sibling/toolchain | Same source; set `TWIN_ROOT` to its isolated checkout |
| `test_twin_bridge_runner.py` | Linux cannot execute `cmd`; the new batch runner has not run on Windows | Native Windows execution of the existing probe suite |

Repository search recovered the helper through `LOCAL-PRESERVATION-MANIFEST-20261028.json`. The matching Twin repository remains unavailable through the current GitHub installation; accessible older local Twin copies and supplied older handover ZIPs did not contain the required channel adapter. No fake sibling adapter was substituted.

### GitHub startup failure

Run `41328583562` ended `startup_failure`, path `BuildFailed`, with zero jobs. Its commit's check-run collection was also empty. The fresh test-only workflow at the new candidate reproduced the same condition in run `41332145767`, again with zero jobs. The accessible APIs did not expose a validation/error annotation or the underlying startup reason. Therefore this is diagnosed only to the pre-job startup stage, not to a claimed YAML, billing or runner-policy root cause. An accessible startup diagnostic or a working equivalent isolated runner is still needed.

The managed local executor has no usable non-root UID mapping, Docker or owned PostgreSQL service. Dropping to a non-root UID fails. An online connected Windows machine has PostgreSQL 16.15 binaries and Python 3.12/3.13, but automatic approval review rejected continued transfer of the private candidate there because that destination lacked explicit user authorisation. The exact partial transfer was identified by hash and removed. No test database or candidate service was started on that machine.

## Real-model preparation and remaining quality work

Recovered the original 308-page transcript PDF. Extraction reconciles all **122 chats and 5,127 messages**, including each chat's declared message count. Prepared 200 distinct draft context/turn cases across seven creators and 119 chats, with incident-oriented sampling and nearby ordinary candidates. Known test, suspected test and unverified customer/business cohorts remain separate. Raw customer transcripts, IDs and case contents remain outside this repository.

These are **unreviewed source drafts**, not 200 approved quality cases. Expected outcomes, approved creator facts/catalogue/prices/purchase state, incident traceability and independent rubric review still require reconciliation. The private revised execution plan and original private case file remain unavailable.

Both real prompt routes assembled all 200 draft cases without errors. No configured OpenRouter test credential is available in this executor. No replacement model/provider was selected. Existing canned evaluations are retained as regressions only. Budget estimates use the repository's explicitly unverified price seed, not verified billing prices: three generations on every case on both routes, at 400 completion tokens and a repair on every generation, estimate $2.75 combined. Do not run that plan against a $2 cap. Prepare a route-stratified plan within the existing total cap, verify current model pricing and enforce the shared budget before any approved model calls. Do not reduce case coverage or claim passing scores to fit a budget.

## R0-R9 acceptance disposition

| Stage | Evidence retained/completed | Remaining release gate |
|---|---|---|
| R0 | Remote head verified; parent preserved; candidate tree verified | Current API/worker/supervisor source/image/schema/settings manifest and original private-plan reconciliation |
| R1 | Existing hold/control/ownership regressions pass | PostgreSQL interleavings; staffed owner and acknowledged handover |
| R2 | Complete-answer and shared legacy-route regressions pass | Repeated actual-model completeness/relevance/repetition/over-suppression scoring |
| R3 | Existing facts, attachment, identity and intent regressions pass | Private incidents, approved facts, real attachment and operator evidence |
| R4 | Existing readiness/fairness regressions pass | Real shelf descriptions/versions/previews and provider progress |
| R5 | 43 state tests and 26 full-path scenarios retained and passing | PostgreSQL authority persistence; model judgement; bounded pilot evidence |
| R6 | Existing quote/budget/receipt regressions pass | Populated PostgreSQL upgrade/recovery and approved real purchase/unlock |
| R7 | Existing inbound-intent regressions pass | Message-level original incident and nearby legitimate scoring |
| R8 | Existing synthetic safety, recovery and timing tests pass | Intended delivery-mode configuration, representative PostgreSQL load and >=95% within 180 seconds |
| R9 | Candidate, evidence and approval boundaries supplied | All preceding offline gates, current parity, approved delivery checks and staffed pilot |

The existing 1,000-fan account-gap burst remains a queue-safety test, not evidence of the 180-second target. No supported provider capacity is claimed. Required declared scenarios remain 100 simultaneous fans on one creator, multiple creators and a 50-creator roster, including media, backlogs, restart, 429, authentication outage and competing writers. Current production mode/provider limits must be captured before selecting the representative configuration. The retained 300-second post-401 cooldown is unchanged and outages must be reported separately and in overall user-visible delay.

## Prepared migration, configuration and rollback sequence

**Not approved for execution.** R0 and real PostgreSQL qualification precede these steps. Do not fill unknown image digests, recipient IDs or settings revisions by guessing.

1. Use the existing `deploy/final-plan-20261101/r0_host_snapshot.sh` and `r0_readonly_snapshot.py` through the designated production operator. Capture running and stopped roles, immutable image IDs, source overlays, schema, settings fingerprints, connection/binding revision, holds, leases and unresolved intents/receipts. The general cloud-audit row available at `2026-11-01T11:00:22.812726Z` is not this OnlyFans parity manifest.
2. Reconcile in-flight and unknown delivery; retain all evidence and existing intentional pauses. Record a staffed operator and monitoring owner. No generic queue flush, account activation or silence-based resend.
3. Produce and restore-test the existing encrypted backup using code compatible with the **observed** pre-upgrade schema. The candidate backup CLI accepts 0108 and three explicitly listed historic revisions, not arbitrary 0103-0107 sources. Use the verified matching pre-upgrade image/tool where required; do not mislabel a database as a supported revision. Exact backup/image selection is blocked on R0 and PostgreSQL rehearsal.
4. On a populated disposable copy, run the actual chain through `0108_commercial_progress`, repeat upgrade, compare retained history/purchases/holds/intents/receipts and rehearse recovery under workers. Prove compatibility with the intended retained rollback image before scheduling production.
5. Only after separate migration approval, supply the explicit PostgreSQL URL through `ONLYFANS_MIGRATION_URL` and use the candidate's existing CLI:

```bash
PYTHONPATH=service:. python -m db.schema upgrade --url-env ONLYFANS_MIGRATION_URL
PYTHONPATH=service:. python -m db.schema check --url-env ONLYFANS_MIGRATION_URL
```

The CLI uses one explicit target and UTC, a 5-second lock timeout and a 30-second statement timeout. Never use `bootstrap` on populated storage. PostgreSQL lock duration/chain behaviour remain unmeasured here.

6. 0108 adds nullable commercial-state, invitation association and quote/lifecycle fields. Its downgrade intentionally retains evidence. A revision-label downgrade is not proof that an older image safely understands current obligations. Prefer forward recovery; restore only to a separate empty target, which the existing recovery tool quarantines. Do not overwrite live storage or clear a restore fence.
7. Prepare configuration through the existing per-creator audited API: contextual off/shadow/live; `fasttCommercialInitiative=active`, `fasttFirstOfferWindow=[2,6]`, `fasttRepeatOfferWindow=[3,6]` only for a separately approved pilot. Preserve every intentional account/fan Off setting, new-fan exclusion, spend exclusion, quote, currency, provider quota and independent on-request permission. No configuration was saved during this pass.
8. Roll back execution only to an exact image proven compatible with the observed advanced schema and obligations. Preserve hard stops, takeover, purchases, refusals and unknown receipts. Merely turning contextual mode off does not disable separately enabled request sales or campaigns. Use the existing approved runtime/conversation stop controls for a separately authorised operational pause.

## R9 delivery and pilot approvals still required

No live test is requested or authorised by this report. Before executing the original two checks, the approval packet must identify both account/fan pairs, exact approved assets/previews, integer amount/currency, current configuration/ownership revisions, operator and authorised test purchase. Those original approved tuples are not present in the accessible handoff; recover them from the private plan or designated operator rather than selecting replacements.

For each approved tuple: record one intent; verify provider acceptance; reconcile local delivery projection; inspect visible card/assets/count/type/price; complete only the approved test unlock; confirm purchase history and durable ownership; replay reconciliation to prove no duplicate send/payment. A planned invitation or card is not delivery evidence. Verify an actual handover is acknowledged by its staffed owner.

After offline qualification and those delivery checks, obtain separate single-creator pilot approval. Preserve pauses while deploying an approved exact image; observe separately approved shadow decisions first; run a bounded staffed pilot only after inventory and ledger readiness. Record baseline/cohort/window, collected revenue per eligible fan, refunds, orders, repeat purchases, complaints, handovers, cost and substantive latency. Expand only creator by creator after the corresponding approval/evidence.

Abort on wrong recipient, unauthorised action, duplicate send/charge, inconsistent asset/price, broken stop/takeover, serious identity/consent failure, lost ownership/receipt evidence or unresolved delivery. Preserve and reconcile obligations; do not replay an uncertain POST based on silence.

## Exact resources still needed

1. A working isolated PostgreSQL/Linux/Windows qualification environment. Existing hosted runs start no jobs; using the connected `LAPTOP-PKFJ7M3I` for this private candidate requires explicit destination approval. Some image/platform tests additionally need their appropriate Docker/Linux environment.
2. The authoritative Twin channel-adapter sibling and matching locked dependencies, or GitHub installation access to that repository.
3. A scoped configured-model credential and permitted transport in the isolated evaluator; approved world-state/private incident reconciliation and current pricing within the existing budget. No secret should be pasted into the report or repository.
4. Read-only production operator access/exports for role images/schema/settings/inventory/obligations/monitoring, the staffed case owner and the original R9 test tuples.

Until these are supplied and the affected complete suites pass, this branch is a preserved implementation candidate with explicit blocked acceptance gates, not a finished release.
