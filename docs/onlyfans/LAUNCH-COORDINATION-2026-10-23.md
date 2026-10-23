# OnlyFans launch coordination, 23 Oct 2026

Source: Jesse's "CONSOLIDATED ADDITIONAL ASSIGNMENT" email, 23 Oct 2026 15:59 (saved copy: JESSE-CONSOLIDATED-ASSIGNMENT-2026-10-23.txt).
No new live send, purchase or activation authority comes from this document.

## Pinned commits

| repo | branch | pinned |
|---|---|---|
| backend (this repo) | codex/onlyfans-offer-import | d7e9d2ff6313c77c498416706ff4ab60475e0616 |
| backend audited base (= deployed source) | n/a | 88e79248e4b504cdaad6cb6c998fad79423e971d |
| dashboard (offer-import worktree) | codex/onlyfans-offer-import | 927b2d75105f6180c37f2759ed119f89521b979d |
| dashboard (launch worktree, Chat 5) | codex/onlyfans-launch-dashboard | 927b2d75105f6180c37f2759ed119f89521b979d (no launch commits yet) |
| dashboard base | main | 244271a36031a9eaf10b1d0ea64f41d97babd80a |

Schema: single head `0089_offer_import`, parent `0088_native_vault_media`. Unpublished, additive.
Pins for Chats 1-4: PENDING, not received.

## Branches and file ownership

| chat | branch | owns |
|---|---|---|
| 1 ledger | codex/onlyfans-ledger-recovery-20261023 | service/onlyfans_runtime/ledger.py, launch_ledger/ |
| 2 history + memory | codex/onlyfans-history-memory-20261023 | launch_history_memory/ |
| 3 personality | codex/onlyfans-personality-20261023 | launch_personality/ |
| 4 proactive selling | codex/onlyfans-proactive-selling-20261023 | launch_engagement/ |
| 5 dashboard | codex/onlyfans-launch-dashboard (Claude, separate session, not started) | dashboard components/onlyfans-launch/, lib/onlyfans/launch/; screens A readiness, B purchases/ownership, C personality/AI settings, D fan memory, E automation/follow-ups |
| us (offer-import branch) | codex/onlyfans-offer-import | importer, fixed-price, content-sync; shared worker/store/connector/planning; shared models; migrations and schema registration (head 0089_offer_import, parent 0088_native_vault_media); API mounting; dashboard navigation and NativeWorkspace; dependency files; common test/CI |

Rule: chats 1-4 do not edit the "us" list. They hand us a contract and we integrate.

## API contracts

| chat | contract | status |
|---|---|---|
| 1 ledger | purchase-ledger parse/recovery API | PENDING, not received |
| 2 history + memory | history recovery status, fan memory API | PENDING, not received |
| 3 personality | personality / AI settings API | PENDING, not received |
| 4 engagement | automation / follow-up API | PENDING, not received |

Our own offer-import and content-sync contracts are in OFFER-IMPORT.md and CONTENT-SYNC.md.

## Integration status

Honest as of this commit. "Live verified" is NO for everything: nothing is deployed.

| item | code complete | integrated | deployed | live verified |
|---|---|---|---|---|
| Offer import (OnlyFans to CRM) | yes on branch | n/a (own branch) | no | no |
| Fixed-price selling | yes on branch | n/a | no | no |
| Content sync (CRM to OnlyFans) | partial: no verified unpublished upload (blocker B1), paid-post read unverified (B2) | n/a | no | no |
| Migration 0089 | yes | n/a | no | no |
| Chat 1 ledger | UNKNOWN | no | no | no |
| Chat 2 history + memory | UNKNOWN | no | no | no |
| Chat 3 personality | UNKNOWN | no | no | no |
| Chat 4 engagement | UNKNOWN | no | no | no |
| Chat 5 dashboard screens A-E | no, not started | no | no | no |
| Combined launch acceptance | no | no | no | no |

Known test gaps: see section 3 of OF-IMPORT-CHECKPOINT-50-2026-10-23.txt. The browser HTTP test has no passing run on disk; the aggregate pass count is UNVERIFIED.
