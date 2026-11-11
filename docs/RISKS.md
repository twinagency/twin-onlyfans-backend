# RISKS — twin-onlyfans-backend (reviewed weekly, CHECKLIST §D)

L/I = likelihood/impact (H/M/L). Owner "?" = OWNER TO FILL. Seeded 2026-11-11 from docs/TAKEOVER-20261111.md §6 + handover open items.

| id | risk | L/I | owner | mitigation | trigger | review |
|---|---|---|---|---|---|---|
| R1 | Local relay boot goes live: `service/sessions/` files present, automations send to real fans unless `CHATTERLY_TEST_MODE` | M/H | Jaka | always set `CHATTERLY_TEST_MODE`; never boot `service.server` with sessions present | relay started locally | 2026-11-18 |
| R2 | 12 tests in `service/tests/conversation_engine/` never run by `run-service-tests.py` | H/M | team | add recursive glob or document separate run | any engine change | 2026-11-18 |
| R3 | Host ≠ git: guard early stop 400 on host vs 300 in git; pilot pin script host-only | H/H | Ronan | owner reconciles host → git | any trial deploy | 2026-11-18 |
| R4 | Migration head/rank pins fragile across trial (≤0129b) and commercial (0133→0136) lines | M/H | team | features.json R06/R07 tests; pins listed per commit | new migration | 2026-11-18 |
| R5 | Drop keeps moving (step2-transition, commercial-integration branches) → windows stale | H/M | Jaka | pin windows by SHA; check refs before each window | new origin push | 2026-11-18 |
| R6 | Plaintext key list `~/twin/API-KEYS-2026-11-06.txt` on owner laptop; keys maybe pasted in chats | M/H | Ronan | move to Vaultwarden, rotate if shared | — | 2026-11-18 |
| R7 | Third-party billing owners unrecorded (FansAPI, OpenRouter, AWS, DO, GitHub, Cloudflare, ...) | M/M | ? | owner records billing email per account | renewal / card failure | 2026-11-18 |
| R8 | FansAPI trial pricing ends ~2026-11-13 | H/M | Ronan | confirm plan + cost before then | 2026-11-13 | 2026-11-13 |
| R9 | Live trial: unconfirmed delivery (handover bug #2) + 429-stranded requests until R0 deployed | H/M | Ronan | reconcile before more sends; owner yes for R0 deploy | trial sends | 2026-11-18 |
| R10 | No snapshot on Work Server; restart owner unknown for non-pilot services | M/H | ? | owner configures snapshots, names restart owner | host loss | 2026-11-18 |
| R11 | `ops/plan-c-supervision` README/code carry live account/binding ids + tunnel URL | M/M | team | move ids to host config | repo made more public | 2026-11-18 |
| R12 | PRs fire ~8 old-pinned workflows; creating backend `main` fires ci.yml | M/L | team | decide before first PR / `main` | first PR | 2026-11-18 |

## Decisions on open questions (2026-11-11, decided by orchestrator, with Fable)
| # | question | decision | reason |
|---|---|---|---|
| D1 | Is e8e90954 the handover head? | Yes; W1 pinned by SHA 659ea29..e8e90954 | integration branch's merge-base with drop = e8e90954; d10e065 would give a known-red pin |
| D2 | Where is "one full sale"? | On `claude/commercial-integration-20261111` (0135 terms, commerce.py, sale-path test) → W2 | not in drop; W1 has sale prerequisites only |
| D3 | Undisclosed test_real_transport_pg change | High seed for W1 L3/L7; rule-7 breach reported; assertion left as is; our test `test_rule_retained_vault_unsellable_pg` covers sellability | we never edit existing assertions |
| D4 | Merge origin/handover/20261111? | Never merge or check out; `git show` only | CLAUDE.md prepend changes agent instructions; infra identifiers |
| D5 | §8 PYTHONPATH incomplete | Use `<root>:<root>/service:<root>/service/tests:<root>/ops/plan-c-supervision`; fix upstream in twin-ai-kit | handover/ is a kit copy, not edited here |
| D6 | Read handover `docs/handover/context/**`? | Deferred; revisit at W1 close (recall diff vs independent-review-7aa81dc) | not needed for steps 3–5; YAGNI |
| D7 | `ONLYFANS_DASHBOARD_READ_TOKEN` set? | Treat as not set | handover lists none; unverifiable without admin; irrelevant while no PRs |
| D8 | Guard early-stop 400 host vs 300 git | Git 300 is the reference; R3 stays H/H, owner reconciles | host access = prod stop item |
| D9 | 12 nested conversation_engine tests not run | Don't edit runner (outside §3); run by explicit path; REVIEW.md blocks tests not proven to run; new tests top-level only | scope |
| D10 | Next window head | W2 = e8e90954..commercial-integration (pin at cut); step2-transition never a head | step2 commits are inside integration |
| D11 | Codex/DeepSeek lenses | Codex Sol + Astra run (dev tool, /tmp worktree, -m pinned); DeepSeek unavailable | no scrub-diff.sh; paid API |
| D12 | REVIEW.md approval | Approved 234155de after Fable + devil's advocate, 3 rounds | owner delegated approval |
| D13 | W1 mode | Review-only, lanes by directory, no fixer on team code, 2 in flight, not launched | team's Claude owns engine fixes |
| D14 | Rules-test base | e8e90954 + new test files only (not working branch) | working branch carries our unreviewed d6978211 (own /rwl later) |
| D15 | Green rule tests | Accepted only with a break-once mutation proof; `passes` true only where proven and no slop component pending | no manufactured reds |
