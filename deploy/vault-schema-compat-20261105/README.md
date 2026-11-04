# Vault description schema compatibility

The dedicated description workers were built for schema 0100_manual_free_media. They stayed running while the database advanced to 0111_moxy_owner, then correctly refused startup after the vault repair restart. The failure was `Database revision is not supported by this process`.

This overlay copies the complete current schema contract, model definitions, migration graph, snapshots and required model-registration dependencies from the already validated main worker image. It preserves the legacy database engine, v3 description verification, task-local model authority, photo concurrency and worker account settings. It performs no database migration, revision stamping or weakened compatibility check.

Before rebuilding, verify both input image IDs against manifest.json. Build this Dockerfile only from those retained inputs. The manifest identifies every changed source file and its SHA-256; unchanged copied files remain those already in the pinned base image. The accompanying source changes preserve the same dependency set in Git.

Validation completed before rollout:

- Schema metadata matched the main image exactly: 166 tables, including column types/nullability, keys, indexes, constraints and version metadata.
- Full current `check_schema` passed against production with `transaction_read_only=on` explicitly asserted.
- Expected and observed schema revisions both were 0111_moxy_owner.
- Candidate imports passed with networking disabled.

The manifest records command IDs and immutable image IDs. Separate isolated PostgreSQL regression validation is tracked in the release evidence; no live database schema was changed.
