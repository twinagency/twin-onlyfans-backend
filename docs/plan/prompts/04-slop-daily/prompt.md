# 04 · Slop harness + nightly full run (T3-scheduled, 02:00)

Rules (global CLAUDE + infloww `slop/README.md`): ONE `slop/` per repo, extend never fork; seed by SQL; act only through real endpoints; fake only at wire boundaries outside service code; declare every bypass; every heavy run through `~/.slop/gate.py`; `--scale=small` to iterate; medium/120-user alone (gate exclusive; 200 only on demand). Team ticket 04 ("one slop/, none at tip") is this work: absorb the backend load test and Moxy qualify runs instead of adding a second harness. Branch `kit/slop`.

Build (implementer) per repo:
- `slop/README.md` — contract: commands, scales, scenario list, bypass list, invariants, evidence layout, how to add a scenario, how FAULT works.
- `slop/run` — `--list` (one-time setup), `<scenario> [--scale=small|medium --users=N --no-ui --rate]`, `--changed` (own/flow picks from `slop/map.json`, 15-min budget, `--budget=0` for shared code), `all [--map]`, `nightly [--detach --apply-map=<folder>]`, `--break --baseline=<green folder>` → prints CAUGHT|MISSED, `--share`. nice 10; gate slot per scenario; evidence `slop/evidence/<UTC>-<sha>-<scenario>-<hex>/` with `summary.txt`, `invariants.json`, `checks.json`, logs, screenshots; nightly adds `status.json` (pid, updated), `done`, `summary.md` (first line `NIGHTLY OK|RED|INFRA <stamp>: ok/flaky/fail/infra/skip of n`), `plan.txt`, `map.json`.
- `slop/lib/` — gate (loads `~/.slop/gate.py`), stack (temp DB, loopback only, fake endpoints), faults (`FAULT` with `targets=[checks]`), nightly driver, map.
- `slop/seed/*.sql` — deterministic from `--seed`, only product-writable rows. `slop/fakes/` — Telegram (reuse `.local-dev/fakes/telegram-fake.mjs`), OF relay upstream, AI providers, email. `slop/actors/` — simulated users. `slop/scenarios/` — one per user flow; latency budgets in the scenario file.
- Scenarios first: `inbox-storm` (200 fans, order kept, nothing dropped; FAULT drops one), `mass-message` (queue drains, counts match; FAULT double-send), `dashboard-browse` (50 users × key pages, no 5xx, p95; tag volume), `toggle-roundtrip` (save → runtime reads it; FAULT half-wire), backend: `relay-admission` (user lane never starved by bg walk).
- Invariants every run: nothing dropped, counts/order kept, no 5xx, p95 vs last green, no egress to prod/paid hosts (egress hook), no secrets in evidence.
- Nightly: AI schedules `schedules/nightly-slop.md` (fixed 02:00) in a dedicated detached worktree `~/<repo>-slop-nightly`; map PR auto-merged (generated files only); fix PRs never auto-merged; ≤2 fixes/night; never loosen a budget to go green.

Proof: top-level spawns `slop-runner` (Sonnet) → `slop/run --changed --scale=small` GREEN (folder name); `all` once; break-it-once per FAULT → CAUGHT, revert → green; one medium/120 alone. Opus judges reds caused/pre-existing/flake. `/rwl`. `docs/learn/04-slop.md`. `Ship:` line.

## Also do (from GAPS.md)
- Engine rules → tests: build every row of `handover/TEAM-BRIEFING.md` §7 (send-authority, unknown-never-retried, supersede, tenant isolation, price/bundle matrix, migration pins) as unit tests beside the code or slop invariants; one `features.json` entry each.
- Row 10: scenario `kill-switch` — flip `OUTBOUND_PAUSED` mid-storm → 0 sends within 5 s, queue intact, flip back → drains in order.
- Row 15: screenshots only from seeded synthetic data; `slop/evidence/**` scanned (gitleaks + PII regex) before any upload; PRs carry the folder name only.
- Row 34: `slop/fakes/README.md` with last-verified date per wire fake (Telegram, OnlyFans relay, BrowserBase); fixtures refreshed by a human canary on a test account; 11h sweep flags fixtures > 30 d.
- Row 43: invariant `deleted fan → 0 rows anywhere` across every table carrying a fan id.
- Row 35: deterministic unit tests for state machines live beside the code, not in slop.
