# Isolated build status

Recorded: 2026-11-07 00:56:46 UTC. Work Server host: `ubuntu-s-4vcpu-8gb-syd1`.
Project: `/srv/projects/twin-conversation-engine-20261107`.

## Baselines and changes

- Worker baseline `fa7f1387267b43fafc1b34a8c44690c4ac579c2d`; isolated backend branch `codex/twin-conversation-engine-20261107`, head `85e650175942f5a89a957bfdc18d6fe450294ed5`.
- Dashboard baseline `a39e34a524b74bdf3bb2e5571453e56401796864`; isolated dashboard branch `codex/twin-conversation-engine-20261107`, head `95a2cf3ad87bb0d89f4fc256847efa9b08eedd33`.
- Separate vault input `91be062064b3071fea1a11509ed90d81ecb0075e`; no vault merge has been made. No combined release or production parity is claimed.
- Specification SHA-256 `5364224b0fd63d36ae53c479e0691a48e89e929db621a02bc1af3cd3f044e155`. Handoff SHA-256 `aa1b2643ba2a93dcf62bc7c37deae11a46046c46ecd7103909fd8a8a51f6e8f2`. The only task documents copied here are the two supplied files.
- The original worker remains clean; the original dashboard still has its prior `artifacts/`; the original vault candidate retains its prior 21 untracked entries. The isolated checkouts are clean at their new commits.

Backend files added or changed relative to the baseline:
- `service/conversation_engine/__init__.py`
- `service/conversation_engine/evidence.py`
- `service/conversation_engine/identity.py`
- `service/conversation_engine/recovery.py`
- `service/conversation_engine/sales.py`
- `service/fans_api_adapter/__init__.py`
- `service/fans_api_adapter/history.py`
- `service/fans_api_adapter/send_outcome.py`
- `service/fans_api_adapter/webhooks.py`
- `service/tests/conversation_engine/test_facade_contract.py`
- `service/tests/conversation_engine/test_fans_api_contract.py`
- `service/tests/conversation_engine/test_foundations.py`
- `service/tests/conversation_engine/test_import_boundary.py`

Dashboard files added relative to the baseline:
- `tests/contracts/facade.test.mjs`
- `tests/contracts/fixtures/manual-queued.json`
- `tests/contracts/resolve-typescript.mjs`

## What is implemented

- Separate immutable conversation/action identifiers, including tenant, account, creator, fan, provider, binding epoch, action, key and payload hash.
- A conservative, pure recovery decision: incomplete history, unknowns, conflicts, mismatched identity and distinct receipts do not become automatic retries; a proven not-sent history still requires an externally approved retry budget and the same action identity.
- Exact-scope absence coverage, including provider, evidence kind, subject, item version, interval, freshness and an explicitly supplied source-capability decision. No source authority registry or persistence is implemented yet.
- Pure sales pause and explicit item-scoped reopening. No natural-language classifier, durable storage, purchase cancellation or sending is attached.
- Synthetic-only Fans API parsing of documented send errors, raw-body HMAC signature verification, and continuation validation for the exact account/chat on the approved host. No HTTP client, webhook acknowledgement, durable deduplication, success acceptance or real provider qualification is implemented.
- A dashboard route inventory and synthetic contract tests for scoped reads, schema versions, queued acknowledgements, refusal and unconfirmed write outcomes. The route matrix is `contracts/dashboard-route-matrix.md`; its 40 entries are inventory, not full route/browser qualification.

## Verification

- `offline-runner-with-postgres.log`: 18 Python tests and 7 dashboard contract tests passed, plus 2 selected existing PostgreSQL tests passed. The PostgreSQL cases exercised a populated baseline upgrade retaining an unknown delivery and a receipt-projection crash repaired without a second synthetic wire call. The new primitives are not connected to those existing paths.
- `baseline-postgres-migration-capped.log`: existing synthetic PostgreSQL binding migration upgraded, rolled back, upgraded again and checked scoped binding constraints.
- `vault-editor-reader-baseline.log`: **one reproduced baseline failure** in `test_operator_correction_beats_model_description_in_prompt`: after the human description edit, the reader reports `pending`, not `described`. This remains an integration/release blocker and has not been fixed.
- `work-server-inventory.txt`: read-only service/process/container snapshot, capacity, lab isolation, and the hashes of the two supplied documents. Existing PostgreSQL containers were not used. No local Telegram process/service was observed; locations elsewhere remain unverified.
- Node used a test-only built-in TypeScript stripping loader; the normal dashboard build, browser suite and complete route authorization matrix have not run.

Run the no-network suites with:

```sh
ssh work-server '/srv/projects/twin-conversation-engine-20261107/evidence/run-offline-tests.sh'
```

For the two PostgreSQL cases, start the **dedicated disposable** `twin-engine-lab-pg-capped-20261107` container and wait for `pg_isready`, then run the same script with `--postgres`. Its data is on a 256 MiB tmpfs and is recreated on container restart; it contains synthetic fixtures only. The earlier disk-backed lab container is stopped and its synthetic files were left intact. Neither database is a production resource.

## Isolation and provenance

The controlled test image is `moxy-current-base:verified-e5f092544145`, ID `sha256:e5f09254414516aea9b85f0c8085050733a7161a5bb5f5fd179d774e19708e17`; the selected source is mounted read-only. Test-only pytest dependencies were copied into a separate 14 MiB project directory from a local environment and mounted read-only. The Python, Node and PostgreSQL runs use no provider credentials; network is `none`, or the network namespace of the no-network lab PostgreSQL. No host ports are published. PostgreSQL trust authentication is confined to that isolated synthetic namespace. One test runner at a time is limited to 0.75 CPU, 768 MiB for unit tests or 1 GiB for PostgreSQL tests; PostgreSQL is limited to 0.5 CPU, 512 MiB, 64 processes and a 256 MiB data tmpfs. The baseline host had 4 CPUs, approximately 6.9 GiB available RAM and 139 GiB available disk. Headroom is a point-in-time observation, not a guarantee.

Provider fixtures are synthetic and based on these public vendor pages, not on account responses: https://docs.onlyfansapi.com/introduction/essentials/idempotency ; https://docs.onlyfansapi.com/webhooks/protecting-your-webhooks ; https://docs.onlyfansapi.com/api-reference/chat-messages/list-chat-messages . No metered provider/model call, paid purchase, message, production change, deployment or activation was made.

## Gates and next work

Continue independent offline contract and state work. Do not treat this as a working conversation engine or release candidate.

1. Complete Stage 1 dashboard route/permission and unavailable-state tests; browser and non-OnlyFans regression checks remain outstanding.
2. Decide the combined schema lineage: the worker expects schema 0111 while the separate vault candidate adds 0112. Do not select a new migration revision or claim image compatibility until integration is designed and tested. Durable requests, sales permissions, coverage, and owner epochs remain unimplemented.
3. Resolve the reproduced vault editor/description binding failure during the combined integration without auto-approving changed media. Preserve the candidate's window and version evidence.
4. Extract one mechanical delivery authority and fence all competing send paths before any new engine can submit. The existing shared authority still contains legacy policy; these new primitives are not wired into it.
5. Obtain platform permission, the exact supported model/host and privacy terms, current runtime/external-writer identities, purchase-to-media evidence coverage, provider budget/access, creator boundaries and approved operational rules. These block their dependent work and live qualification, not unrelated fixture development. The full requested rules 2 and 12 remain unverified; no explicit-content generation or bypass was implemented.
6. Real provider/model calls, purchases, live messages, deployment, activation and migration of live state require the separately specified user approvals. Moxy/Moxie two-creator development remains paused.
