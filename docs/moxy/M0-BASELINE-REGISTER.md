# M0 baseline register (Moxy two-creator build)

Written 2 Nov 2026. Local only. Nothing pushed, merged, deployed or activated.
Worktree: `~/twin/twin-onlyfans-backend-wt-moxy2`, branch `claude/moxy-two-creator-20261102`.
Base: `d3a7022` (tip of `origin/codex/of-original-completion-20261102`).

## Baseline choice

The plan names `eadd802` as the repair continuation. That branch has since moved 12 commits to `d3a7022`.
The newer commits are repairs and qualification records. They include frozen manual quotes, native self-holds,
a PostgreSQL settlement deadlock fix, held-fan starvation, and spend reserved before requests.
The new layer therefore starts from `d3a7022`, not `eadd802`.

## Commits seen

| Item | Ref | Seen as |
| --- | --- | --- |
| Repair continuation (base of this work) | `d3a7022` | tip of `origin/codex/of-original-completion-20261102` |
| Repair continuation named by plan | `eadd802` | ancestor of `d3a7022` |
| History/memory/billing (PR #77) | `e3f033c` | tip of `origin/codex/parallel-history-capacity-20261101`; plan named `488036c`, 2 commits behind |
| Moxy prototype | `7d72dc1` | `origin/claude/moxy-workflow-engine-20261102`; 207 tests; shadow-only |
| Local prior Moxy config | `~/twin/moxy-workflows/engine-config/` | amelia.json, cristina.json, moxy_playbook.json from the prototype era; reference only |

Prototype note: `7d72dc1` is a separate line. It differs from the repair base by 95 files, mostly deletions,
because it branched earlier. Do not merge it. Reuse its types and tests selectively after review.

## Migration graph (blocker confirmed from the trees)

Common ancestor: `0104_contextual_item_scope`.

| Repair line (this worktree) | History line (`e3f033c`) |
| --- | --- |
| `0105_operator_cases` | `0105_earnings_totals` |
| `0106_read_lane_fairness` | `0106_parallel_history_reads` |
| `0107_active_selling_quote` | |
| `0108_commercial_progress` | |

Both lines use revision numbers 0105 and 0106 with different IDs and contents.
Each source tree enforces a single exact expected schema head.

Rules for the reconcile (M0 step, not done yet):
- Never rename an applied revision. Never stamp away a difference.
- Add a merge migration whose `down_revision` is the tuple of both heads, or an equivalent linear order that is
  proven safe on PostgreSQL from each supported starting state.
- Update the exact-schema check to accept the reconciled head.
- Test upgrades from three starts: `0104`, repair `0108`, history `0106`.
- The plan's release note says PR #77 code was deployed live at 02:27 Brisbane on 2 Nov. Which head production
  actually holds is NOT verified here (see Not verified).

## Dependency register

| Component | Where | Decision | Evidence still needed |
| --- | --- | --- | --- |
| Provider connection, inbox intake, stored messages | `service/native_pilot/intake.py`, `connection.py` | retain | tests pass on PostgreSQL |
| Accepted-send receipts, recovery | `native_pilot/ledger.py`, `recovery.py`, `autoreconcile.py` | retain | unknown-outcome tests |
| Admission, overlap, one conversation owner, holds | `native_pilot/admission.py`, `overlap.py`, `control.py` | retain | all internal send routes |
| Quotes and delivery | `automations/_quotes.py`, migration `0107` | retain, inject Moxy terms | existing 15%/20% discount logic is not Moxy policy; exclude |
| Pacing and offer windows | `native_pilot/pacing.py`, `pacing_schema.py`, `automations/pacing.py` | exclude from Moxy commercial behaviour | plan section 5 rejects R5 windows/extensions as Moxy rules |
| Commercial progress | migration `0108` | review, adapt | check it adds no cadence defaults |
| Prompt/model route | `native_pilot/model.py`, `contextual.py` | adapt: one writer | confirm no second caption rewrite runs after the writer |
| Media claims, vision | `native_pilot/media_claims.py`, `photo_vision.py` | retain | claim-check tests |
| Operator cases | migration `0105_operator_cases` | retain | owner/escalation fields |
| `automations/ai_chatter.py` legacy persona/sales ladders | | do not run alongside the Moxy writer | route a creator to exactly one engine |
| Prototype `service/moxy_workflow/*` | on `7d72dc1` only | reference; no behaviour adopted unreviewed | small-talk templates and numeric cooldowns rejected by plan |
| Dashboard `main` (86691f8) and PRs #338/#339/#340 | `twin-dashboard` | reuse later (M5) | not touched here |

## Not verified in M0 (no live reads were made)

- Live image digest, compose file, and which schema head production holds.
- The two accounts' saved settings (enablement, pause state).
- Whether the history line is already running in production.
- Unpushed work on other worktrees: not swept in detail. Sibling `twin-onlyfans-backend-wt-*` worktrees exist and
  `wt-moxy` holds only the prototype; no uncommitted Moxy work was found there (`?? venv` only).

Live reads go through SSM. They need Ronan's go. They are listed here so nothing is assumed.

## M0 completion status

- Candidate with the technical fixes in one tree: NOT done. Base is `d3a7022` only. History line not merged.
- Migration reconcile: NOT done. Designed above; no code written.
- Dependency register: written (this file).
