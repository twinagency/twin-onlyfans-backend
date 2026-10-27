# Maintenance command packet — BLOCKED BY LIVE SCHEMA DRIFT

DO NOT EXECUTE against current production. The verified source is now 0093_conversation_default_off, not 0092_ledger_history. The commands below are the previously prepared 0092 procedure and are retained only for review. Reconcile source/schema compatibility first; never relabel or stamp the live database. See FINAL-RELEASE-RECORD-20261028.md.

Preparation only. Execute after the agreed Astra final review, final acceptance and private target/key verification. Application source is 1c2030eb5fb1a3af7df166019a771dd8f5528f2a; dashboard is 4599999761dbf2ff2b517359642d10262324867a.

Selected immutable forward images:

- Backend API/worker/sender/broker: sha256:7578467ec1c0f216983b9b1c763284d5b86cd8a74cb2a9e23d45a1bebca6ba2c
- Sign-in: sha256:f1dc62a4d44142648e1ed128de3e6797f61081fbf9ef0bd3770e79edd47bbc9c
- Dashboard: sha256:98a82df874d61b744d11bf0e8635466f19a3e814abfb9d666c190a9b46f865d0

Keep the current complete private role configuration, credentials and mounts; change only the reviewed image references and explicit outbound fences. The raw inspection/environment recovery records contain secrets and stay in the private release directory, never in this repository.

Close admission by stopping the API/control entrypoint and dashboard, then drain or fence sender, worker, sign-in and broker. Capture and disable restart policies before stopping them. Verify there are no late sign-in completions or other writers against the verified CRM target. Do not stop unrelated Telegram/Instagram/Flirt workloads. Do not restart a sender from a rollback.

After all writers are confirmed stopped, the private env file must hold the verified source and empty isolated restore target, retained backup encryption key and existing proxy-encryption key. It is not created or populated by this packet. The source must genuinely be 0092_ledger_history.

```sh
install -d -m 700 /var/lib/twin-release/final-stabilisation-20261028/backups
docker run --rm --network twin-onlyfans_default --workdir /app/service --env-file /secure/final-crm-recovery.env -e PYTHON_DOTENV_DISABLED=1 --mount type=bind,source=/var/lib/twin-release/final-stabilisation-20261028/backups,target=/backup --entrypoint python sha256:6487b89c9ac5d6081701cd6bf5430ba3b6c5c79a659ce6de0c6234991955c1dc -m onlyfans_runtime.operations backup /backup/twin-before-0093.twinof --include-proxy --source-revision 0092_ledger_history --pg-bin /opt/pg16
```

The backup command now names the isolated immutable recovery-tools image tested in 17 cases, with matching PostgreSQL 16.15 wrappers at /opt/pg16. Its source-revision allowlist still excludes the newly deployed divergent 0093 source, so this command must refuse current production. Do not install packages into a live container or bypass the revision check. Retain backup metadata and SHA256 privately. Restore only into the verified empty isolated destination, with the same key:

```sh
PYTHONPATH=service python -m onlyfans_runtime.operations restore /private/backups/twin-before-0093.twinof --url-env ONLYFANS_RESTORE_TEST_URL --pg-bin /opt/pg16
```

Require authenticated restoration, public+onlyfans_proxy row preservation, no active Alembic version and the independent quarantine fence; both runtime and broker readiness must refuse the restored destination. Retain separate owner/agency session-store recovery material before applying session SQL.

Run the bounded statistics and explicit migration chain from the selected candidate with ONLYFANS_MIGRATION_URL loaded privately. The following is the complete SQL/API sequence for review; no URL or key is supplied on argv:

```python
import os
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from alembic import command
from db.schema import _config, revision, check_schema
url = make_url(os.environ["ONLYFANS_MIGRATION_URL"]).set(drivername="postgresql+psycopg2")
assert url.get_backend_name() == "postgresql"
engine = create_engine(url, pool_size=1, max_overflow=0, connect_args={"options":"-c timezone=UTC -c lock_timeout=5000 -c statement_timeout=30000"})
try:
    with engine.begin() as conn:
        assert revision(conn) == "0092_ledger_history"
        check_schema(conn, expected="0092_ledger_history")
        for table in ["messages", "of_content_sync_source", "of_runtime", "of_content_sync_account", "of_offer_import_job"]:
            conn.execute(text("ANALYZE public." + table))
    previous = "0092_ledger_history"
    for target in ["0093_ledger_markers", "0094_purchase_reply", "0095_content_fold_pending", "0096_vault_continuation"]:
        with engine.begin() as conn:
            assert revision(conn) == previous
            check_schema(conn, expected=previous)
            command.upgrade(_config(conn), target)
            check_schema(conn, expected=target)
        previous = target
    with engine.begin() as conn:
        check_schema(conn)  # includes final pending-source protection
finally:
    engine.dispose()
```

Any statistics or migration failure stops this sequence with writers still fenced. Do not raise deadlines, hand-stamp or replay unknown attempts.

Broker schema is upgraded through the existing explicit entrypoint, which initializes and returns without starting its server or inventory loop:

```sh
PYTHONPATH=service python -m managed_proxy.api --init-schema
```

Use the verified private broker database URL, existing connection key and a distinct management token in the management-only environment. Do not grant that credential to creator/public code.

Apply dashboard deployment/dashboard-session-revocation-upgrade.sql from dashboard 4599999761dbf2ff2b517359642d10262324867a using the verified private SQL connection to Twin/Moxy project aouaenqayfltoqrmpitg, with ON_ERROR_STOP. The Flirt project lkmfwpqegjluirfusnaw is a different target. Keep legacy HMAC compatibility off and require admin re-login.

Start reviewed images with outgoing automation/campaign admission fenced. Verify live hashes/configuration, permit readiness ledger/import processing, then enforce current binding/connection coverage, fresh lastCompleteAt within 15 minutes, completed traversal and no blocking conflicts before paid sending per intended account.

Execute the existing controlled-provider packet through the persistent dashboard-managed sender only, with fresh actor/revisions/model/proxy/budget/media/price. At most one manual text, one approved paid card and one reply to genuine fresh controlled input. A missing fresh input or uncertain receipt is a stop, not permission to fabricate/retry.

The retained tested full-release rollback is schema-0096-compatible backend sha256:49e446b89c828a38212c673d9710b011506fe722724a2955bedf4424ddbb5d3a in restricted read mode, with all writers stopped. Preserve all newer receipts/counters/unknown attempts. The caption-only 0092 image is not a 0096 rollback.
