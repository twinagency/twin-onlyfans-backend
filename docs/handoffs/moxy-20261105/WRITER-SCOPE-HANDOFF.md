# Amelia conversation rule scope repair

Recorded 2026-11-05T01:56:52.051135+00:00

Commit `a2bb80115750079888e86e2a0de982334134a971` is in
`/srv/projects/moxy-rule-scope-20261105`.

The writer now selects a single conversation instruction branch from the same
resolved permission included in the model payload. This removes ambiguous
restriction instructions from prompts where conversation permission is allowed.
It distinguishes an imagined interaction from an actual meeting, commissioned
order or physical fulfilment. It also explains that media restrictions govern
media and that historical outgoing claims cannot establish a creator boundary
without support in current rules or approved public facts.

The code change is confined to `service/moxy_replacement/writer.py`. Six
parameterized regression cases were added to
`service/tests/test_moxy_permission_and_retry_context.py`. They exercise the
actual writer payload under allowed, denied and operator-amended permissions,
verify the selected branch against permission resolution, and verify snapshot,
history and current rule preservation.

## Validation

78 unique focused checks passed. Individual run counts overlap. See
`TEST-RESULTS.txt` for captured terminal summaries and initial fixture setup
failures, and `VERIFICATION.json` for structured results.

The final PostgreSQL run passed all 47 cases in 169.70 seconds, including
20 database cases. The test-owned container
`moxy-reply-repair-fixed-pg-20261104` was returned to its original stopped state.
Fixtures created and dropped isolated UUID test databases.

Root reported candidate-image checks for all three exact recorded incident
inputs. Each projected the allowed branch and preserved the input snapshot.
These checks made zero model calls.

## Release and scope

Candidate image
`sha256:772b51737c1242f6b803e0b13831ed70d07e08d9a363415b9ee9a12a1f309475`

Verified production base supplied by root
`sha256:5d57a6dab8b37caeee5f614ae00b20e16f82a6382075d8f8c565c52896fa56c3`

Production host release directory
`/var/lib/twin-release/moxy-writer-scope-a2bb801`

Root reported successful production deployment and technical verification using
SSM command `7001e358-2901-4da1-8e42-25e2c0b3d630`. The running image matches the
candidate above. Amelia remains mode on at revision 14 with only fan 318022380,
20 products and no pending intents. Root verified the exact settings hash was
unchanged, along with pilot.py, runtime.py and text_delivery.py hashes and the API
container/image.

The host receipt is
`/var/lib/twin-release/moxy-writer-scope-a2bb801/DEPLOYMENT-VERIFIED.json`.
The sibling `rollback.sh` restores the previous compose chain and base image
`5d57a6dab8b37caeee5f614ae00b20e16f82a6382075d8f8c565c52896fa56c3`.

This deployment metadata was supplied by root and has not been independently
retrieved by the reviewer. Root completed startup verification at
2026-11-05T01:58:22.561697+00:00 with SSM command
`fda7b4e5-4ca0-4674-aa4a-8640fc0a7f3b`. The worker was running with zero restarts,
the expected image, zero tracebacks and zero error lines across 33 startup log
lines. The receipt is `STARTUP-VERIFIED.json` in the host release directory.
No new actual-model reply has been evaluated.

## Evidence limits

No new actual-model behavior proof was generated for this change. Recorded-input
prompt projection proves instruction wiring and scope. It does not establish that
future model replies will consistently follow those instructions.

This change adds no runtime semantic boundary detector. An unsupported refusal
can still escape deterministic validation. Do not describe the repair as a
guarantee of model wording or conversational quality.

This evidence was prepared by the read-only/test reviewer. Production changes,
final image verification and rollback are owned by root.
