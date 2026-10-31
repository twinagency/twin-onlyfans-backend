# Reply throughput controls - 2026-10-31 (branch claude/reply-throughput-20261031)

Obeys `ONLYFANS-SHARED-CEILING-2026-10-31`: send gap never under 10 s per account, default 20 s, first 429 returns the
account to 20 s and alerts, nothing ramps up by itself. Every behaviour change below is off or unchanged by default.
Nothing here is deployed; migration 0101 has only been run on a throwaway local PostgreSQL.

| control | default | what it does |
|---|---|---|
| `of_delivery_policy.daily_limit_units` (migration `0101_daily_send_cap`) | NULL = no cap | rolling-24 h cap on the account's FASTT sends (dispatching + sent + delivery_unknown; a definite not_sent gives its place back). Reason `allowance-daily-cap`; no model call is spent while capped. Set through the operating-limits command / `POST /onlyfans/control/v1/accounts/{id}/activation/operating-limits` optional field `dailyLimitUnits` (int 0..20000 or null). The lifetime `limit_units` (0..10000) is untouched. |
| `FASTT_PER_ACCOUNT_SEND_GAP=1` | 0 (every account held to >= 20 s as before) | the account's own `throttle_seconds` (10..) sets its gap; a stored value under 10 means "not set" and falls back to 20. `FASTT_MIN_SEND_INTERVAL_S` can then only raise it (its floor becomes 10 instead of 20). Ramp is a manual per-account setting: 20 -> 15 -> 10. |
| `ONLYFANS_ALLOW_SEND_GAP_BELOW_FLOOR=1` | unset | without it the activation / operating-limits commands refuse `throttleSeconds` < 10 (HTTP 400). The DB check stays 1..86400. |
| first provider 429 on a send | always on | `engine_alert` row `provider-throttle` (open), `provider-throttled` event, and if the account's throttle was under 20 it is set to 20 with a `of_delivery_policy_change` audit row (actor `fastt-engine-backoff`). Nothing in the engine lowers a throttle again; the alert stays open until an operator looks. |
| `FASTT_ACCOUNT_TUNING` | unset | JSON `{"of-123": {"concurrency": 8, "quiet_s": 3, "burst_max_wait_s": 20}}` per account (same bounds as the global knobs: concurrency 1..16, quiet 1..60 s, burst max 5..300 s). Defaults stay 4 / 7 s / 45 s. The send gap is deliberately not settable here. |
| `FASTT_STALL_ALERT_S` | 600 | owed work (not held for a person) older than this raises `send-stall` (event every 5 min while it lasts + `engine_alert` row `send-stall`; fields `moving`, `oldest_wait_s`, `blocked`); `send-stall-cleared` when it resolves. |

`engine_alert` is an additive table in the `fastt_runtime` schema (created by the runner on start, SQL in
`service/native_pilot/sql/engine_alert_{upgrade,rollback}.sql`); the application schema change is migration 0101 only.
Rolling back 0101 needs the downgrade in the migration or a forward-fix; old images do not accept head 0101.

Tests (plain scripts, no live credentials, run from `service/tests` with the `ONLYFANS_TEST_PG_URL` of a disposable server for the `_pg` ones):
`test_throughput_daily_cap.py`, `test_throughput_engine.py`, `test_throughput_burst.py` (`BURST_N` fans, default 1000),
`test_throughput_migration_pg.py`, `test_throughput_queue_cap_pg.py`, and the added case in `test_onlyfans_activation_pg.py`.
