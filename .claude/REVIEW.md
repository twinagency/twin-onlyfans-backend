# REVIEW.md — review rules (root wins over .claude/REVIEW.md)

Reviewers read this first. Findings need `file:line`. Verdict line last: `APPROVE` / `REQUEST CHANGES`.

## Invariants (block on violation)
1. No prod Supabase refs (`aouaenqayfltoqrmpitg`, `cnnsllgzskqkvdovyiiv`) anywhere but a deny-list.
2. Dashboard: a setting that saves must be read by the runtime — walk `migration → mapClone → both repos' types → actions.ts OPTIONAL_COLUMNS → useCloneConfig → control → bot/relay reader`. Half-wired toggle = REQUEST CHANGES.
3. `/ai` assistant: reads via allowlist only, writes via existing validated server actions only; never raw SQL, never money values.
4. Backend relay: no OF credentials or session cookies in logs, errors, or test fixtures. `curl_cffi` impersonation stays; no plain `requests` to onlyfans.com.
5. A clone is a DB row. No per-clone code paths.
6. Secrets: nothing key/token shaped in diffs, screenshots, or evidence folders.
7. Engine (see `handover/TEAM-BRIEFING.md` §5–7): no send outside the existing sender path; stop/takeover/no-contact/unknown-delivery/budget holds win; ledger `unknown` never retried; new inbound supersedes unsent work; tenant isolation (same fan/media id in two accounts shares nothing); migrations additive with evidence-preserving downgrade and consistent head pins; manual prices only; no invented discounts; tips acknowledged never requested. A diff that weakens any of these = REQUEST CHANGES, no exceptions for green tests.
8. Changed or deleted assertion without a `TEST-CHANGE:` line in commit + PR = REQUEST CHANGES.

## Structure (block unless justified in PR)
- File pushed over 1000 lines. Duplicate helper where a canonical one exists. Logic in the wrong layer (feature logic in shared path).
- New flag/mode/conditional bolted onto an existing flow instead of a reframing that deletes branches.
- `any`, casts, silent fallbacks hiding an invariant, needless optionality.
- UI: no em dashes in copy; explanatory text in ⓘ hints; secret fields blurred; use `components/clone/fields.tsx` primitives.

## Proof required per change type
| change touches | must show |
|---|---|
| any endpoint / DB path | `slop/run --changed` green, folder named (never `latest`) |
| UI | screenshot trigger + settled state in run folder + side-effect check (DB row / API response) |
| migration | up applied on local Supabase, `actions.ts` retry path still strips the column pre-migration |
| perf markers (section 02) | p95 table before/after |
| scheduled check (section 03) | one real run log + one deliberately red run |
| deploy scripts | dry-run output only; never executed in review |

## Known failure modes to hunt
- "configured but inert" (10 confirmed cases historically). Dead switch left in UI after removal. Deleted brake making the bot pushier.
- OF session expiry handled by retry storm instead of re-auth signal.
- Tests that pass because they fake the service instead of the wire.
- Claims in docs/report not backed by a run folder.

## Process
- Two read-only lenses (structure, correctness) + devil's advocate in parallel; one writer; re-review until clean; max 3 rounds then record open findings.
- Codex second opinion on risky diffs: `codex exec -s read-only` with this file inlined.
- Slop is not universal: skip with a written reason when the diff cannot touch volume/concurrency.
