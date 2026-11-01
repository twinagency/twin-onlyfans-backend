# Continuation status

The current review package is [README.md](README.md), with [verification](VERIFICATION.md) and [R9 gates](R9-REVIEW-AND-RELEASE-GATES.md). The verified integration parent is `a6b93bd227fec82589f955593811543889194613`; the older start point below is historical. No deployment or activation occurred in this continuation.

---

# OnlyFans final repair: progress record and status (1 Nov 2026)

Integration branch: `claude/of-final-integration-20261101` (local only; nothing pushed, deployed, migrated or activated).
Start point: `6807b6e` (R0 snapshot = 2f1a397 + R0 read-only tools). PRs #75 and #76 merged first.

## Status table (honest values)

| stage | implemented | tested (targeted) | full suite | reviewed (GPT/Jesse) | merged to release | built | installed | enabled | provider accepted | purchased/unlocked | commercially proven |
|---|---|---|---|---|---|---|---|---|---|---|---|
| R0 baseline | branch yes; live snapshot NOT run (needs operator, D1) | n/a | n/a | no | no | no | unknown | no | no | no | no |
| R1 hold policy, detectors, cases | yes | yes | pending | no | no | no | no | no | no | no | no |
| R2 complete answers, validator | yes | yes | pending | no | no | no | no | no | no | no | no |
| R3 facts, memory, attachments | yes | yes | pending | no | no | no | no | no | no | no | no |
| R4 read fairness, product readiness | yes | yes | pending | no | no | no | no | no | no | no | no |
| R5 active selling | in progress (separate branch) | in progress | pending | no | no | no | no | no | no | no | no |
| R6 canonical quote, lifecycle | in progress (separate branch) | in progress | pending | no | no | no | no | no | no | no | no |
| R7 promo intent | yes (with R3) | yes | pending | no | no | no | no | no | no | no | no |
| R8 timing, capacity | yes | yes (synthetic only) | pending | no | no | no | no | no | no | no | no |
| Eval harness | yes (branch claude/of-final-eval-20261101) | 20 tests pass; synthetic run 430 cases | n/a | no | no | n/a | n/a | n/a | n/a | n/a | n/a |
| R9 release package | in progress (this folder) | n/a | n/a | no | no | no | no | no | no | no | no |

## Integration fixes made while merging (all local)

- Schema chain: `0106_read_lane_fairness` re-chained after `0105_operator_cases`; 0106 column adds made idempotent (create_all databases already have them).
- `pack_claim._DELIVERY_PROMISE`: subjectless "sending you a photo" now held. Exposed when R2 removed the forced "?" from EXPLORE bridges.
- R1 `safety_detect`: single-quote stripping no longer eats contractions ("i've hurt myself and i'm scared" was invisible); added overdose / pills / "no point in living" / "don't want to be here anymore" / consent-with-adverb phrases. Found by the eval harness (EV-0350, EV-0352..0357). Regression + nearby-legitimate tests added.
- `text_delivery`: auth-failure check reads `.reason` safely; R1's dispatch refusal (store.Refused, no `.reason`) crashed the campaign-controls path.
- Test fixtures updated for deliberate contract changes, no assertion removed:
  - R4 product readiness needs a version (`content_hash` or `updated_at_of`) and a meaningful description: fixtures gained `updated_at_of`; requested-caption cases with undescribed items now expect refusal / exclusion.
  - R4 shelf check reads the offered set before the resolver (2 awaits, not 1).
  - R4 read-budget starvation fields on budget fixtures.
  - Schema-head asserts moved to 0106.
  - R2 validator holds tag-shaped text on every native send path: runner escaping test uses non-tag text.

## Known pre-existing failures (also fail on the pre-merge tip 97bac0f)

- test_native_pilot_context, test_native_pilot_expiry, test_native_pilot_storage_pg, test_native_pilot_load_pg (plan section 4 triage still owed).

## Open items / decisions

- D1 R0 live snapshot (operator run). Also verify live vault rows carry `updated_at_of` or `content_hash`: R4 readiness refuses items without a version, which would empty the shelf.
- D4 model spend for the real eval run (estimate $1.22 for 3 generations, $2.03 for 5).
- R8: 401 cooldown (300 s) is not released by a reconnect; left unchanged (auth behaviour).
- Eval findings: promo cases need cross-fan evidence; rubric vs R1 policy on distress (reply vs hold) needs a decision.
