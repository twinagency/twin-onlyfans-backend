# 02 · UI speed markers + backend reader

Goal: dashboard pages report load time; backend aggregates; a check goes red on regression. Repo: twin-dashboard (Next.js). Branch `kit/perf-markers`.

Design (YAGNI, no new deps):
- `lib/perf.ts`: `markPage(route)` using `performance.mark/measure` + Web Vitals already exposed by Next (`useReportWebVitals`); batch-send via `navigator.sendBeacon` to `/api/perf`.
- `app/api/perf/route.ts`: POST stores `{route, metric, value, ts, build}` into a `perf_samples` table (migration in `../bot/db/migrations/` per repo rule; local Supabase only). GET `?route=&days=` returns p50/p95 per route per day.
- Instrument 3 critical pages first: login, clones list, chat inbox. Marks named `page:<route>:ttfb|fcp|lcp|hydrated|data-ready`.
- Check script `scripts/perf-check.mjs`: fails (exit 1) if today's p95 for any route > 1.2 × last green value stored in `slop/evidence/perf-baseline.json`.
- Logging rule: markers are structured JSON, one line, no `console.log` prose. Remove any debug prints you add.

Proof: `dev.sh up`, load the 3 pages via `operator` agent (screenshots in run folder), `curl localhost:3001/api/perf?days=1` shows rows, `perf-check.mjs` green; then throttle one page (artificial 2s delay in dev) and show it red; revert.
Then `/rwl`, `slop/run --changed` (add scenario: 50 users × 3 pages, no 5xx, p95 recorded). Write `docs/learn/02-perf.md`. `Ship:` line.

## Also do (from GAPS.md)
- Row 33: baseline lives in tracked `slop/baselines/perf-baseline.json`, updated only from a green main run; a red needs n ≥ 30 samples/route/day and a 7-day rolling baseline (operator traffic is tiny; single-day p95 is noise).
- Row 32: add Lighthouse CI (`lighthouserc.json`: LCP, TBT, score assertions; 3 runs) on the PR preview URL as the stable synthetic gate; RUM is the prod truth, LHCI is the PR gate.
- Row 27: ship web vitals to the error tracker/OTel too (Sentry EU or GlitchTip, release = sha) so perf and errors share one timeline.
