# 01 · Review groups (lenses) + `REVIEW.md`

Goal: each repo gets a project-specific root `REVIEW.md` (mirrored to `.claude/REVIEW.md`, kept identical) and the `/rwl` loop works end-to-end with every lens.

Lenses (measured working in infloww W28, 2026-11-10):
| lens | model | role | calls/loop | tag |
|---|---|---|---|---|
| structure + correctness | Claude Opus, effort high, read-only, parallel | the two main reviews | every round | `[structure]` `[correctness]` |
| fixer | ONE Claude Opus high | the only writer | per round | — |
| Codex Sol | `nice -n 10 codex exec -m gpt-6-sol -c model_reasoning_effort=high -s read-only` | round-1 review, final check, repros (risky backend, `-s workspace-write` in throwaway worktree), stuck-breakers | 3 + repros | `[codex]` |
| Codex Astra | `-m gpt-6-astra -c model_reasoning_effort=medium` | devil's advocate + adviser consults (hard decisions, conflicting evidence, after 2 failed fixes, every 4 rounds) | DA 1 + consults | `[codex-da]` |
| DeepSeek Flash | T3 `delegate_task` `{providerInstanceId:"deepseek", model:"deepseek-flash"}`, role review, read-only | cheap second opinion hunting correctness blockers; round 1 + final | 2 | `[deepseek]` |
Rules: never omit `-m` (CLI default unpinned). Any lens unavailable → say so, carry on, never retry, never block. Root `REVIEW.md` text goes into every lens prompt. Two levels only. Findings merged into one file, stably numbered, severity-ranked, each `file:line` + why + what fixed looks like; re-review marks each closed/open with proof.

Steps (top-level Opus; implementer writes; reviewers read-only):
1. Read kit `REVIEW.md`, `CLAUDE.md`, `docs/TAKEOVER-*.md`, the team's `reports/*/AGENT-RULES.md` and `tools/adversary.sh` (twin-tgaiclone), README, ADRs. Rewrite `REVIEW.md` so every rule names THIS repo's invariants and failure modes with file paths; delete rules that cannot apply. Point to the team's existing proof rules instead of restating them.
2. Add `scratchpad/CORRECTNESS-REVIEW.md`: the claims this code makes (what must stay true after any change).
3. Dry run on branch `kit/review-smoke`: 3-line harmless change, `/rwl`; confirm all five lenses ran or were reported unavailable, one writer, APPROVE. Revert the change.
4. `docs/learn/01-review.md` (≤10 lines): `/rwl`, `/rwl-codexedits`, `council`, `/th`, when each; how to read a findings file and a verdict.

Report: REVIEW.md diff summary, smoke-run findings file path, lens availability table. `Ship:` line (PR for REVIEW.md; human approves rules).

## Also do (from GAPS.md)
- Row 8: REVIEW.md rule — migrations expand→backfill→contract over ≥2 deploys; `CREATE INDEX CONCURRENTLY`; no DROP/RENAME in the same PR as code.
- Row 9: REVIEW.md rule — every migration PR runs splinter lints locally; `0013`/`0008`/`0024` must be 0.
- Row 10: REVIEW.md invariant — every outbound send path checks `OUTBOUND_PAUSED` (global kill switch); prove wiring, not config.
- Row 14: DeepSeek lens gets the scrubbed code-only diff (`scripts/review/scrub-diff.sh`, section 11); gate fails → lens reported unavailable.
- Row 24: `docs/review-ledger.csv` — every finding: id, lens, severity, verdict (accepted/FP/dup/OOS), fix_sha, minutes. Monthly precision per lens; a lens under 40 % gets re-prompted or retired.
- Row 25: `review/canaries/*.patch` — 10 planted bugs (data loss, false success, retry storm); quarterly run through `/rwl`; catch-rate per lens into the ledger.
- Row 26: reviewer prompts carry no author summary or verdict; ≤400 LOC per lens call (split by file group); keep ≥2 independent lenses.
- Row 35: `docs/TESTING.md` — unit (state machines: session expiry, backoff, queue order) · integration · slop; flaky quarantine 3 strikes; monthly mutation run on hot modules.
