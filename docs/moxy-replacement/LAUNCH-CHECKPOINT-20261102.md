# Moxy launch checkpoint, 2 November 2026 UTC

## Actual release state

Jesse explicitly authorised completing deployment and commercial testing. This checkpoint does not revoke that authorisation. Do not request deployment permission again merely because older handoffs say review only.

The full Moxy engine is NOT deployed or enabled. No customer messages or purchases were made by this launch session. Production schema remains 0110_recent_history; no Moxy account activation or migration 0111 has run.

Only the bounded vault-page repair is deployed: commit d27368e45fd058d6a3a82c7c22c87f333e46c245, worker image sha256:21cf0c7143604855911b969492798c3d8ad0ad691c7f92d027a37d3e9e48adfd. Other service versions were preserved.

## Corrections 2026-11-03 (supersede statements marked SUPERSEDED below)

Applied from Jesse's handoff brief of 3 Nov 2026. Where this section and an earlier statement disagree, this section governs.

Bundle candidate counts:
- The earlier totals of 1,116 (Amelia) and 816 (Cristina), 1,932 together, were double-counted. The two inventory runs each rescanned the same saved priced-message rows and each wrote its own candidate set, so every combination was counted once per run. SUPERSEDED.
- Correct distinct attachment combinations: 558 Amelia + 408 Cristina = 966.
- Saved priced-message rows scanned (unchanged): Amelia 78,451; Cristina 67,348. Row counts include priced rows that may not contain media.
- The 966 combinations come from historical priced messages. They are NOT individual media counts, verified purchases or automatically approved products. A combination groups unordered attachment ids only; price and order variants within it are retained, and previews and lock state are unverified until a provider source is read for that exact message.

Vault import state:
- At 3 Nov 2026 05:14 Brisbane (2 Nov 19:14 UTC) each account had 60 imported vault items. Amelia had zero nonempty descriptions; Cristina had one. Both vault imports were still pending.
- This supersedes the earlier 36-item snapshot below, which is kept only as history.

Commercial mapping recovery (code, this branch): offer_import.recover_mappings is a read-only report that deduplicates across runs, keeps variants, blocks offers with unavailable assets, maps only an exact existing catalogue item automatically, and lists everything else in an unresolved register. It creates no approvals, prices, bindings or sequences. Amelia's "VIP vault graphics" are progress/marketing artwork and are not a sequence. Not yet run against production.

## Preserved integration

Backend branch: codex/moxy-launch-20261103.
Code commit: f237162eb28d92baa73d89c894bd666250156ce1.
Parents: Claude 2f60f8bd3253156cabf281bd4192cc586f0b5965 and scoped-vision repair 18ff3a32f5f71d7df100248d8df7a073a71573ea.
This incorporates production 9d6756b, bounded vault repair d27368e, Claude completion, and delivery fixes. It preserves production changes and Moxy sender ownership fences.

All 1007 service files were compared by Git blob hash against the qualified development worktree: zero mismatches. Never upload environment files, session secrets, credentials or raw customer archives.

Development worktree: /srv/projects/moxy-launch-20261103 on work-server. Its local Git ancestry remains the original 54020a7 base plus the preserved overlay. Do not force-push that ancestry over the published integration branch. The published commit is the reproducible source.

Changes added in this launch pass:
- Bounded per-account vault pages: six items for the two launch accounts; unchanged 1 MiB response and ownership protections.
- Fresh ingress temporarily defers a queued reply instead of permanently refusing it. Wire reservations and counters are released consistently; new fan input, takeover and expiry are revalidated.
- Existing stale/recovery refusal remains intact.
- Legacy egress fixtures explicitly identify non-Moxy ownership; the API route allowlist includes only the specific Moxy configuration GET/PUT route.
- Optional scoped description credentials use only the selected account's verified OpenRouter key, only for describe_media. Unselected accounts retain existing behaviour. An unconfigured fallback provider remains blocked.
- No scoped description flag has been enabled persistently. That repair is committed, not deployed.

## Test evidence

Counts overlap; do not add them into a unique-test total.
- Initial integrated Moxy suite: 229 passed, 705.12 seconds.
- Vault regression selection: 66 passed, 57.03 seconds.
- New ingress regression failed on unpatched source as expected.
- Broad delivery qualification: 99 passed, 13 failed. All 16 new ingress cases and the three latest multicreator cases passed. Failures were investigated: stale/recovery deferral was narrowed, pending-ingress expectations updated, legacy-owner fixtures corrected, and the exact Moxy API route added.
- Final delivery/fence selection: 181 passed, 271.43 seconds, exit 0.
- Scoped credential tests: 15 passed, 1.59 seconds. An initially missing OpenRouter fallback registration was caught; the final implementation refuses that unavailable fallback instead of selecting an unregistered model.

Logs on work-server:
- /srv/projects/moxy-launch-integration-20261103.log
- /srv/projects/moxy-vault-after-20261103.log
- /srv/projects/moxy-pending-before-20261103.log
- /srv/projects/moxy-delivery-qualification-20261103.log
- /srv/projects/moxy-delivery-final-20261103.log and .exit.json
- /srv/projects/moxy-caption-key-test-20261103.log

Disposable PostgreSQL only: moxy-review-pg-20261102, port 55432. Runner /srv/projects/moxy-launch-test-runner-20261103.py obtains its fixture credentials internally. Never use production for regression tests.

## Production data recovered

Correct accounts:
- Amelia: of-312988353
- Cristina: of-63665785
Tenant: twin-internal.

The accounts have verified native connections with encrypted account-bound OpenRouter model credentials. The worker's global house key is absent. That mismatch explains the automatic description failures; it is not proof that no account model key exists.

The normal inventory importer scanned already-saved outgoing priced-message rows, with no new history downloads and no model calls:
- Amelia: complete, 78,451 rows scanned, ~~1,116 bundle candidates~~ (SUPERSEDED, see Corrections 2026-11-03).
- Cristina: complete, 67,348 rows scanned, ~~816 bundle candidates~~ (SUPERSEDED, see Corrections 2026-11-03).
These are inventory candidates, not approved products, verified purchases or revenue. Row counts include priced rows that may not contain media. Exact source observations, previews, asset membership and current approved prices still require validation.

Inventory job IDs:
- Amelia: 6337dbb9-8476-47d2-960d-19ed4effcedb
- Cristina: 41f36e40-ee7c-4fa6-9926-988ac0e97990

SUPERSEDED snapshot (see Corrections 2026-11-03 for the later 60-item observation): last checked vault rows: 36 per account. Amelia had 35 failed descriptions and one fetch_failed. Cristina had one described asset and 35 failed. This was an advancing partial vault import, not a complete vault snapshot.

One bounded real-media check succeeded for Cristina: media 4773415574, call 43557, 269 description characters. Its generated timestamp was newer than its row update timestamp. Amelia had no item eligible under the existing retry delay, so its check attempted zero items. Do not call Amelia verified or bypass retry delay to manufacture a result.

No persistent scoped-key flag or bulk model-analysis job was started.

## Approved graphics inspection completed

Jesse explicitly approved the previously blocked three-image OpenRouter read with "Yes go". The approval applies to this bounded inspection, not unlimited background analysis.

A single model call read Amelia's folder 27540040 ("vip vault graphs") images 4294360357, 4294360356 and 4294360354. Result: call 43558, OpenRouter Qwen3-VL-30B, recorded cost 7 millicents (US$0.0007), maximum output 1024 tokens, one model attempt. SSM evidence: 3e2c5979-1ee0-4505-b317-84aee94e4ed5.

The machine extraction found marketing/progress artwork:
- "Invite Only Stripping", 50% complete.
- "Invite Only Stripping", 25% complete.
- "Invite Only Amelia's Locker Room", "Vault Unlocked", with four broad category names.

No exact content IDs, prices, bundle membership or ordered four-item paid-message level were extracted. The model labelled the third image "ordered_four_ppv: true" despite returning only category names. That unsupported interpretation is NOT accepted as a sequence mapping. These results do not authorise products or resolve creator-sheet ambiguities.

Two preliminary executions stopped before model I/O because the inspection helper called the synchronous native transport inside an async loop. Diagnostic trace identified the existing guard; transport calls were correctly moved to asyncio.to_thread. The guard was preserved. The request log showed no Amelia describe_media calls before the successful bounded call. No customer messages, payments, account activation or persistent model-analysis flag changes occurred.

The exact commercial sequence is still unresolved. Retrieve Moxy's existing VIP Vault/Locker Room scripts or sequence export showing which actual content belongs to each step and its price. Do not ask Jesse to create a new strategy or manually catalogue the entire vault; map existing authoritative source material. Current fan ownership, labels, operator responsibility and provider qualification remain independent release gates.

## Remaining launch gates

1. Verify exact asset-to-product-to-sequence mappings against current source material and creator price rules. Historical bundle frequency and folder names are evidence, not approval.
2. Verify exact purchase/ownership links for the selected real test fans. Do not globally set ownership_verified or purchase_links_verified true.
3. Resolve current labels/exceptions, VIP qualification, Cristina's graveyard shift, assigned handover operator and external sender coordination from actual operational records.
4. Complete actual-model conversation and controlled provider qualification on the configured products. Synthetic tests and one successful media description are not a completed real selling test.
5. Reconcile dashboard branch/source against the running frontend; preserve newer onboarding work.
6. Prepare coordinated schema-compatible release for all affected processes. Old strict-schema processes cannot simply be left running across migration 0111. Preserve paused legacy supervisor, existing obligations and uncertain delivery records.
7. Enable only the verified creator/test scope, verify real receipts and reconciliation, then expand within Jesse's launch authorisation.

Do not invent defaults to clear readiness, silently skip Moxy sequences, restart stopped bulk history/memory jobs, or present the current repair as commercially live.

Useful SSM evidence IDs:
- Vault repair promotion: 0ba7622c-f37b-4bd7-ac2f-b716993eeddb
- Bounded description: 4ad1f6a8-dc82-4439-8261-5b699a4a618d
- Description/inventory state: 9583ec63-7c1a-42e6-8986-f1acf83d7cac
- Completed inventory scans: f8f96425-0e60-4493-84a0-ea5f82244cd6
- Verified graph folder metadata: e472c89f-2a35-4872-911d-9bb76eda8c68
