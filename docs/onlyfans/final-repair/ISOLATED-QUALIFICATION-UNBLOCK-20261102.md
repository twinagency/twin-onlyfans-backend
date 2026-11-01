# Isolated qualification: concrete blocked actions

This is an implementation/test resource request, not release or live-send approval. Candidate code is `546518171972784264908344ba5f9d0c6ea49993`, tree `4a8326b3aa8b9f4c67609b5f2c3704128c468f28`, on `codex/of-original-completion-20261102`.

## A. Private Linux test transfer

Prepared on the authorised laptop under `C:\Users\conta\codex-work\of-original-qualification-20261102`:

| Archive | Bytes | SHA-256 |
|---|---:|---|
| `qualification-source-546518171972784264908344ba5f9d0c6ea49993.zip` | 4,831,155 | `723148637ff7d63f6f5941ebe27baea1c5c4ce2ddc1b440fc5aac1d344315b80` |
| `qualification-references-5465181.zip` | 32,505,500 | `76b6d269cf41218979d0a3250b1433c01e0724757897e4634c8f76425f46ca01` |

The first contains 1,008 tracked service/scripts/web/dependency files. The second contains the exact released backend `8a427a3` (585 files), paired dashboard `ce99d8a` (1,642 files), and preceding read-only recovery source `fceb624` (1,007 files). All source blobs were verified against their authoritative Git hashes. Configuration examples are included; active environment files and the private transcript/evaluation files are excluded.

Proposed destination: `s3://twin-worker-transfer-38e051d1/original-qualification-20261102/`, AWS account `323149985112`. Use private server-side-encrypted objects; public-access blocks were verified. No upload has occurred.

After transfer, use task-owned temporary directories and resource-limited test containers on `i-09a9dd3bd749643a5`. Recheck available resources first. Test containers must have their own disposable PostgreSQL, synthetic credentials/accounts, no production mounts or published host ports, and an isolated network. The inspected Python test image is `sha256:458659b6d160219cce93b45c227a63e802f38e2c5e4c832eb57e087710537bd3` with Python 3.12.16/pytest 8.3.5. Verify exact requirements, Node and PostgreSQL clients before accepting results. Keep measured-load runs separate from competing qualification jobs.

GitHub's registered manual CI entry was also tried: run `41346876191` resolved the reusable test workflow but ended `startup_failure` with zero jobs. Local Linux has no PostgreSQL binaries and cannot switch to the available nonroot uid. Windows testing continues, but cannot provide the Unix-specific checks.

Automatic approval review rejected the earlier private archive upload because the payload/destination lacked explicit authorisation. The requested action is explicit approval for this private qualification transfer and isolated test execution. It does not authorise deployment, live database access by tests, activation, settings changes or customer messages.

## B. Five unpublished source files for preservation

Read-only hashes identified 24 worker overlays after line-ending normalisation. Nineteen exact source blobs were recovered from the authorised repository. Both raw and normalised blob lookups failed for these five:

| Path under `service/` | Normalised Git blob SHA |
|---|---|
| `native_pilot/budget.py` | `6e4618d1f2cb08abf73a2b7eea7c3ca08ac1a13d` |
| `onlyfans_read/earnings.py` | `27cb1d895a1650dae67993ce22b9c162abf6b4f5` |
| `onlyfans_runtime/profile_learning/evidence.py` | `b56b9dac61d523b53f39cf93f36b45da0a57b9cf` |
| `onlyfans_runtime/profile_learning/worker.py` | `44f2d24e74adf31e5013013b6a7e9be09127251f` |
| `onlyfans_worker.py` | `efe744bb11c737bb33b2a2071401a3fbb2d5e324` |

The smallest alternative is for their owner to commit the exact source to the authorised repository. Otherwise, approve a read-only copy of only these five files from the observed worker into the existing laptop qualification workspace, checking their captured hashes. Do not copy environment files, secrets, databases, logs or customer records. Review the code before integrating it; do not overwrite newer worker work.

Automatic approval review rejected the broader production-source copy as sensitive egress beyond the authorised version/schema/settings audit. No production source contents were copied by that rejected action. This remaining source gap prevents a reproducible deployed baseline and a defensible migration/rollback plan for live `0106_parallel_history_reads`.

## Other missing resources

* The authoritative Twin sibling revision/archive containing `src/config.ts`, `src/channels/{identity,http,store}.ts` and its matching lockfile. The restored bridge helper is real; no replacement adapter was invented. The 63 current sibling branch trees and inspected deployed source lack those channel files. Historical path queries against all 63 branch heads also returned no matching store-file commits, without lookup errors. This blocks the two real bridge modules and complete sender integration evidence.
* A configured test credential for the existing OpenRouter/DeepSeek route, restricted to the existing $2 provider-side test budget. Configure it securely; do not paste it into chat. The shared journal and pre-request reservation tests pass. Zero actual-model calls have occurred.
* Approved creator/configuration and operational world-state evidence for the original cases: product/assets, quotes/currency, purchases, delivery state and holds. The revised 200-case source draft preserves full prior context and the named incidents, but historical statements are not approved facts or catalogue authority. The supplied consolidated execution brief and older detailed plan have been recovered; the separately referenced private case/world-state file remains unavailable.

The original R9 account/fan/asset/preview/amount/currency tuples, staffed handover owner and separate live-delivery/unlock/pilot permissions remain later gates. They are not requested by this test-resource packet.
