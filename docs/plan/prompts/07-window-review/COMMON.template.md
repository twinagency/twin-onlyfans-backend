# W<N> rwl — common rules (every W<N> thread reads this first)

Window: base `<BASE_SHA>` (W<N-1> end, #<n>, <date>) .. `origin/<main>` `<HEAD_SHA>` (#<m>, <date>). ~<k> non-rwl PRs (#a–#b).
Already rwl'd, don't re-review: every `rwl/W<N-1>-*` PR — their fixes only; W<N-1> open findings in `.scratch/rwl-W<N-1>/STATE.md` are context, not scope.
Not in scope: open PRs <#…>.

## Setup
- Fresh worktree off `origin/<main>`, branch `rwl/W<N>-<slug>`. <one-time setup cmd, e.g. `slop/run --list`>.
- Scope = union of files changed by your PRs: `for n in <PRs>; do gh pr view $n --json files -q '.files[].path'; done | sort -u`, diffed `git diff <BASE_SHA> origin/<main> -- <files>`. Read callers only to verify behaviour. Ignore `slop/evidence/`, `*.png`, generated maps/fixtures.
- Defect outside your files → "Open findings" note naming the owning W<N> thread, don't fix.

## Loop
- `/rwl` (review-loop skill). Claude structure + correctness reviewers (Opus, high), read-only, parallel; ONE Claude fixer (Opus, high); re-review both until clean. Root `REVIEW.md` in every reviewer/fixer/DA/adviser prompt. Two levels only.
- Codex Sol high = Opus-tier lens: round-1 review, final check, repros (risky backend), stuck-breakers. Always pinned: `nice -n 10 codex exec -m gpt-6-sol -c model_reasoning_effort=high -s read-only ...` (repros: `-s workspace-write` in throwaway worktree). Tag `[codex]`.
- Codex Astra medium = adviser: round-1 devil's advocate (`-m gpt-6-astra -c model_reasoning_effort=medium`, tag `[codex-da]`) + consults per `.claude/orchestration.md` (hard decisions, conflicting evidence, after 2 failed fixes, every 4 rounds): evidence-first brief → continue / redirect / insufficient evidence + main objection + next check; verdict + your response in FINDINGS.md.
- DeepSeek lens (skill step 2 + final, 2 calls): `delegate_task` target `{providerInstanceId:"deepseek", model:"deepseek-flash"}`, role review, read-only, tag `[deepseek]`. Unavailable → say so, carry on, no retry.
- Never omit `-m` (CLI default model unpinned). Codex budget per skill (3 + repros + stuck-breakers) + adviser consults. Codex missing/fails: say so, carry on.
- Commit approval pre-granted. YAGNI: fix what findings say.

## Project invariants (correctness reviewer checks these)
<copy from REVIEW.md "Invariants", one line each; add owner decisions that reviewers must NOT propose changing>

## Proof + ship
- Unit tests green; `slop/run --changed` green via slop-runner agent (quote `slop/evidence/<folder>`, never `latest`); own FAULT pick → `--break --baseline=<green>` CAUGHT; own `volume` pick → `--scale=medium --users=120 --no-ui` alone (120 = owner cap after port exhaustion 2026-11-09; 200 only on demand).
- UI fixes: trigger + settled screenshots in run folder + side-effect check.
- PR body: `found N · fixed N · open N` + "## Open findings" bullets (counted later by `tools/open_findings.py`).
- Rebase on origin/<main> right before merge; merge when green; no deploy (deploy after the window).
- Label claims measured/inferred/guess. Final line: `Ship: PR <url> · merged <sha|no + why> · deployed no (W<N> batch, deploy later)`.
