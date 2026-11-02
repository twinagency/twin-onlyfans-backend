# Provider recovery qualification

A confirmed 429 rejection can now retry with a new immutable intent and bounded idempotency key after the existing account cooldown, provided the original snapshot is unchanged. The model decision and model charge are reused. Three proved rejections stop automatic retries. Original rejection receipts remain intact. Changed prices, refusal state or Off mode prevent recovery. Accepted/unknown attempts are never reopened.

401 rejection is a visible provider_authentication_required hold. Campaign provider rejection is an explicit campaign_provider_recovery_required hold; automatic campaign recovery remains unfinished. No production authentication or settings were changed.

First exploratory run: 3 failed, 14 passed in 74.49 seconds. It exposed gap-mode cooldown timestamp selection and an overlong retry idempotency key. Corrected recovery/integrated suite: 17 passed in 73.38 seconds. Extended recovery/campaign regression: 14 passed in 63.85 seconds. Counts overlap; do not add them.

All new provider responses are simulated fault injection. Actual provider recovery and actual-model quality remain unverified. Prior qualification limitations remain in force.
