# Recovery for the identified Plan C monitor incident

This is the existing pilot supervisor/worker owner's manually invoked recovery.
Only these independently identified incidents are supported, for account
of-323358988, binding01612323-b06f-5320-b623-4e032f474a6e and connection epoch1:
- c59fd2459671cc3e0d3a82a1a8de26b9675cc24a4e0567e3f712ea3b89016725:
  credit_monitor_unavailable, stopped scope12.
- c240c781574ad075939782179ab926c9f0a69cc3fc40a292f81f7d3f57e3ad14:
  provider_webhook_health_unavailable, stopped scope16.
It does not activate, publish a sender permit, release application holds or
settle/refund costs. Every unlisted incident stays held for its own decision.

The collector reads the exact dedicated webhook's supported complete bounded
delivery listing, current subscription and usage, and retains source bytes.
Full local receipts are compared by their retained SHA and parsed payload.
A single non-holding digest may represent a positively classified fan outside
the two-fan pilot; its original digest and the inability to reproduce its exact
raw bytes from the provider's logged object are retained explicitly. Unknown
recipients, conflicts, failures, missing relevant evidence or truncated provider
intervals refuse recovery. This is not a full event-completeness claim.

Before installation obtain independent review of the final revision and the
actual interval's scope findings, including those digest limitations. Keep
runtime/policy disabled and sender stopped. Preserve original v11 scripts before
atomically installing the four compatible guard/writer files and incident_state.
Do not remove or rename the unresolved marker. Review the exact candidate image
and write the root-owned supervision-v11/reviewed-recovery-release.json with
{"image":"sha256:<reviewed image>","commit":"<reviewed full commit>"}.
This is a release pin, not a replacement for independent review.

Run resolve_monitor_incident.py under the existing restricted host credential
owner. It collects fresh evidence, invokes the exact image's existing DB audit
owner, preserves the credit high-water value and only then publishes the
incident-specific resolution receipt. Original incident bytes and append-only
resolution records remain. Guard acceptance requires both immutable records.
An unresolved=true/false edit alone grants nothing. A new failure atomically
records a distinct held marker; resolver and failure writer share a lock.

If the process crashes after audit but before publication, inspect the retained
attempt, then explicitly run:
  resolve_monitor_incident.py --resume /srv/projects/twin-plan-c-pilot-20261109/evidence/<exact-attempt>
The DB command is idempotent; stale unaudited evidence is refused. A repeat after
successful publication makes no new provider call. No automatic retries exist.

While the incident is held and sending is stopped, switch to the final compatible
receiver, preserving/recording the exact handover interval and checking supported
delivery records afterward. Update the existing emergency wrapper and guarded
sender image pins. Resolve only after proof and review. Start the supervisor in
stopped mode and verify fresh checks before the separate authorized activation.
Recheck native writer fencing, current fan takeovers, all application holds,
populated backup/compatible rollback and both-fan history. Paid cards and the
account-wide financial import remain disabled. The monitored total provider
allowance and lower existing stop threshold, and AI cap/reservations are unchanged.

Tests distinguish mocked provider/Docker dependencies, real isolated PostgreSQL
audit transactions and actual read-only provider evidence. No test result
authorizes live sending by itself.

The root resolution owner records an immutable marker-access audit tied to the
DB command and original hash, then grants only group65532 read on that verified
marker (root owner, mode0640). Bytes remain unchanged, and the worker cannot
write marker/archive/receipt/gate. Every recorded failure creates distinct
marker bytes under the same lock even when an older incident remains open.
Thus old collected evidence and explicit crash-resume refuse any new failure.

The existing receive-only installation uses receive_only.py explicitly. It
validates worker/tenant/account/durable binding and the protected secret, then
constructs only the existing IntakeOnlyPilot and loopback create_app server.
It never delegates to the active factory, even after an active-scope restart or
model-related environment changes. No model or provider credential is required.
The installation's existing worker.sh can embed these exact reviewed bytes in
its Python heredoc, preserving its current file mount and intake continuity.
Record the entrypoint and wrapper hashes and preserve the prior wrapper. Any
embedded copy must equal this file; it is not a separate implementation.

For the second approved incident, retain the complete monitoring-period listing
from the existing webhook-enable evidence and separately record the reported
18:54+ incident window. A known older signed receipt anchors the full listing;
it is explicitly not a fresh post-incident delivery. Every reported record in
the narrower window must match the retained full listing, including payload and
status. An empty narrow window cannot erase the required full-window anchor.
Unmatched, incomplete, wrong-case or changed evidence refuses recovery. The
original network subcause was not retained; no timeout change is implied.

A second approved resolution preserves the first immutable incident and audit
receipt; only the current pointer is replaced after the new DB audit commits.
Missing prior history, a subsequent failure or a class/revision/hash mix-up
stays held. Both cases use the same existing audited owner and actor checks.

The third explicitly approved case is
 df20805ad9525b3ccc54b5dba161940cbdca78bda053d916d0578bbb42a27f33,
 provider_webhook_health_unavailable, CURRENT stopped scope20 (the intervening
 ten-text cap configuration is authorized; stale stopped18 is refused).
Use the full monitoring-period and separate incident listing as for case2.
The original 20:13 exception was not retained. A separate read-only reproduction
at 20:33 captured TimeoutError opening the delivery-log request at6.013s;
a neighboring successful response took6.363s. This does not establish the
historical exception. Only delivery-log GETs now use12s socket/request timeout;
all others remain6s. No retry or moving window is introduced. A complete check
older than30 monotonic seconds fails before STATE publication. The existing10s
cadence,45s wire lease,90s watchdog/start timeout and30s start grace are unchanged.
A third recovery preserves BOTH prior immutable histories and does not activate.

Scope-aware sender readiness and the guard (U3). Guard decision (2026-11-13): the host guard
supervision-v11/guard_core.py, sha256 c2be833f7191bf5a94f3c1f841e09effac62cf5a0b03b751663545e9af905797 (early stop 300,
Jesse 2026-11-10; previous 5aef81d2 = 250), is retained byte-for-byte as service/tests/fixtures/host_guard_core_c2be833f.py.
The release-owned guard_core.py (sha256 8a06770e214e7b69847ebb0f65f0b2481c4467fd1102a9084a98489ebd11e04b) differs only by
the optional credit_limit=300 keyword credit_estimate gained in a815e8e so each account can carry its reviewed early stop
(validated: malformed or non-positive limits refuse). GuardFailure, require_health and require_fresh are AST-identical and
the default behaviour matches the host guard over a grid; service/tests/test_plan_c_guard_compat.py proves this and
service/tests/test_plan_c_u3_readiness_pg.py pins the release-owned bytes. The host copy is never edited independently.
guard_core.py is the release-owned guard imported directly by the supervisor and its tests; it is not an install source.
The guard still requires the sender /readyz to report
ok=true, mode=active, code=ready and sendingReady=true. What changed is the worker's definition of
sendingReady (service/plan_c_worker.py): it is false for loop failure, stale loop, refused scope or
budget state, and for an ACCOUNT-WIDE evidence hold (unknown, malformed, conflicting, unbound-account
or over-bound unresolved digests). A hold the retained exact bytes prove belongs to one fan (a signed
material event such as tips.received) removes only that fan from work.eligibleFans (work.heldFans
counts it) and no longer turns the other fan's sender permission off. Both fans held reports
sendingReady=true, eligibleFans=0, heldFans=2 and sends nothing; it is not a health failure.
work.malformedHolds and workHeld still count every unresolved digest. Deploy this worker revision
together with the existing guard; no guard/hash/manifest change is required, and rolling the worker
image back restores the previous (stricter, fan-hold-denies-all) readiness with the same guard.

## Commercial multi-account configuration (offline candidate)

Each supervisor process must receive TWIN_SUPERVISOR_CONFIG, a private JSON
file accepted by operation_config.validate. No running process falls back to
a live legacy account, scope id, webhook id, URL or database name.

Required fields: version (twin-supervision.v1), root, tenant, account, binding,
epoch, providerAccount, webhookId, publicUrl, receiverUrl, senderUrl,
databaseContainer, databaseRole, databaseName, supervisorUnit, senderUnit,
senderContainer, teamAccounts, creditStop, initialExternalReserved, reservations.
Each reservation names a relative evidence path, integer amount, boolean optional.

Use an account-specific root, credential-reference file, state, checkpoint,
sender identity, ports and unit names. The existing filenames under root
are retained; no state is migrated by startup. The existing budget-state file
must match account_id, binding_id and connection_epoch. A wrong root or a prior
account's state refuses startup.

teamAccounts explicitly lists up to ten accounts billed to the same reviewed
provider team. The read-only supervisor query totals their existing durable
scope reservations while preserving the selected account's own counter.
These are conservative reservation estimates, separate from provider billing.
The reviewed early-stop threshold is explicit creditStop. This monitoring
configuration does not replace the final application admission checks.

Root-owned operational tests must run as root inside an offline, read-only
container with only temporary writable storage. Application tests retain
their unprivileged runtime. Do not mock away ownership and chmod checks.

The commercial audience contract remains at most 100 explicitly listed fans
per creator. The vault selection remains at most 1,000 assets; old ownership
history is retained separately. A2 unknown-text retirement remains disabled.

Shared request admission also requires TWIN_PROVIDER_TEAM_LIMITS_FILE in every
real provider process. Use the same read-only provider-team.v1 JSON for every
member: team, accounts (the explicit at-most-ten account IDs), creditLimit and
externalReserved. Existing per-account scope counters are summed under one
PostgreSQL transaction lock before each preparation, active or stopped-backfill
HTTP attempt. No separate usage ledger exists. Keep the reviewed team membership
and baseline unchanged during qualification or a run; replacing membership or
reducing the external baseline is a new budget authorization. Fixture transports
may omit this file. Missing/invalid real configuration refuses provider requests,
activation and planning. The monitor remains an additional detection boundary,
not a substitute for transaction-level admission.
