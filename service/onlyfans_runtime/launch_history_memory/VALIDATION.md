# Offline validation record — 2026-10-23

Branch: `codex/onlyfans-history-memory-20261023`.
Pinned backend parent: `dbd09b06dace2c24829ded6bc778d7e6d2ded4f0`.
Dashboard contract baseline: `927b2d75105f6180c37f2759ed119f89521b979d`.

## Environment and boundaries

Tests used synthetic fixtures and a dedicated PostgreSQL 18.4 instance bound to
127.0.0.1:56473. Each database fixture bootstrapped a disposable database through
the pinned migration head `0089_offer_import`, installed owned test metadata,
then dropped the database. Dependencies were installed in a separate local test
environment from the pinned test requirements. No dependency file was changed.
No production credentials, accounts, provider reads, model calls or fan messages
were used. Synthetic model generation exercised the real native planner.

The owned modules do not register themselves in shared runtime or API entry
points. Planning tests explicitly install test-local versions/prompt adapters.
These prove the proposed integration behavior, not its deployment. A populated
combined migration, shared integration tests and actual provider coverage remain
Claude's integration work. No production migration, activation, merge or deploy
was performed.

## Exact results

All commands below run from the backend checkout with `PYTHONPATH=service;service/tests`
on Windows, `ONLYFANS_TEST_PG_URL` pointing only to the disposable local server,
and a Python environment containing the pinned test requirements.

| Test group | Result | Duration |
| --- | --- | --- |
| `test_onlyfans_launch_history_memory.py` + `test_onlyfans_launch_history_memory_pg.py`, final run | **59 passed**, 1,411 warnings | 189.22 s |
| `test_onlyfans_launch_history_memory_recent_pg.py` + `test_onlyfans_launch_history_memory_planning_pg.py`, final run | **14 passed**, 4,098 warnings | 221.36 s |
| `test_onlyfans_launch_history_memory_recovery_pg.py`, initial full run | **29 passed**, 266 warnings | 396.56 s |
| Recovery focused follow-up, including two added cases and three affected existing cases | **5 passed**, 26 deselected, 131 warnings | 84.20 s |
| Existing native memory, native history, offer import and recovery fairness regressions | **49 passed, 1 failed**, 353 warnings | 386.92 s |
| Dedicated suite collection | **104 tests collected** | 4.42 s |
| Owned module compilation | **Passed** | — |
| Generated migration SQL inspection | **7 tables, 2 append-only triggers** | — |
| Staged whitespace/diff check | **Passed** | — |

The dedicated runs cover **104 unique cases** (45 pure, 14 memory database,
31 recovery database, 11 recent cutover database and 3 planner database cases).
The 29-case recovery run and five-case follow-up overlap by three; they are not
34 unique cases. This is a record of separate passing runs, not a claim of one
104-case invocation. Warning counts are preserved; the existing models emit
`datetime.utcnow()` deprecation warnings.

An intermediate recent/planner run found an incorrect stale-claim failure path
(1 failed, 13 passed). The implementation now validates the persisted claim
before selecting initial recovery versus existing catch-up. The final 14-case
rerun above passed.

The existing regression failure was
`test_onlyfans_native_memory_pg.py::test_encrypted_restore_preserves_memory_cost_and_append_only_provenance`.
It failed in `operations._client` before backup execution with
`FileNotFoundError: [WinError 2]`: the installed portable PostgreSQL package has
the server but no `pg_dump`/`pg_restore` clients. No client was found on PATH or
in the standard local PostgreSQL installation location. Backup/restore is
**unverified in this environment**, not a passing test or an attributed product
regression. Shared operations code and its test were not modified. Claude must
run this test with compatible PostgreSQL client binaries during integration.

Final paired test commands:

```text
python -m pytest -q --disable-warnings service/tests/test_onlyfans_launch_history_memory.py service/tests/test_onlyfans_launch_history_memory_pg.py
python -m pytest -q --disable-warnings service/tests/test_onlyfans_launch_history_memory_recent_pg.py service/tests/test_onlyfans_launch_history_memory_planning_pg.py
```

Recovery suite command (the record above includes the initial full run and the
focused follow-up rather than an unnecessary repeat of already passing cases):

```text
python -m pytest -q service/tests/test_onlyfans_launch_history_memory_recovery_pg.py
```

Existing regression command; `ONLYFANS_TEST_PG_BIN` must contain compatible
`pg_dump` and `pg_restore` clients to complete the backup test:

```text
python -m pytest -q service/tests/test_onlyfans_native_memory_pg.py service/tests/test_onlyfans_native_history.py service/tests/test_onlyfans_offer_import.py service/tests/test_onlyfans_recovery_fairness_pg.py
```

## Evidence of memory and recovery behavior

* More than 1,200 canonical messages place a distinctive fact and interaction
  in the middle of history. Retrieval reaches them while the native latest-100
  window stays intact. Common-word filler cannot outrank the distinctive query.
* A synthetic model receives historical evidence through the real composer and
  native planner. Corrections or source retractions during generation invalidate
  its plan. Historical processing itself creates no reply, send or sale intent.
* Initial recovery silently establishes recent coverage and resumes older pages
  independently. Short pages persist across restart; initially empty history
  does not suppress its first later live message. Existing outage gaps are not
  truncated to 100 messages. New inbound planning progresses while archive work
  remains pending; active live work takes priority over archive writes.
* Source receipts and evidence commit atomically; restart, rollback, duplicate
  replay, late older inserts, edits and unsends are covered. Expired claims,
  connection revisions and cursor generations fence stale results.
* Creator/account/fan and agency access are scoped, including binding revocation.
  Conflicting provider evidence cannot advance archive coverage. Same-page offer
  observations survive without turning priced messages into payment evidence.
* Canonical durable facts retain precedence. Manual set and clear commands use
  optimistic concurrency, idempotency and append-only provenance. Clears resist
  old imports and automatic proposals. Full canonical values affect correction
  versions even when prompt projection clips an oversized legacy value.

Extraction is deliberately conservative English pattern recognition. Historical
retrieval is lexical and source-linked; saved profiles are reused. This is not
universal multilingual semantic recall or a newly generated abstract biography.
Live history coverage and live account completion have not been measured here.

## Changed files and handoff

All changed files are new, within the assigned ownership paths:

```text
service/onlyfans_runtime/launch_history_memory/__init__.py
service/onlyfans_runtime/launch_history_memory/extraction.py
service/onlyfans_runtime/launch_history_memory/models.py
service/onlyfans_runtime/launch_history_memory/memory.py
service/onlyfans_runtime/launch_history_memory/recovery.py
service/onlyfans_runtime/launch_history_memory/recent.py
service/onlyfans_runtime/launch_history_memory/schema_contract.py
service/onlyfans_runtime/launch_history_memory/INTEGRATION.md
service/onlyfans_runtime/launch_history_memory/VALIDATION.md
service/tests/test_onlyfans_launch_history_memory.py
service/tests/test_onlyfans_launch_history_memory_pg.py
service/tests/test_onlyfans_launch_history_memory_recovery_pg.py
service/tests/test_onlyfans_launch_history_memory_recent_pg.py
service/tests/test_onlyfans_launch_history_memory_planning_pg.py
```

`INTEGRATION.md` specifies the progress, coverage, memory-read, correction and
backfill contracts, and exact shared schema/worker/store/planning/memory/API/UI
changes for Claude. It also explains the pinned implementation's incomplete
recovery and why old messages could invalidate current work. No shared file or
dashboard file was edited. This is a contribution to the combined checkpoint;
it introduces no separate approval stage.
