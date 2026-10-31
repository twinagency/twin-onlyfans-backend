# OnlyFans commercial readiness handover

Audit date: 31 October 2026, Brisbane time. This folder preserves the completed read-only audit and the subsequent developer brief. It does not implement the scale redesign or certify production readiness. Recheck current source and deployment before acting on this snapshot.

## Read first

1. `Twin-OnlyFans-Commercial-Readiness-Audit-2026-10-31.pdf`: original eight-page report, evidence references and limits.
2. `AUDIT_TEXT.txt`: searchable text extracted from the original PDF. PDF tables and links are clearer in the PDF/source.
3. `DEVELOPER_BRIEF.md`: requested workload, delivery requirements and API documentation checklist.
4. `build_audit.py`: original report generation source. Its ROOT is a scratch path from the audit session; change ROOT/OUT when reproducing. Requires ReportLab, DejaVu fonts and an existing output directory.

## What was completed

Read-only inspection of production containers, resource settings, running source, database aggregates, bounded logs, stored test reports, relevant GitHub repairs and upstream FASTT source. Report generated and visually reviewed. No application code changes were made in this audit workspace. No service restart, production load test, fan message or purchase was performed as part of the audit.

This handover includes all substantive artifacts created for this audit. Temporary rendered page images, library transfer helpers and the user-uploaded screen recording are not application changes or audit deliverables.

## Findings that drive the work

- A 20-second account-wide send interval caps theoretical output at 180 sends/hour or 4,320/day per creator. Even one reply for each of 10,000 chatting fans exceeds that ceiling. This is observed Twin runner policy, not a proven OnlyFans requirement.
- Twenty fans arriving together expose the issue: with the first send at 25 seconds, the twentieth cannot send before 405 seconds at that interval.
- Preparation concurrency defaults to four per account, with a ceiling of 16. The supervisor defaults to 64 eligible accounts, configurable up to 256. More preparation alone does not remove the sending bottleneck.
- Production used one shared 8-vCPU / roughly 32-GiB host. API limit 0.5 CPU/256 MiB/one worker; sync 1 CPU/768 MiB; sender 2 CPU/2 GiB; Postgres 2 CPU/4 GiB and 150 connections. These are historical observations, not new sizing recommendations.
- Small completed-turn samples do not prove capacity. A recent Alva response exceeded 180 seconds. Include unanswered turns and distinguish live arrivals from historical backlog when measuring.
- Proactive evaluation was running, but this audit did not prove an end-to-end proactive offer, delivery, payment, unlock and purchase-memory cycle.
- Vault descriptions and launch-history/purchase imports remained incomplete. Track useful sale-ready inventory, not only imported item counts.
- A frozen-dispatch unresolved-delivery state repeatedly reconciled without resolution. Event counts are not distinct failed fans.
- The native ownership-list pilot cannot prevent manual sending from Infloww. Its operational coexistence guarantees still need evidence.

## Preserve the repairs

| Issue | Attribution from audit | Reference/status at audit |
| --- | --- | --- |
| Unsolicited photo ratings/point deductions | Inherited FASTT prompt/routing | Explicit-rating-only repair present in running photo/caption release |
| Media gets no usable response | Mixed engine and Twin admission/validation | Caption fallback present; unsupported media/paid input outcomes still require review |
| Manual free/paid media blocked by history holds | Twin integration | Backend PRs 62/64; 35 checked accepted manual intents matched saved provider text and price |
| Creator facts expire and block replies | Twin integration | PR 67 reported deployed |
| Historical identity question retriggers pause | Twin replay/control | PR 63 repair exists |
| Narrow keyword gate suppresses proactive offers | Twin contextual addition | PR 68 evaluator present; complete sale still unproven |
| Account-wide throttle and preparation limits | Twin deployed runner | Current capacity blocker |
| Purchase/vault import starvation | Twin sync and provider dependencies | PRs 60/69 partial repairs |

Backend references: https://github.com/twinagency/twin-onlyfans-backend/pull/60, /62, /63, /64, /65, /67, /68, /69, /70. Dashboard bulk controls: https://github.com/twinagency/twin-dashboard/pull/340. Treat PR references as repair evidence; independently check merge and running-image status before claiming a current deployment.

## Recommended implementation order

1. Reconcile running overlays with repository source and produce one reproducible release. Preserve newer fixes and a rollback artifact.
2. Establish actual supported OnlyFans/provider sending and reading budgets. Design concurrent processing across fans with ordered work within each fan, durable recovery and receipt reconciliation.
3. Resolve uncertain delivery, instrument every input through its terminal outcome, and cancel pending work safely on human takeover.
4. Prove manual and contextual media sales through purchase/unlock and owned-media exclusion. Split valid keep-chatting decisions from evaluation errors.
5. Complete fair imports, memory freshness, catalogue descriptions and visible onboarding gates across creators.
6. Size workers, database/pools and API against the agreed fleet and peak, then run the acceptance matrix in the PDF on the assembled release.
7. Publish accurate API documentation from actual routes/schema. The developer brief requests documentation; it does not supply a verified API URL, key or endpoint contract.

## Resolve these specification details

- 10,000 daily chatting fans does not mean 10,000 simultaneous requests. Agree reply turns per fan, active hours, peak arrival rate and creator count. The PDF proposes 10 replies/fan/day and five-times-average peaks for planning.
- Agree the timer anchor for grouped messages. A minimum measured from the latest bubble can be reset indefinitely by continuous messages. Preserve an explicit maximum waiting deadline from the first unanswered input.
- The user's requirement is 25 to 180 seconds. A later draft proposed 99% within that window; that was not an agreed relaxation of the user's maximum. Use the PDF's acceptance requirement for every in-scope test response, and explicitly report exceptions, failures and unanswered work.
- Internal durable scheduling is necessary for pacing and recovery. The requirement is independent fans progressing without sustained backlog inside the agreed load, while respecting verified provider limits.
- Preserve intentional new-fans-off configuration. Connected/runtime-disabled accounts alone are not proof of activation defects.

## Evidence retrieval

The PDF pins image IDs, inspected source paths and read-only SSM command IDs. Its last page records known test limits and historical fixture failures. Raw production logs and database exports were not retained in this workspace; retrieve bounded evidence using those references if access is available. Do not treat this snapshot as a fresh production check.

## Report back

For each fix: exact cause, FASTT/Twin/provider attribution and confidence, changed files/commit, affected scope, meaningful tests, release image, deployment status and rollback. List any blocked requirement explicitly. Do not claim commercial readiness from synthetic mocks alone; validate provider limits with a controlled canary after full-stack simulation and recovery tests.