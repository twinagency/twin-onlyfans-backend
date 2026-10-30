# Reconciliation checkpoint: parallel implementation reached production

30 October 2026, Brisbane. This work has not deployed or enabled its candidate.

The existing native sender image changed during this review. The tested baseline was immutable sender `sha256:42a374a0e29fa08419c2ec4987299d187215a28066528a48d1ac94c79a97e84a`, which included the concurrent queue and contextual offers. Its six relevant source files were reconciled without replacing other overlays. A later read-only check found sender `sha256:2fa1dd0604814eb0a287e67599d4215a648169c3078c1ac0c871069656c01e8c` and changed source hashes in five of those six files. Its Service.turn already contains a different durable pacing implementation, including pacing.settings/prepare/finish and a captured reply authority. Do not layer this candidate over that implementation.

Independent PR #59 and dashboard PR #337 implement the same requested features. They were not created or assigned by this session. Their descriptions still say not deployed; those descriptions alone do not establish the current live feature settings. No person was contacted. Current creator switch values have not been verified by this work. Automatic approval review refused the attempted read of creator nicknames/configuration JSON as outside the approved six-source-file review scope.

## Tests on the 42a374a production baseline

- 84 focused pacing, format, recovery, receipt and identity cases passed against the assembled image. A missing format configuration propagation seam was fixed before this pass.
- Four API negotiation cases passed. New fields require replyPolicy=1, so older exact response parsers remain compatible. Contextual negotiation stays independent.
- Real PostgreSQL run: 71 of 72 cases passed across policy, configuration, takeover and manual sending. The historical second-bubble fixture attempted dispatch two seconds after a refusal despite the existing 20-second account throttle. Its failure reproduces on the unchanged baseline. A fixture-only 30-second timestamp correction passes the distinct-attempt test; production throttling was preserved. This is 72 unique cases passing across the recorded runs, not a single clean 72-case run.
- Ten native script suites passed. The old service pace-stop fixture fails identically on the unchanged baseline and candidate, expecting turn-abandoned where the live source emits turn-released.
- Synthetic real-Service PostgreSQL load completed for baseline, candidate off and candidate on. The initial fixture had inconsistent credential revisions; the real account guard correctly refused delivery. All cohorts were corrected to the same canonical revision and rerun. Final candidate on delivered 19 replies, paused/took over six fans, refused both in-flight controlled sends, and observed zero sends after pause/takeover. Baseline/off comparisons and detailed timing/queue metrics remain on the host. This short accelerated harness uses synthetic model/provider boundaries, does not measure full prompt-building CPU, and establishes neither production capacity nor real provider delivery latency. An off/on throughput comparison needs interpretation of intentional waiting and the finite observation window before any acceptance claim.
- Reconciled frontend configuration contract and focused TypeScript checks passed. Full build dispatched for dashboard candidate 79ae9fcb64d0e5537767cfa5494f45903bf2097d, run 41018396322; its result is not asserted here.

## Artifacts and scope

`current-six-files.patch` and `current-six-files.json` capture only the six approved source-file differences/hashes against the historical tested image. They are evidence, not a complete deployment package. Scheduling modules, configuration/API negotiation and splitter changes are additional dependencies. They must not be applied to the later live pacing implementation.

The isolated on-host candidate, test-only images, synthetic logs and replay harness are under `/tmp/reply-pacing-reconcile-2399`. Disposable PostgreSQL/load containers and internal networks were removed in finally blocks. No production container was restarted, no production schema/configuration was written and no fan test traffic was initiated by this work.

Next action is to establish the provenance and effective scope of the already running pacing implementation and consolidate the duplicate work. The current live deployment supersedes this candidate's source baseline. A fresh reconciliation and release gate is required before any further promotion; restoring this older image would erase later work.
