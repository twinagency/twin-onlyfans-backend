# Confirmed OnlyFans sales-engine repairs - 31 October 2026

Final revision: `e31fa5cbf96112676d4e6d99976f33a285ff24e6`.
The earlier deployment notes below are retained as release history; the final
status, evidence and rollback details in this section supersede them.

## Status

| Area | Status |
|---|---|
| Code fixed | Yes; thirteen reviewable commits on the final branch |
| Local regression tests | Passed; final review tests `141 passed` |
| Deployed image PostgreSQL tests | Passed; `33 passed` on the exact final image |
| Deployment | Verified on API, worker and FASTT supervisor |
| Purchase freshness | Verified across repeated live scans; zero stale refusals after final deployment |
| Contextual model budget | Verified live at `1,200`; one hook completed normally at `334` tokens |
| Provider-backed contextual card | **Not verified**; no current approved test conversation was supplied |
| Provider-backed card delivery | **Blocked** pending an approved account/fan test conversation |

Deployed code revision: `e31fa5cbf96112676d4e6d99976f33a285ff24e6`

Deployed image:
`323149985112.dkr.ecr.ap-southeast-2.amazonaws.com/twin-onlyfans@sha256:3facf3a80d5d05b9024d6343f76653ad1259174710b21c727bfc043b485c2ddd`

CodeBuild:
`twin-onlyfans-release:132ab223-448d-48df-af45-40e930c65863` (succeeded)

Deployment SSM command:
`21127a1a-44b4-4250-a980-ccd7e7fa7fa7` (succeeded)

Final image PostgreSQL command:
`dcf0e439-daf6-40d3-9e72-256f852ceff1` (succeeded; `33 passed`)

Final deployment monitor command:
`45d8e5a7-55be-4fc9-9d73-abd73cd6ea92` (succeeded)

Final context/freshness audit commands:
`86a255ec-f3d1-40c6-9119-d04c16032e2c`, `9cc3941e-c4f9-4036-a6a2-a8611d43dcab`,
and `8c1cbbc6-b7ee-4bde-a36a-eddcf1ac5529` (succeeded)

## Review follow-up repairs

The independent review found four remaining gaps. All executable code gaps
were closed before the final image was built:

1. **Future delivery promises.** The EXPLORE bridge could say
   `"I will send you a video tonight, okay?"` and pass both downstream media
   checks. `has_delivery_promise()` now owns the future/current-promise rule,
   and both `voice_line_ok()` and `claims_media_sent()` use it. The reproduced
   sentence is now rejected by both paths, while a question such as
   `"want me to send you a photo?"` remains allowed.
2. **Provider lease contention.** Recent inbox reads could take the single
   account read lease immediately before a due financial refresh. The ledger
   scheduler now makes a durable, 45-second-bounded priority reservation and
   commits it before deferring inbox work. The reservation survives the
   worker/inbox race but cannot strand intake if the ledger worker dies.
3. **Incomplete contextual model output.** The contextual hook was still on
   the default 400-token allowance. `content_resolver_hook` now receives the
   resolver's 1,200-token cap. Incomplete replies record only safe diagnostics
   (`finish_reason`, `completion_tokens`) so a recurrence can be diagnosed
   without retaining provider bodies or conversation text.
4. **Stale partial refresh windows.** A recent purchase scan that outlives the
   345-second freshness contract now restarts with a new provider window
   instead of extending the stale one. Normal refreshes use the last proven
   provider cutoff with a bounded overlap; the independent history lane still
   owns older transactions and late refunds.

Final review commits:

```text
bf05015 Block future delivery promises in captions
32dbae0 Give contextual hook the resolver output budget
25aa60f Reserve the next provider lease for due purchases
2c02917 Enforce delivery promises at final text boundary
67a4c3c Keep due purchase refresh ahead of inbox reads
3854b6b Record safe diagnostics for incomplete model output
c66b5a9 Preserve intake turn after each ledger page
711b23c Commit ledger reservation before deferring inbox reads
e31fa5c Refresh recent purchases from the last proven cutoff
```

Changed in this follow-up:

- `service/automations/pack_claim.py`
- `service/native_pilot/media_claims.py`
- `service/native_pilot/model.py`
- `service/onlyfans_runtime/ledger.py`
- `service/onlyfans_runtime/provider_read_budget.py`
- `service/tests/test_contextual_evidence.py`
- `service/tests/test_contextual_offers.py`
- `service/tests/test_ledger_read_fairness_pg.py`
- `service/tests/test_ledger_refresh_schedule.py`
- `service/tests/test_native_pilot_model.py`
- `service/tests/test_onlyfans_ledger_history_pg.py`
- `service/tests/test_onlyfans_ledger_pg.py`

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

## Final review evidence

Local checkout, final review tests:

```text
141 passed, 2 warnings in 6.77s
```

The suite covered contextual evidence and future-promise rejection, native
model caps and safe incomplete-output diagnostics, contextual origin guards,
refresh scheduling, offer restrictions and discount negotiation.

Exact final image, real PostgreSQL, networking through the existing isolated
test network:

```text
33 passed, 71 warnings in 252.95s
```

This included the incremental recent-window test, lease fairness, stale-window
restart, full-history resynchronisation and reconnect fencing.

The final image build also asserted at build time that:

```text
HEAD == "0103_concurrent_delivery"
content_resolver_hook is a resolver purpose
"I will send you a video tonight, okay?" is a promise and is rejected
the recent window starts from the prior proven cutoff with overlap
```

### Live provider evidence

The final deployment was observed across two recent-refresh cycles:

- All six enabled, verified creator accounts had provider coverage inside the
  345-second freshness contract at the final audit.
- `purchase_freshness_refused` count after the final deployment: `0`.
- Cristina (`of-63665785`) had a six-minute incremental window
  (`window_seconds=606`), completed with offset `3`, and later refreshed again
  with fresh coverage. A lease deferral occurred without leaving the account
  stale, demonstrating the intended bounded wait rather than a silent false
  claim of freshness.
- The other enabled accounts used recent windows of roughly 515-601 seconds,
  not a repeated seven-day rescan.
- A live `content_resolver_hook` call recorded
  `max_output_tokens: 1200`, completed normally with `334` completion tokens,
  and produced no `model-output-incomplete` event.
- No dispatching text, delivery-unknown text, held dispatch slot or active
  conversation lease was present at the final read.

### Remaining provider-delivery gate

`of_contextual_offer` still contained zero rows after the final audit. Several
live evaluations were observed and correctly classified as `no_interest` or
`no_matching_media`, but none was a successful contextual card delivery.

No account/fan pair was explicitly supplied as an approved test conversation.
The historical canaries are not safe to message without fresh approval, and the
request explicitly prohibits sending unsolicited test messages to real fans.
Therefore provider-backed card delivery remains unverified. The code, database
integration and deployment gates are complete; this final live-delivery gate
requires an approved connected account/fan conversation and one controlled
inbound message.

## Earlier release test evidence

This subsection preserves the evidence from the earlier `f3abbdb` deployment.
Its PostgreSQL gap was closed by the final `e31fa5c` image test above.

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

## Final deployment verification

The final image was deployed after the exact-image PostgreSQL gate passed.
Before recreation, the prior live image was
`release-711b23c`; the release process preserved API and worker environment and
restored the supervisor's explicit seven-account scope immediately after the
recreation window.

Final live state at `2026-10-31T15:50:52Z`:

- API, worker and FASTT supervisor all run revision
  `e31fa5cbf96112676d4e6d99976f33a285ff24e6`.
- All three are running with `restarts=0`.
- API health is `200 {"ok":true,"role":"api"}`.
- Schema remains `0103_concurrent_delivery`.
- The supervisor scope is the preserved seven-account explicit scope:
  `of-176774528:*`, `of-210806616:*`, `of-24181859:*`, `of-323358988:*`,
  `of-63665785:*`, `of-87030857:*`, `of-99421145:*`.
- All six enabled and verified creators had fresh recent-ledger coverage at
  the final audit.
- There were zero `purchase_freshness_refused` events after the final
  deployment.
- Cristina's account completed an incremental recent scan with a 606-second
  window and later refreshed again without stale coverage.
- The contextual model hook recorded `max_output_tokens: 1200` and completed
  normally with `334` completion tokens.
- No dispatching or delivery-unknown text, held dispatch slot or active
  conversation lease was present.

The final deployment is not a schema migration. The database remained at
`0103_concurrent_delivery`.

## Rollback

The previous compose file and container inspection are retained at:

`/var/lib/twin-release/release-e31fa5c/`

Rollback command:

```sh
/var/lib/twin-release/release-e31fa5c/rollback.sh
```

The script restores the prior compose file and recreates API, worker and FASTT
supervisor. It validates the compose file, waits for the three services, and
prints the resulting image/revision plus API health. The retained snapshot is
`compose-before.json`; there was no schema change in this release, so no
database downgrade is part of rollback. Do not run this rollback after a later
schema migration without rechecking image compatibility.

## Outstanding verification

No provider-backed contextual card was sent. There were still zero
`of_contextual_offer` rows after the final audit. Existing live evaluations
correctly recorded `no_interest` and `no_matching_media`, but those are not
card-delivery successes.

The historical test fan IDs are not sufficient authorization to message a real
fan, and no current approved account/fan test conversation was supplied.
Sending to an arbitrary fan would violate the request. To close this single
gate, provide one current approved test conversation on a connected account
with contextual sales enabled. Observe exactly one matching card, its
media/account ownership, payable price, durable receipt, and absence of a
duplicate send. Until then, provider-backed card delivery remains unverified;
all other code, test, database and deployment gates are complete.
