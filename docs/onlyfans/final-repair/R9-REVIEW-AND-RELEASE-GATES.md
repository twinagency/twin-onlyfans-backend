# R9 review and release gates

Nothing in this file authorises execution against production. This build authorises isolated implementation and tests only.

## Before approving release

1. Reconcile the supplied consolidated brief with the private revised R0–R9 execution plan and original incident/transcript cases. Recover any additional unpushed original work before declaring those requirements complete.
2. Review the exact candidate commit and its diff from `a6b93bd227fec82589f955593811543889194613`. Confirm no Moxy branch or unrelated production setting is included.
3. Run the complete isolated service suite on an owned PostgreSQL instance with `ONLYFANS_TEST_PG_URL` and the appropriate native test URL. Include the modules excluded by the default runner, PostgreSQL migration chain/idempotence, multiple workers, locks, recovery and load cases. Run the Twin bridge contract/roundtrip and Windows runner checks with their real sibling repository and toolchain.
4. Run repeated real-model generations against the private incident cases and nearby legitimate cases. Independently score answer completeness, relevance, refusal/hold handling, product truth, price, progress and excessive suppression. Synthetic transport passes are not quality approval.
5. Read-only parity snapshot: deployed API/worker/supervisor SHA and image digest; schema revision; saved configuration/fingerprints; binding/connection revision; shared ownership; active holds; ledger coverage; unresolved intents/receipts; inventory descriptions and version coverage. Validate that the shelf contains actual suitable content. Do not infer deployment from a branch.
6. Verify a staffed owner, case routing, due dates, review coverage, provider limits, model spend budget, rollback image compatibility and monitoring. An operator case without a staffed owner is an unresolved gate.

## Proposed configuration diff for separate approval

| Item | Candidate change | Production change made here |
|---|---|---|
| Contextual mode | existing off/shadow/live retained | none |
| Commercial initiative | lower/standard/active, prepared pilot active | none |
| First reference window | [2,6] confirmed raw conversational bubbles, adjustable | none |
| Repeat reference window | [3,6] completed grouped replies, experimental and adjustable | none |
| Live delayed sales jobs | new R5 live policy refuses timer/payment-only nudges and cards | no production activation |
| On-request permission | existing independent authority retained | none |
| Schema | nullable additive revision 0108 after 0107 | not applied |

The legacy scalar cadence fields remain readable for old clients; they no longer derive the first or repeat windows. Counts are reassessment inputs, never send authority. Windows changing mid-attempt preserve counters and history. Creator restrictions, stops, budgets, ownership, support and delivery uncertainty remain authoritative.

## Migration and rollback assessment

0108 adds nullable attempt state to `of_conversation_state`, invitation terms/body/delivery association to `of_sales_invitation`, and quote/lifecycle fields to `of_text_intent`. It creates no new sender, queue or sales authority. Real disposable PostgreSQL now verifies the candidate chain, populated historical upgrades and encrypted restore quarantine; current commands and source scopes are in [the qualification checkpoint](QUALIFICATION-CHECKPOINT-20261102.md). This does not establish the observed production branch's compatibility or production lock duration. Live `0106_parallel_history_reads` descends from `0105_earnings_totals`, while this candidate descends from `0105_operator_cases` and `0106_read_lane_fairness`. Do not run a generic upgrade, stamp away a revision or overwrite newer overlays. Reconcile the exact graph and preserve the unpublished worker changes first.

The former rollback source `d6811b1` refuses 0108 at startup. The preceding candidate `fceb624b814435f36be33d0938c3a5b46494a8e5` passes the unchanged read-only API preservation, account-isolation and closed-command test against 0108. It is a source-level recovery option with all senders/background writers kept off. A matching immutable image and compatibility with the reconciled deployed graph still need qualification; do not treat this as approval to install it.

Downgrade intentionally retains evidence. Turning a feature off must not erase attempts, refusals, purchase ownership, accepted obligations or uncertain delivery. Do not roll back to an image that cannot safely interpret the advanced schema. Recover forward through the existing positive-receipt authority; do not replay a timed-out POST based on silence.

## Original real-account delivery gates retained

After separate explicit authorisation, perform the original two approved account checks using an approved test conversation. Record exact recipient, approved assets/previews, canonical amount, configuration and ownership revisions, one dispatch intent, provider acceptance, local projection, paid unlock and purchase history. Never count an invitation or planned card as completion. These checks have not been run in this continuation.

Only after those checks and a further explicit pilot approval may an approved single creator use the prepared active profile. Start with separately approved shadow observation where appropriate, then a staffed, bounded live pilot. Do not enable additional creators by default.

## Measurement and abort

Record every eligible and excluded attempt. Show first-offer bubbles and repeat-offer grouped replies independently, including no-card attempts, holds, pending invitations, clarification extensions and failed/uncertain deliveries. Compare collected revenue per eligible fan with a declared baseline, request-to-offer completion, delivered cards, confirmed purchases, order value, repeat purchases, complaints, cost and substantive reply latency. More invitations are not more revenue.

Measure the 95% within 180 seconds target against a declared multi-creator load. Existing SQLite synthetic throughput is not a supported production capacity claim. Include provider throttling, read fairness and the retained 300-second post-401 cooldown in the assessment.

Abort on an unauthorised recipient/action, duplicate send or charge request, inconsistent price/assets, broken hold/takeover, identity/safety failure, lost receipt/ownership history or unresolved delivery. Use the existing runtime/conversation stop authority with separate operational authorisation. Merely switching contextual mode off does not revoke separately enabled on-request or legacy follow-up permissions. Preserve and reconcile in-flight obligations; do not flush deferred pitches.
