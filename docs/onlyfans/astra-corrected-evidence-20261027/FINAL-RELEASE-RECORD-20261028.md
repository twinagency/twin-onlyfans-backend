# Final Twin CRM release record for Astra

Preparation in progress. This record does not certify completion of the full release.

Backend source: 1c2030eb5fb1a3af7df166019a771dd8f5528f2a. Dashboard source: 4599999761dbf2ff2b517359642d10262324867a. Backend PR #33 and dashboard PR #306 contain the consolidated approved work. The final paired source archive SHA256 is 1accd652bcca3c274b569ac72ac6ec6910dd20508d477d665527fb6bced31c60. The exact final acceptance run is [40541003637](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/40541003637); immutable artifacts are [40541668140](https://github.com/twinagency/twin-onlyfans-backend/actions/runs/40541668140). These results must be harvested before a green release decision.

The completed caption-only release remains live: source 3ac035bddf0f7cd29285d7ad5d194b902def1f13, application correction 8d89d1ffedc860648424c41707e89908497d3bc5, image sha256:80c488cc3fb9dd974d8c743c36be0cbb627468af6192b289d76723a9c2fd9b7c. The latest check verified all 656 Python files, running sender and restart count zero. Source/configuration hashes for every current backend role and the compiled live dashboard are in final-production-fingerprint-20261028.json. Schema remains 0092_ledger_history; one existing uncertain-send pointer remains held. No production migration, grant reset, credential/persona/model change, automation expansion or controlled send was performed during this continuation.

| Live restored runtime file | SHA256 |
| --- | --- |
| automations/content_prompts.py | 6985fba7c32efe7c00d47800a6cd2e939275380a9d8af93819bbebdf81eb216c |
| automations/content_resolver.py | 4de0ee4a2f936989f67dbec81d09403abecb5086dc2330379cd73ef776c1eeab |
| automations/pack_claim.py | 121f0fcafe97106d2a819214b7afc596016336767d91760e935e78458236d385 |
| automations/pack_sender.py | 793645f1a99e73392e70a25dd5af763d3d7961d18f64ae892cc4a41d3fc5a5a4 |

## Completed corrections

The final candidate retains 12c7fed4, b2a00c32, 8d89d1ff, ebd02c31, 3ac035bd and the later fixture corrections. The newer deployed worker/API/sign-in commits 9052698, 8a72ec0 and 4ea6bfa are already ancestors of the consolidated candidate. Their immutable-job-scope, existing folder-state parsing and scrolled sign-in frame fixes are preserved without reimplementation.

Native AI-05 now checks the composed final text for delivery narration and unbacked claims after finalize_draft, retaining link, empty and length checks. Only the exact selected sale's generated count prefix and price footer are exempted from the unbacked-term floor. Saved descriptions and conversational text remain checked. The separate restored FASTT named-request path still makes no added caption call. The valid native card still uses one model call; appended delivery, extra numeric media and invented-price claims are refused.

Affected verification passed: 44 native-caption/media PostgreSQL cases; 101 browser assertions over 48 page/viewport cases; production-session-contract login, logout/replay/outage and cross-brand scope checks. Five additional session fixtures demonstrably failed because they omitted the new RPC, and now pass with the actual production RPC contract mocked. Existing permissions and failure assertions were retained. The final exact-pair native PostgreSQL job has passed; remaining final suite/aggregate results are pending.

## Focused conditional triage

Maintenance fairness keeps the completed bounded preparation and failure isolation, with serial NativeEngine/global-hook capacity disclosed. No new scheduler or scaling work was added. Financial status transitions and unsupported rows retain the completed shared status contract, identity/conflict handling and fail-closed completeness; current production totals are not certified by historical tests. Broker management remains distinct and server-only, with its explicit schema upgrade and credential-separation check required before start. Vault continuation, frozen inputs, pending-source protection and deployed folder-state handling remain; no new ceiling or backoff redesign was introduced. A failing final regression or unresolved money/isolation/authority/duplicate-send risk blocks release rather than being waived.

## Release sequence for review

Use [the existing release/backup/rollback procedure](../astra-correction-release-gates.md) with the final immutable image IDs from the artifact manifest. API, worker, sender and broker use the same reviewed backend image. Reuse the existing populated 0096-compatible restricted rollback proof for the exact retained backend image sha256:49e446b89c828a38212c673d9710b011506fe722724a2955bedf4424ddbb5d3a, source d6811b124928f8167e6625ce9db5db81bf227fad, and dashboard rollback source 871b3e8955bf8af357f6f66d56c4446bf98fee8e. Newly rebuilt copies are not substituted for that tested image without proof.

After Astra's final review: close command/campaign admission; fence or drain the persistent sender; stop other writers, automatic restarts and late sign-in completion. Take fresh encrypted public+onlyfans_proxy backup from the verified 0092 source and separate session-store recovery material, verify isolated quarantined restoration, then run the five-table ANALYZE transaction under 5-second lock and 30-second statement limits. Explicitly migrate 0093, 0094, 0095 and 0096 in order, checking each revision and the final pending-source trigger/index. Apply the broker recovery upgrade explicitly and the dashboard session revocation SQL to the Twin/Moxy store at aouaenqayfltoqrmpitg, never the separate Flirt store lkmfwpqegjluirfusnaw. Preserve owner/agency rows. Legacy HMAC compatibility stays off; admin re-login is required. Start reviewed roles with outgoing activity fenced.

Before reopening paid sending for an account after 0093, require matching current binding and connection coverage, lastCompleteAt within 15 minutes, completed required traversal and no blocking conflicts. No timestamp-only or zero-total substitute is accepted. Preserve every newer receipt, unknown attempt and used counter. The caption-only 0092 rollback is not valid against 0096.

Refresh [the existing controlled-provider packet](../astra-controlled-provider-verification.md) immediately before execution. Use the existing authorised account and confirmed controlled recipient, authenticated actor/current revisions, saved proxy/model/budget and exact approved media/price. Limit the existing persistent dashboard-managed sender to one manual text, one approved paid item and one fresh-input AI reply. No second sender, fabricated fan input, forced purchase or uncertain retry.

## Outstanding execution evidence

Final six shards, SQLite native suite, two isolated scripts and aggregate; complete final immutable-image inventory; production-window backup/restore IDs; approved-window source/config checks; controlled live journey and financial reconciliation remain required. The performance targets remain form 300 ms after shell, list 500 ms, inbox 2 s, thread 1 s warm/2 s cold, list visibility 5 s and eligible AI acknowledgement 120 s. They are not certified by synthetic screenshots; record misses and unmeasured values at the intended starting workload. Serial capacity remains a limit. No 250-creator claim or extra rollout scope is authorised by this packet. AUTH-02/03 remain jointly deferred.

The existing [36-ID register](REQUIREMENTS.md) remains the requirement register. Historical aggregates remain tied to their original commits. This full reliability release has not yet been deployed.

## Immutable forward image inventory

| Role | Image ID | Source commit |
| --- | --- | --- |
| crm-backend | sha256:7578467ec1c0f216983b9b1c763284d5b86cd8a74cb2a9e23d45a1bebca6ba2c | 1c2030eb5fb1a3af7df166019a771dd8f5528f2a |
| crm-dashboard | sha256:98a82df874d61b744d11bf0e8635466f19a3e814abfb9d666c190a9b46f865d0 | 4599999761dbf2ff2b517359642d10262324867a |
| crm-signin | sha256:f1dc62a4d44142648e1ed128de3e6797f61081fbf9ef0bd3770e79edd47bbc9c | 1c2030eb5fb1a3af7df166019a771dd8f5528f2a |

All six build jobs passed. Exact artifact IDs/digests, archive SHA256 values, dependency inventories and expiry timestamps are retained in final-immutable-images-20261028.json. The tested historical 0096 rollback remains selected as described above; newly rebuilt rollback copies are retained without transferring the historical actual-image proof to different bytes.

CI orchestration supplement: the commerce/preupgrade callers now pass the Actions-read capability required by the retained reusable workflow. GitHub forbids permission elevation across nested calls (https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations). Actionlint passed. This changes CI caller permissions and evidence only; the selected runtime source/images and paired acceptance remain pinned to 1c2030eb5fb1a3af7df166019a771dd8f5528f2a / 4599999761dbf2ff2b517359642d10262324867a. No green result is reassigned to the supplement commit.
