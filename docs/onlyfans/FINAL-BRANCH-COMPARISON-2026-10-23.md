# OnlyFans integration selection

The selected main implementation combines the native integration with the
stronger behaviors from the concurrent Claude work. Selection is based on
contracts, preserved evidence and executable regressions; a branch name or
model attribution does not establish correctness.

The review inventoried 30 backend refs (28 distinct heads) and 428 dashboard
refs (414 distinct heads). The recent backend Claude and Codex combined aliases
both reached `e7eae43485a053bb74b9efef3f13b577cee41f48`. Recent backend feature
branches were already ancestors. Dashboard Claude's combined head
`710005d67c99db46e62c6b133a7e259821aa8846` was incorporated before the additional
OnlyFans UI branch `28fd0038966550c733c11fbfe5c53d52403eed1f` and production-main
presentation commit `0b2a88e02a58fc5b1724d8cf45d4e2ef727e0b68` were reviewed.
The subsequent main publication `979e097946dad0c1aa3cb83680dc584cb28e6533`
contains five further commits: the same studio UI, its explicit sidebar color,
bounded release mirror recovery and scoped navigation geometry checks. Those
contributions are reconciled while retaining native launch controls and the
tested hydration fix; the final candidate retains this main ancestry too.

| Area | Selected behavior | Why |
| --- | --- | --- |
| Personality, memory, engagement and stopping | Integrated native workflows, including concurrent selective event hooks and metadata-only credential reader | Real purpose-separated dashboard/backend rehearsal passed; binding, replay, revision, permission and unknown-outcome protections remain authoritative. |
| Ledger | Concurrent canonical flat read/retry plus integrated paginated evidence/resolution | One durable retry scheduler, complete scan/connection CAS, current-binding projections and supported evidence-backed investigation. |
| Refresh and late imports | Integrated durable receipt queue and bounded repair | More than 50 requested sources and occupied-account requests persist; late lower-ID arrivals are independent of provider order. No three-attempt cutoff silently loses work. |
| Reconciliation fairness | Claude's independent priority cursor, integrated with existing ordinary rotation | The new regression exposes repeated priority-prefix selection; three enabled items rotate within three quota-one passes. |
| A/B/A transitions | Integrated immutable snapshots plus separate occurrence history | All three transitions survive without rewriting prior evidence or duplicating immutable snapshot identity. The upstream scenario asserts two snapshots and three ordered occurrences. |
| Paid posts | Integrated candidates, source holds and audited real cursor proof, with Claude's minimum archive progress fix | A head read cannot consume every continuation pass. Unknown provider pagination remains partial until actual account/session validation; deleted or retracted sources hold sales. |
| Shared provider reads | Integrated account/session-fenced budget | Recent, history, ledger, refresh, posts and vault share authority and backoff. The lease remains valid through atomic result commit, with reservations for recent intake and maintenance. |
| Carrier uploads | Integrated fresh permission and conservative recovery | Current allowlist, role, binding, session, asset lease and revocable dashboard permission are checked at write boundaries. Every prior uncertain receipt/cleanup blocks duplicate upload. |
| Schema | One forward `0091_launch_controls` after published `0090_launch` | Preserves the concurrent ingestion index and priority cursor without rewriting deployed/published migration history or installing competing source/read-gate tables. |
| Dashboard | Native operator controls plus reviewed new OnlyFans overview/presentation | Functional controls and strict contracts remain; current production Telegram presentation is preserved. UI changes require renewed build/browser acceptance. |

All 15 upstream content-findings scenarios were adapted to the selected
authorities without a global pacing bypass. Their 23-test reconciliation run
passed with zero failures, errors or skips. The previous integrated paired
rehearsal passed all four cases. These targeted proofs are overlapping evidence,
not a summed unique release total. The final exact source pair still requires
the complete declared acceptance and repository gates before promotion.

No branch was deleted. Older divergent hosted-provider/demo branches remain
available; they are separate alternatives rather than replacements for the
validated native account authority. History memory remains conservative English
pattern extraction and lexical source retrieval. Real provider cursor coverage,
production deployment, account verification and autonomous activation are
separate release outcomes.
