# Cristina recovery tools: review notes (offline, nothing deployed)

Branch `claude/cristina-recovery-tools-20261112`, base `3b8fc64`. For owner/Jesse review only.
No deploy, restart, activation, FansAPI or model call was made. Deploying either piece needs a NEW reviewed image
and release pin (`reviewed-recovery-release.json`): an owner decision. Both tools run inside that image.

## Piece 1: `readiness_unavailable` monitor-incident class

Incident: the supervisor stopped at 11:17:48 UTC with `pilot_supervisor_stopped:readiness_unavailable`
(sender `/readyz` not 200, e.g. 503 `loop_stale`). Cause is strongly supported (slow model chain, 17.5s then a retry)
but UNPROVEN. That is carried in every receipt and printed by the resolver (`READINESS_CAVEAT`).

Unlike the 11 Nov classes it is NOT added to the thin v2 receipt. A stopped sender loop can strand a send, so it
resolves only through a new receipt version `plan-c-monitor-resolution.v3` that is valid for this class only
(v2 can no longer carry it, v3 can carry nothing else).

Rules added (ops owner `ops/plan-c-supervision/incident_state.py`, database owner
`service/onlyfans_runtime/monitor_incident_recovery.py`; the two must agree and a test compares them on a corpus):

| rule | where |
|---|---|
| class registered; `V2_CLASSES` excludes it; v2 refuses it | incident_state.py:29-31, :136; monitor_incident_recovery.py:23 |
| journal must show exactly ONE `pilot_supervisor_stopped:` line and it is `readiness_unavailable` (a second line, even the same class, or any other class refuses). Re-proved in the image by the validator | incident_state.py:74 `journal_class`; validate_interval.py:18 `_readiness` |
| bounded interval: starts at the marker's last-healthy instant, `start < end <= resolved_at`, at most 24h (tighter than v2's 7 days) | incident_state.py:40, :136-148; monitor_incident_recovery.py:34, :91-105 |
| sender absent and scope `stopped`: collector refuses a running sender / any non-stopped scope and records `sender_absent`, `scope_status`; validator refuses unless both are exactly true/`stopped`; resolver re-asserts sender stopped; DB owner requires scope stopped, runtime and policy disabled | collect_monitor_evidence.py:42; validate_interval.py:18-39; monitor_incident_recovery.py (existing scope checks) |
| unresolved or unknown send: every queued/dispatching/unknown text intent and dispatching/unknown independent admission must be NAMED by the operator, exactly (no more, no fewer). Collector lists them, resolver compares before auditing, validator re-compares, DB owner re-checks inside the locked transaction | collect_monitor_evidence.py:25,43; resolve_monitor_incident.py:30 `attest_readiness`; monitor_incident_recovery.py:112 `unresolved_sends`, :163 |
| operator evidence reference required: `operator:fansapi:<6-108 chars>` (same rule as `evidence_allowed('fansapi', ...)`), no control chars | incident_state.py:49 |
| unproven-cause caveat retained (receipt field, must equal the constant) and printed (stderr `CAVEAT:` line plus `cause_caveat` in the JSON result) | incident_state.py:42; resolve_monitor_incident.py (before `state.resolve`) |
| receipt identity binds incident, evidence hash, interval, revision, evidence reference and named sends | incident_state.py:86 `command_key_v3` |
| the v3 interval counts for delivery accounting like v2 (`resolved_intervals`) | incident_state.py |

Operator attestation is written to `operator-attestation.json` in the evidence directory BEFORE the evidence hash is
computed, so it is bound into `evidence_sha256`.

Deliberately NOT done: no send permit, no hold release, no settlement of a named send (it stays `unknown`), no
activation, no new retry, no change to supervisor.py / guard thresholds / send path. Naming a send acknowledges it; it does not resolve it.

Commands (run by the existing restricted host owner, as today; sender must stay stopped):
```
# 1. dry look: collection itself writes nothing but retained evidence. It prints the unresolved sends to name.
python3 resolve_monitor_incident.py --evidence-ref 'operator:fansapi:<what you reviewed>'
#    refuses with the list of unresolved sends if any exist and are not named. Then re-run naming each:
python3 resolve_monitor_incident.py --evidence-ref 'operator:fansapi:<what you reviewed>' --named-send <intent_or_attempt_id> [--named-send ...]
# crash after the DB audit: resume the exact saved evidence (attestation is inside it)
python3 resolve_monitor_incident.py --resume /srv/projects/<root>/evidence/<exact-attempt>
```
Note the resolver has no separate dry-run: its first run collects evidence and, if every rule passes, audits and
publishes (as the existing resolver always has). The refusal output is the preview. Flags are refused for every other class.

## Piece 2: conflicting provider event key review (`resolve_conflicting_webhook.py`)

Hold: one `messages.sent` key arrived twice with different payloads (non-test fan <NON_TEST_FAN_ID>). The first variant
(outside fan, not held) leaves a digest row only and NO retained bytes; the second is held and its bytes retained. A conflict
re-holds every variant account-wide, so both fans show `pilot_evidence_pending`. `_reconcile_provider_event` refuses any
conflicting key and the queue review accepts chat_queue events only.

Code: `service/conversation_engine/conflicting_webhook.py` (logic), `ops/plan-c-supervision/resolve_conflicting_webhook.py` (CLI).
Pattern: the queue review's (`operator_v2._reconcile_provider_queue`): audit record + one `digest_resolution_key` marker per
variant, one transaction. The only edit to existing logic is `pilot_scope.CONFLICT_REVIEW_KINDS` (pilot_scope.py:324, :349, :357):
the unresolved-digest view treats the new marker kind `plan_c_conflict_marker` like the queue marker. A later variant of the key
still re-holds both fans (tested).

Rules (all in `review()`, all refuse; none relaxes another owner's rule):
- dry run by default; `apply=True` / `--apply` required to write. Dry run runs the same checks including stopped scope.
- requires actor (named human + `reconcile` permission), scope revision, 64-hex command key, evidence statement `operator:fansapi:...`, event key, epoch, and the expected fan.
- scope must be `stopped`, text-only, runtime/policy disabled, revision as seen.
- the key needs >= 2 retained variants; any row in `of_fans_webhook` / `of_fans_webhook_conflict` for the key (projected/quarantined) -> belongs to its existing recovery owner, refused.
- exact bytes of EVERY variant proven: retained receipt bytes, or operator-supplied bytes whose SHA-256 equals the retained digest (or whose canonical JSON serialization does: reported as `operator_supplied_reconstructed`). Never assumed; no bytes -> refused.
- every variant: valid envelope, signed for this connection's provider account, event is `messages.sent` or `messages.received` ONLY (money, ownership, subscription, unknown refused whatever fan), documented fan == the named fan, fan not a trial fan, and no key/value anywhere in the body equals a trial fan id.
- BOTH variants are kept: digests and receipts untouched; the audit record keeps the exact text of every variant.
- releases only this key; after writing it re-checks the key left the unresolved view or rolls back. Other unresolved keys still hold (tested).
- idempotent by command key; a key reused for another command conflicts; a marker already owned by another resolution kind refuses.
- `reviewed_conflicting_delivery()` lets the interval validator accept provider deliveries of the reviewed key (otherwise Piece 1's interval validation would refuse the 11:33 delivery as "digest evidence is missing or conflicting"). It re-proves each reviewed variant and uses the trial fans recorded in the audit.

Commands (inside the reviewed image, DB url file as for `db_audit.py`):
```
python resolve_conflicting_webhook.py --tenant <t> --account <a> --event-key evt_ba4bc155... --event-epoch 1 \
  --expected-fan <NON_TEST_FAN_ID> --scope-revision <N> --command-key <64hex> --actor op-session:<64hex> \
  --evidence-ref 'operator:fansapi:<statement>' [--variant-file first-variant-bytes.json]      # DRY RUN
python resolve_conflicting_webhook.py <same args> --apply                                       # records and releases
```
Exit code 1 and `{"refused": true, "reason": ...}` on any refusal.

Limits: if the first variant's exact bytes cannot be produced (hash must match), the tool refuses and the owner must decide.
Only `messages.sent`/`messages.received` are reviewable. A variant already carrying a `plan_c_webhook_resolution` marker refuses.

## HOST COPIES DIFFER FROM THE REPO (read-only SSM, `/srv/projects/twin-plan-c-cristina-20261112/text-trial/supervision-v11/`)

Pulled and diffed against `3b8fc64` (sha256 of the host files match the live ones). Every difference is a string substitution:

| file | host differs from repo |
|---|---|
| all | `ROOT` = `/srv/projects/twin-plan-c-cristina-20261112/text-trial` (repo: `twin-plan-c-pilot-20261109`) |
| incident_state.py | `ACCOUNT='of-63665785'` (repo `of-323358988`). `BINDING` is UNCHANGED (still the old pilot binding `01612323-...`): confirm it is Cristina's binding id or no class receipt can validate (pre-existing, not changed here) |
| supervisor.py | scope SQL account `of-63665785`; psql role/db `<cristina_trial_db>`; provider `FANSAPI_ACCT` `<FANSAPI_ACCT>`; LOGIC: `external_reservations` starts at `38+readjson(BASELINE)['prior_scope_credits_carried']` (repo: `38`) |
| guard_core.py | early stop `>= 500` (repo `>= 300`) with a comment citing owner approval 12 Nov |
| resolve_monitor_incident.py | sender container `twin-plan-c-cristina-active-sender`, unit `plan-c-cristina-sender.service` |
| collect_monitor_evidence.py | psql role/db, account `of-63665785` in the receipts/digests/queue SQL, container/unit names, `FANSAPI_ACCT` |
| validate_interval.py | `PROVIDER='<FANSAPI_ACCT>'`. NOT changed: `FANS=(318022380, 601164546)`, `REQUIRED_PILOT_RECEIPT` and the delivery `URL` are still the old pilot's values on the host. |
| db_audit.py | `record_resolution(store,'plan-c-cristina','of-63665785',...)` |

Compatibility: I applied exactly those substitutions to my new repo files and diffed them against the host copies; only my additions differ
(`diff` output retained in the session). So the new files can be installed on the host by re-applying the substitutions. A plain
`patch` of my diff does NOT apply cleanly (context lines touch the substituted lines). New code takes the account from
`incident_state.ACCOUNT` (host-correct) and never hardcodes an id; the CLI requires `--tenant`/`--account`.

Flags that matter, for the owner:
1. `service/onlyfans_runtime/monitor_incident_recovery.py` (inside the image) hardcodes `ACCOUNT='of-323358988'`, `BINDING`, `PROVIDER`, `CREATOR`, `FANS`
   and `record_resolution` refuses any other account. The host `db_audit.py` calls it for `of-63665785`. I could not inspect the live image. If the
   Cristina image is not built with her constants, NO class receipt (old or new) can be audited. Confirm before relying on this. My PG tests monkeypatch these like the existing ones.
2. Host `FANS` / `REQUIRED_PILOT_RECEIPT` / `URL` in validate_interval.py are the old pilot's. The class-based path ignores REQUIRED_PILOT_RECEIPT. For the new conflicting-delivery check I use the trial fans recorded by the review itself (from the DB scope), unioned with `FANS`.
3. Early stop is 500 on the host and 300 in the repo; the supervisor's credit carry-over logic differs. I changed neither; the collector reuses `supervisor.external_reservations()` and `credit_estimate` as installed.
4. The readiness collection reads the sender-process and DB state through the host's existing helpers (`supervisor.db_state()`), so it follows the host's account.

## Tests, and what was changed

- New: `ops/plan-c-supervision/test_readiness_class.py` (23), `service/tests/test_plan_c_monitor_readiness_pg.py` (23), `service/tests/test_plan_c_conflicting_webhook_pg.py` (25).
- Committed red first (`7aaf8bd`): ops 21 failed / 2 passed; the two PG files error at collection (no implementation).
- Changed after the red commit, all inside my own NEW tests, none weakening: (a) test helper fix: a dropped journal file was not actually removed between sub-cases; (b) replay test reused one receipt object (a fresh `resolved_at` is a different command); (c) corpus helper built a key from a non-string named send; (d) the "wrong bytes" case appended a space, which is legitimately reconstructable (trailing whitespace is not in the canonical form), replaced with genuinely different text. No pre-existing assertion was changed.
- Environment: 18 ops tests fail at base and after (uid 1000 cannot `chown`); R0 acceptance tests need `/opt/r0-baseline`.

## Suite comparison against base 3b8fc64 (what ran, what did not)

Harness: image `twin-engine-offline-main:20261107-r6`, `--user 1000:1000`, `--cpus 1 --memory 1g`, own `postgres:16` container, logs in
`/srv/projects/claude-g1-logs-20261112/`. `tests/test_plan_c_*.py` (base list, 71 files) split into 3 shards; ops tests run from the ops directory with the host guard path added.

| set | base 3b8fc64 | this branch |
|---|---|---|
| ops/plan-c-supervision tests | 18 failed, 106 passed | 18 failed, 129 passed (the 23 new); failing set IDENTICAL (all uid-1000 `chown` PermissionError, environment-only) |
| plan_c shard 00 (390 tests) | 390 passed | 390 passed |
| plan_c shard 01 (233 tests) | 1 failed (R0 acceptance, needs `/opt/r0-baseline`), 232 passed | NOT COMPLETED (stopped at ~30% on coordinator instruction) |
| plan_c shard 02 | NOT RUN | NOT RUN |
| new tests, run separately | n/a | readiness ops 23 passed; readiness PG 23 passed; conflicting webhook PG 25 passed |

So the "fail set identical to base" claim is proven only for the ops suite and shard 00. Shards 01 (partly) and 02 were not completed on this branch. Shard 02 was never run on either side. Before merge, run shards 01 and 02 on both. The two files that
this branch most affects outside the new tests (`test_plan_c_monitor_class_resolution_pg`, `test_plan_c_monitor_resolution_pg`, `test_plan_c_events_stage3_pg`, `test_plan_c_queue_review_pg`, `test_plan_c_auth_recovery_pg`, `test_plan_c_scope_pg`) must be in the shards re-run. The `pilot_scope.unresolved_webhook_digests` edit (CONFLICT_REVIEW_KINDS) touches the shared hold view used by all of them.
`test_supervisor.py` and `test_webhook_health.py` named in the brief are in the ops directory only as `test_supervisor_monitor_loop.py`, `test_supervisor_timing.py`, `test_delivery_history.py`; those ran (in the ops run above).

## Known limits

- Resolver (Piece 1) has no separate dry-run mode: the first invocation collects and, only if every rule passes, audits and publishes. Its refusals are the preview.
- Piece 1 ties validation to the DB owner's hardcoded pilot constants (see flag 1 above) and to the host `FANS`/`URL` values; unchanged here.
- Piece 2 refuses if any variant's exact bytes cannot be produced, if the key was projected/quarantined, or for any event other than messages.sent/received.
- Neither piece was run against the live host or live DB. Nothing here proves the incident's real rows meet the rules; the dry run will show that.
- A new reviewed image and release pin are required (owner decision). The host copies need the substitution list above re-applied to the new files.
