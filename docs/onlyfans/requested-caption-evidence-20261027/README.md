# Requested-media caption correction: evidence

This is an evidence branch, not the runtime deployment source.

- Backend: `98923531d0531944b8dfe5a28d24f03f2718f170`.
- Dashboard: `444fd1303858e6d87204efe5811278657571f8f6`, unchanged.
- Parent backend: `12c7fed4d15d902e999a4c8b9e45823c3f07d9ab`; its backup repair is retained.
- [Implementation and exact scope](https://github.com/twinagency/twin-onlyfans-backend/blob/98923531d0531944b8dfe5a28d24f03f2718f170/docs/onlyfans/requested-caption-repair.md).
- [Hosted release verification](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/40497360991).

## What passed

| Check | Result on this exact backend |
| --- | --- |
| Caption plus backup/recovery boundary | 63 passed, zero failures/errors/skips, including all 21 new caption cases |
| Full native SQLite suite | 29 commands, every exit status zero, zero skips |
| Full native PostgreSQL suite | 7 commands, every exit status zero, zero skips |
| Retained immutable rollback image rehearsal | 4 passed, zero failures/errors/skips |
| Required isolated read/migration scripts | 2 commands, all cases passed, zero skips |
| New immutable backend forward image | Built and retained; exact identifiers in `forward-image/metadata.json` |

These are separate checks with overlapping coverage, not a combined test total.
JUnit files, exact native command results, logs and source pins are preserved
beside this document. Local source hashes match the seven committed files.
The new caption assertion fails against the unmodified parent, as recorded in
`baseline-reproducer.json` and its log. The hosted suites are the final clean
evidence; a local launcher initially omitted CI's PYTHONPATH and its affected
existing test passed after correcting that launcher without application changes.

The new cases include a complete native-engine turn and the actual pack planning
and transport path. Conversation/voice and selected-media descriptions are kept
separate. Count, mixed-media type, price, attachment order, audience and final
media-liveness checks remain effective. Model failure, incomplete descriptions
and unusable output fall back without a duplicate delivery. Hook, substitute and
unnamed-request caption paths retain their existing behaviour. Saved persona,
voice and sales settings are unchanged.

## Remaining release gates

All six paired acceptance shards were attempted and refused before collection:
CI has neither the exact private dashboard archive capability nor the explicitly
provisioned cross-repository Contents-read token. The aggregate checker ran and
rejected the missing shard evidence. The overall workflow is therefore RED.
Do not describe this candidate as having passed full paired acceptance.

Both required isolated scripts passed separately against the exact runtime
commit in [run 40498889651](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/40498889651):
`service/tests/test_onlyfans_read.py` and `service/tests/test_onlyfans_migration_pg.py`.
Their commands and full logs are in `isolated/`. They are not credited as
successful paired shards and do not replace or weaken the aggregate gate.

The previously authorised private AWS source handoff can be reused when its
authenticated execution route is available. No source-proof checks were removed.
PR #33 remains at the previously reviewed head until the corrected pair has its
required release evidence. No production deployment, migration, fan message,
real model call, allowance reset, audience expansion or saved setting change was
performed for this correction.

Controlled real-model verification is still required to assess natural wording
and compare factual claims with the actual selected media. The synthetic tests
prove data flow and deterministic checks; they do not prove real-model quality
or the accuracy of stored descriptions. Outstanding recipient confirmation and
outbound authorisation are still required before real messages/model calls.
Deployment additionally requires complete paired acceptance, fresh production
fingerprints, the existing backup/writer/migration sequence and explicit release
authorisation. Serial-engine capacity and deferred AUTH-02/03 Flirt work remain
documented limitations outside this change.

Historical evidence is preserved: 2,182 cases belong to the original pair;
2,260 belong to backend `0c4d79a` / dashboard `444fd13`; the earlier backup
proofs belong to `12c7fed4`. None is reassigned to `98923531`.
