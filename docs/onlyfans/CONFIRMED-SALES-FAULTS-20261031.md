# Confirmed OnlyFans sales-engine repairs - 31 October 2026

## Status

| Area | Status |
|---|---|
| Code fixed | Yes; four reviewable commits |
| Local regression tests | Passed |
| Deployed image tests | Passed |
| Deployment | Verified on API, worker and FASTT supervisor |
| Provider-backed contextual card | **Not verified**; no current approved test conversation was supplied |
| PostgreSQL test suites | **Not run in this session**; `ONLYFANS_TEST_PG_URL` was unavailable locally |

Deployed code revision: `f3abbdb8b3182e0678106008cf100fb8203b860d`

Deployed image:
`323149985112.dkr.ecr.ap-southeast-2.amazonaws.com/twin-onlyfans@sha256:817131b4c1a65a483abf277bd8b295a4eb3c92d9c06875c92eb38e537ce9fe10`

CodeBuild:
`twin-onlyfans-release:bfb0ceed-1f6d-4f12-81f8-867e58c06a08` (succeeded)

Deployment SSM command:
`ea232824-d484-4b07-8d87-9c1bc1752cc7` (succeeded)

Image-test SSM command:
`63907c76-d190-450f-9ae1-745a8663be84` (succeeded)

Offer-test SSM command:
`eba1b824-1d78-4faf-93e6-222be2350900` (succeeded)

Post-deploy check SSM command:
`7e283db3-3bd9-494e-bd53-8bfa97a9d544` (succeeded)

## Root causes and repairs

### Purchase-refresh scheduling

Commit `c24afb0` fixed the refresh drift. The next scan was scheduled 300
seconds after scan completion, while purchase freshness was measured from the
earlier provider-query cutoff. A slow paginated scan therefore consumed a large
part of the next freshness window before its replacement began.

The next poll is now calculated from the provider cutoff and the observed scan
duration. A scan that has already consumed the interval is due on the next
worker tick. Long scans retain their actual cutoff and never claim newer
coverage. Existing read leases, provider backoff, pagination and the
`provider_read_deferred` hold remain in place.

Changed:

- `service/onlyfans_runtime/ledger.py`
- `service/onlyfans_runtime/provider_read_budget.py`
- `service/native_pilot/contextual.py`
- `service/tests/test_ledger_refresh_schedule.py`
- `service/tests/test_onlyfans_ledger_pg.py`

### Contextual clarification validation

Commit `a72f976` separated the contextual EXPLORE clarification from the older
non-contextual caption validator. A question such as "want a photo in blue?"
is accepted when it is a contextual format clarification, while prices,
quantities, delivery promises and fabricated media details remain blocked.
Non-contextual `bridge_ok()` callers still use the original rule.

### Contextual rejection observability

The same commit routes safe structured failure codes through the existing event
path:

- `no_interest`
- `invalid_model_output`
- `unsupported_conversation_evidence`
- `invalid_contextual_wording`
- `model_provider_failure`
- `evaluation_internal_failure`
- `no_matching_media`
- `sales_eligibility_refused`
- `purchase_freshness_refused`
- `evaluation_failure`

Events carry account, fan and source-message identifiers where available. Raw
model output, conversation text, credentials and unrestricted application
logging remain excluded.

Changed:

- `service/automations/_hook_upsell.py`
- `service/automations/content_prompts.py`
- `service/automations/contextual_offers.py`
- `service/native_pilot/contextual.py`
- `service/onlyfans_runtime/ledger.py`
- `service/onlyfans_runtime/provider_read_budget.py`
- `service/onlyfans_runtime/launch_ledger/dashboard.py`
- `service/tests/test_contextual_evidence.py`
- `service/tests/test_contextual_offers.py`
- `service/tests/test_ledger_refresh_schedule.py`

### Different-item offer restrictions

Commit `d732bbf` removed the native engine's implicit two-open-offer and
two-offers-per-fan-per-day defaults. They were business caps, not delivery
concurrency locks. Native different-item offers now set
`fastt_offer_fan_caps_enabled=False` through the existing SellLane owner.

The generic path keeps its caps. Native same-item duplicate checks,
already-purchased media exclusion, ignored-offer chasing prevention, spend
velocity, human takeover, send admission, leases and retry protection remain.

Changed:

- `service/native_pilot/engine.py`
- `service/automations/sell_lane.py`
- `service/automations/ai_chatter.py`
- `service/tests/test_native_pilot_engine.py`
- `service/tests/test_native_pilot_offers.py`
- `service/tests/test_offer_restriction_policy.py`

### Discount negotiation

Commit `f3abbdb` changed `may_discount()` from one lifetime cut to at most two
cuts on the current media chain. The first qualifying request is 15% off. A
further explicit negotiation can raise the total cut to 20% off the original
price. The existing discount quote records the original basis, preventing
compounding. A third cut is refused, including after a ladder reopen.

The existing unsend/replacement mechanism remains the only sender. Purchase and
ownership are rechecked inside the fan lease immediately before replacement;
the replacement offer and the payable card use the same quoted price. A failed
or uncertain replacement is not replayed.

Changed:

- `service/automations/upsell.py`
- `service/automations/ai_chatter.py`
- `service/tests/test_requested_discount_policy.py`
- `service/tests/test_native_pilot_postsale_engine.py`

## Test evidence

Local checkout:

- `96 passed`: contextual offers/evidence, refresh scheduling and offer-restriction policy.
- `5 passed`: requested-discount policy.
- `30 checks`: native offer restrictions; all cases passed.
- `82 checks`: native pilot engine; all cases passed.
- `286 checks`: full post-sale engine suite; all cases passed.

Exact deployed image, with `--network none`:

- `286 checks passed`: post-sale engine suite.
- `30 checks passed`: native offer restrictions.
- `82 checks`: native pilot engine; all cases passed.

The image build also passed `compileall` and imported the schema, runner,
contextual and ledger modules. Its source check reported
`CONFIRMED_FAULTS_IMAGE_SOURCE_OK 0103_concurrent_delivery`.

The PostgreSQL-focused suites were not run in this session because the local
host had no disposable `ONLYFANS_TEST_PG_URL`. The deployed image's PostgreSQL
schema check passed before service recreation. This remains a test-environment
gap, not a claim that those suites ran.

## Deployment verification

Before deployment, the live host was still on image
`sha256:269a4c1aac59a43475883f54c3d9d052cd4d07796b37c0944d2e9ba8ef80f257`
at revision `6207b52829a0aa0543987fc96e0c8f09ae1a1990`. No active text send,
conversation lease, held slot or delivery-unknown text existed.

After deployment:

- API, worker and FASTT supervisor all run revision
  `f3abbdb8b3182e0678106008cf100fb8203b860d`.
- All three are running with zero restarts.
- API health is `200 {"ok":true,"role":"api"}`.
- Schema remains `0103_concurrent_delivery`.
- Environment differences are `none` for all three services apart from the
  image and `BUILD_SHA`.
- All accounts remain in `gap` mode with the prior token, burst, concurrency
  and conversation-gap settings.
- No dispatching text, delivery-unknown text, held slots or conversation leases
  were created by the deployment check.

## Rollback

The previous compose file and container inspection are retained at:

`/var/lib/twin-release/confirmed-faults-f3abbdb/`

Rollback command:

```sh
/var/lib/twin-release/confirmed-faults-f3abbdb/rollback.sh
```

The script restores the prior compose file and recreates API, worker and FASTT
supervisor on the retained `concurrent-6207b52` image tags. There was no schema
change in this release, so no database downgrade is part of the rollback.
Do not run this rollback after a later schema migration without rechecking image
compatibility.

## Outstanding verification

No provider-backed contextual card was sent. The repository's historical
designated test conversation belongs to a creator that is presently not a
valid current contextual canary, and no new approved test account/fan pair was
provided. Sending to an arbitrary real fan would violate the request.

To close that final gate, provide one current, approved test conversation on a
connected account with contextual sales enabled. Observe exactly one matching
card, its media/account ownership, payable price, durable receipt, and absence
of a duplicate send. Until then, provider-backed behaviour remains unverified.
