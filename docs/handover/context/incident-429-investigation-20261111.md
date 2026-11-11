# Twin Plan C pilot - unanswered message incident (11 Nov 2026) - independent investigation

Read-only on production. No code/config change, deploy, model call, fan message or purchase was made.
All times Brisbane (UTC+10) unless marked Z.

## 1. Verdict

**What failed.** Fan 318022380 sent message 12024091960310 at 6:00:55 pm. Our model call got HTTP 429 at 6:00:59 pm. After that, the pilot's scheduler never selected that request again. It is still `open / prepare_reply`, revision 0, with no second model call, no handover, no cancellation and no alert. The 6:13 pm reply was a **new** request for the new message "Baby?". It was not a retry.

**Our fault (proven offline on the deployed code):**
- `SupervisedPilot.tick()` counts every wired `released` model call as "uncertain" and excludes its request from automatic work forever. That includes 429, any http_* code, and wired `not_sent`.
- This breaks the engine's own contract. `ModelUnavailable` is documented as "the request stays open and may be retried later", and the ledger already treats a released call as settled at zero cost.
- A request stuck this way is never superseded. Only `plan()` for the old message can mark it superseded, and that never runs. So it stays silently open.
- Nothing watches for it: no handover, no operator case, no supervisor check.
- This is not a one-off. The 7:35 am and 7:38 am 429s (requests 6122ac90, b968a156) are stuck in the identical state today.

**Unknown.** Why the provider returned 429 cannot be recovered. The response body and headers were discarded (openrouter.py:206-207). There is no generation id for a 429. The worker writes no logs (4 lines total in the container). The key file is a "reused" key, so other traffic on it is not visible to us. **No evidence supports blaming Relace capacity, OpenRouter limits, spending or our message rate.** Our own 10 Nov note saying "Relace upstream pin capacity" was not evidence-based and is withdrawn.

**Fix status.** A candidate scheduler change was proven offline to restore recovery, keep the 2-call limit, and hand over after a second failure. The **recommended narrower version (section 5) has not yet been run.** Nothing is fixed in production.

## 2. Deployed version (verified 11 Nov, ~6:58 pm)

- Instance i-09a9dd3bd749643a5 (Name tag twin-dashboard-prod), SSM Online.
- Container `twin-plan-c-pilot-active-sender`, image sha256:06562198113f350d2b8eefef22c9ed5217f8a1b6c93f334eac6c7e3181f40aeb, started 23:51:21Z on 10 Nov. Services `plan-c-pilot-supervisor` and `plan-c-pilot-sender` are active.
- Every `service/**/*.py` in the image matches git `659ea29`. Checked with a per-directory sha256 manifest, plus a direct hash of openrouter.py, pilot_supervised.py, model_runtime.py and engine.py.
- Mounted guard `/srv/projects/twin-plan-c-pilot-20261109/supervision-v11` (at /run/guard): every file matches `ops/plan-c-supervision@659ea29` except `guard_core.py`.
  - The only change there is early stop 300→400, with the comment "Jesse approved 2026-11-11".
  - That file is read only by the host supervisor and the monitor, not on the reply path.
- Env: primary deepseek/deepseek-v4.1-flash, upstream relace/fp4, fallback disabled. ENGINE_MODEL_MAX_ATTEMPTS is unset, so it defaults to 2. transport_retry_seconds = 30 (default).
- Scope: id 1, revision 33, status active, active_since 23:51:12Z on 10 Nov, authorization_mode autonomous.
- Container env `BUILD_SHA=f60e656…` is a stale base-image label. The code itself matches 659ea29.

## 3. Verified incident timeline (DB: twin_plan_c_pilot, account of-323358988)

| Brisbane | Event |
|---|---|
| 7:35:37 am | Fan 318022380, request 6122ac90, call 1ffbd668: wire started, then **released http_429** 0.33 s later. The previous call was 43 s earlier. **Still open/prepare_reply.** |
| 7:38:30 am | Fan 601164546, request b968a156, call 27e1398e: **released http_429**. **Still open/prepare_reply.** |
| (both) | The fans kept typing. Later messages were answered by new requests. These two were never superseded. They ran on the earlier activation (scope revision 23), with the same scheduler filter (in place since 9eb8d8f, 8 Nov). |
| 9:51 am | Current activation (active_since 23:51:12Z). |
| 5:57:23 pm | Inbound 12024062326744. Request 167ce6b3 at 5:57:26. Call e732090c was **settled/invalid** (5:57:31). Corrective call c2d6107e was **verified_ok** (5:57:35, chain_pos 1 "retry" step). 3 reply bubbles were sent at 5:57:38, 5:58:41 and 5:59:43, each with a `sent` delivery receipt. Request completed. |
| 6:00:55 pm | Inbound **12024091960310** (ingress applied 6:00:56.65). |
| 6:00:58.4 | Request **0165083a** created (open / prepare_reply / rev 0). |
| 6:00:58.6 | Call **b4f7d339** reserved (446 µUSD); wire started at 6:00:58.625. |
| 6:00:59.6 | Call **released, outcome http_429**, actual cost null, no provider_generation_id. Lease would have expired at 6:02:28. |
| 6:00:59 → now | **No further model call, decision, approval, intent, handover or cancellation** for 0165083a. It is unchanged at revision 0 as of 6:58 pm. |
| 6:13:30 pm | Inbound 12024229540311 ("Baby?"). Request **644ee97c** at 6:13:35. Call 93808045 was verified_ok (6:13:41). 4 bubbles were sent at 6:13:44, 6:14:47, 6:15:49 and 6:16:52, all with `sent` receipts. Request completed. Its prompt history (the last 101 canonical messages) included 12024091960310, so this is a **new request answering with earlier context, not a retry**. I did not read the private text to judge whether the reply addressed the 6:00 pm message. |
| 6:19–6:24 pm | 4 more inbound messages and 4 more verified_ok calls. Some parts were cancelled as `newer_inbound` / `memory_changed` (normal supersession). |
| 6:25:42 pm | **Separate open issue.** Request 63c74441 is `waiting_provider / reconcile_existing_action`. Intent 6d566ed7 is `unknown / invalid_receipt`, with no delivery row. 2 further parts are `ready`. Whether the fan received that bubble is unproven. |

**Call vs bubble counts.**
- From 5:57:24 to 6:13:35 pm: 3 inbound messages, 4 AI calls (2, 1 and 1), 7 reply bubbles.
- That matches "4 attempts for 3 messages" for that span. Any 40-minute window that includes 6:19 pm has 5 calls for 4 inbound messages.
- Gap before the 6:00 pm 429: 3 min 27 s since the previous call. Before the 7:35 am 429: 43 s. Before the 7:38 am 429: about 3 min.
- Pilot totals: settled/verified_ok 28, settled/invalid 3, released/http_429 3, identity_verified 1, length_limited 1.

**Other blockers ruled out for 0165083a (DB plus code):**
- No approval row exists, so `existing` is false.
- No ready decision.
- Owner rows are active.
- Scope is active.
- Budget state was fresh (estimate 289 against early stop 400).
- The same worker id (w-1-c0192161d142) made the calls at 5:57, 6:13 and 6:19 pm, so the worker, gate and budget were live.
- The ingress cursor clears itself on an empty page, so it cannot hold a request out permanently.
- There is a single worker process, so no lease conflict (the lease ran 6:00:58 to 6:02:28 and the row is `released`, not `calling`).

The only clause excluding the request is `uncertain` (see section 4).

## 4. Source references and proof

### Read in code (659ea29, byte-identical to deployed)

- `service/model_transport/openrouter.py:206-207` raises `ModelHTTPStatus(response.status_code)` for a non-200 response. The body, `error.metadata` (provider_name, raw) and the Retry-After / X-RateLimit-* headers are all discarded. Nothing in service/ reads Retry-After.
- `service/conversation_engine/model_runtime.py`:
  - `propose()` catches `ModelHTTPStatus` and calls `ledger.release(outcome='http_429')`, then adds the model to `dead` for this pass.
  - Lines 209-216: on a later pass, a transport failure newer than `transport_retry_seconds` (30 s) blocks the model. An older one does not, so a retry would be allowed.
  - Line 326: raises `ModelUnavailable('model is unavailable; the request stays open')`.
- `service/conversation_engine/model_client.py:77-78`: `class ModelUnavailable(Refused): "…the request stays open and may be retried later."`
- `service/conversation_engine/model_ledger.py:290-293`: `release()` sets status `released`. The reservation is not charged, so the ledger treats a 429 as definitive and zero-cost.
- `service/model_transport/pilot_supervised.py`:
  - Line 322 comment: "Unknown model calls are retained, never automatically retried."
  - Lines 345-352, `uncertain`: status in (reserved, calling, unknown), **or** status `released` AND (outcome null OR outcome != 'lease_expired' OR wire_started_at not null), or identity unsettled.
  - Line 365: work requires `~existing AND (ready OR ~uncertain)`.
  - Result: a released http_429 (wired, outcome != lease_expired) counts as uncertain, so it is never scheduled.
- `service/conversation_engine/engine.py:381-388`: supersession of an old request happens only inside `plan()` for **that** request. An excluded request is never superseded.
- History: before `9eb8d8f` (8 Nov), the filter excluded **every** `released` call. 9eb8d8f relaxed it only for never-wired lease_expired. The defect has existed since the pilot scheduler was written.
- Existing test gap: `service/tests/test_engine_model_pg.py:366` proves 429 recovery only through direct `engine.plan()` calls with a moved clock. No test drives the scheduler.
- `ops/plan-c-supervision/{supervisor,collect_monitor_evidence,db_audit}.py` have no check for open requests with no in-flight work.

### Ran (offline, Work Server, isolated)

- Workspace `/srv/projects/claude-429-repro-20261111`, made with `git archive 659ea29`. The shared repo, the other builder's tree and all shared containers were untouched.
- Runtime: the deployed image family `twin-plan-c-pilot:commit-659ea2913a7e` (Python 3.12.16), with source mounted at /app/service. pytest was installed into the workspace `.pytest_pkgs`.
- Postgres: a throwaway `claude-429-repro-pg` (postgres:16, 127.0.0.1:55499). It has since been removed.
- Model provider: an `httpx.MockTransport` injected as `model_transport=` into the real `build_supervised_pilot`, so the real OpenRouterClient and openrouter.py ran. FansAPI was mocked with MockFans. Signed HTTP intake was real. All data was synthetic.
- Clock: offset on `ModelRuntime._clock` and `ModelLedger.clock`.
- Command: `run-tests.sh <pytest args>` (`SRC=service_exp FIX=1` for the experiment).
- Test file: `service/tests/test_claude_429_scheduler_repro_pg.py` (12 tests). Patch: `experiment/uncertain-http-release.patch`. Logs: `logs/claude429_*.log`.

Results on **unmodified 659ea29** (12/12 pass, each asserting the defect):

| Test | Observed |
|---|---|
| A: 429, then ticks at +0 s ×2 and +31 s, +120 s, +600 s, +3600 s (×4 each), provider now healthy | 1 provider call total. The request stays open/prepare_reply, handover none. 0 decisions, 0 posts. Every tick after the first planned `[]`: **never selected**. No exception escapes tick(). |
| B: first failure 503 / 400 / 401 / connect-error (`not_sent`, wired) | Same defect for each: never retried, silent. **Not 429-specific.** |
| B: timeout (status unknown) | Never retried (by design). |
| B: never-wired lease_expired | Recovers: 1 call, completed, 1 post. |
| C: newer message after the 429 | New request answered. Old request **stays open, never superseded**. |
| D: 429, 429, ok | Stays at 1 call, open, silent. |

Results with the **experiment patch** (an `http_[0-9]{3}` release is not uncertain), 12/12 pass asserting recovery:
- 429/503/400/401: exactly 1 retry after the 30 s cooldown, then completed with 1 post.
- Inside the cooldown: no provider call, no exception.
- 429 then 429: exactly 2 calls, then `handover_pending / await_handover_acceptance / operator`, 0 posts. The limit holds and nothing loops.
- Newer message: the old request becomes `cancelled / superseded_by_newer_input`, the new one completes. 1 post, no duplicate.
- `not_sent` and timeout are unchanged (still never retried).

Regression check, 8 existing suites (q2_n2 crash recovery, auth recovery, provider resolution, v3 retry authority, offline completion, plan_c_supervised, u4 handover, model canonical): 103 passed, 1 failed, on **both** baseline and patched trees. The 1 failure (`auth_recovery … concurrent_recovery[autonomous]`) is a harness artefact: `str(engine.url)` masks the password. It passes on both trees with trust auth. test_engine_model_pg.py on baseline: 87 passed.

**Not run:** the narrower recommended variant, concurrent workers, worker restart mid-cooldown, stop/takeover during a pending retry, production-shaped data (the 3 stuck rows), and Retry-After handling.

## 5. Errors and omissions in ChatGPT's diagnosis

ChatGPT got right:
- The core mechanism. The line references check out: openrouter.py:206-207, pilot_supervised.py:345-352 and :365 (it cited 364-365), model_runtime.py:209-216.
- That the scheduler, not the engine, blocks the retry.

Omissions or errors:
1. **Not a single incident.** The 7:35 am and 7:38 am 429 requests are stuck in the identical way and are still open. It compared the earlier 429s but did not note they were never recovered either.
2. **Not 429-specific.** Every wired release is blocked: all http_* codes and also `not_sent` (connect failure after `mark_calling`, which is provably not delivered). A fix keyed only to 429, or only to http_*, leaves `not_sent` silently stuck.
3. **Silent, not just unretried.** The excluded request is never superseded, cancelled or handed over, and no monitor checks for it. Even in the cases where retry is correctly refused (timeout/unknown), a fan message can sit unanswered with nobody told. This visibility gap is a separate defect, and removing the filter alone does not close it.
4. **Removing the condition alone is not safe as-is.** A broad "http_* is not uncertain" also re-runs 400/401/403/404/422 (config or auth errors) once after 30 s, because propose() only cools down those failures and does not refuse them.
5. **Retry-After** is discarded, but nothing would use it today: the cooldown is a fixed 30 s. Saving it matters for evidence. Honouring it is optional.
6. **The engine/scheduler contract contradiction** (`ModelUnavailable` "may be retried later" vs the scheduler's "never") and the test gap (only direct `engine.plan()`) were not stated. They are why this passed review.
7. **Missed a live item:** request 63c74441 (6:25 pm) has a delivery in `unknown / invalid_receipt` with 2 parts queued behind it.

## 6. Minimal proposed repair (not yet proven in the narrow form)

**R1, scheduler eligibility (pilot_supervised.py `uncertain`):**
- Treat a `released` call as **not** uncertain when its outcome is a definitive availability failure: `not_sent`, `http_408`, `http_429`, `http_5xx` (the same set as `model_runtime.is_availability_failure`, minus `timeout`, which is never `released`).
- Everything else keeps its current treatment: reserved/calling/unknown, timeout, identity-unsettled, and other wired releases.
- Retry timing, attempt limits, reservations, owner/stop checks and sending all stay with the existing `turn()` → `plan()` → `propose()` path. The scheduler only regains the right to re-ask.

**R2, non-retryable HTTP failures go to a human:**
- When a request's only calls are released with a non-availability http_* code (400/401/403/404/422), hand it over through the existing `turn.handover(...)` route instead of leaving it silently open. Today propose() would retry those once after 30 s.
- Alternatively keep them excluded, but R3 must then surface them.

**R3, operator visibility:**
- The supervisor/monitor should raise an incident for any scope-era `open` request with no in-flight call, no ready decision, and age above N minutes (for example 5).
- This covers timeout/unknown holds and any future exclusion.
- It must alert only, not retry.

**R4, evidence (openrouter.py:206-207):**
- On a non-200 response, keep a bounded, secret-free record in the call's `error_class`/evidence: status, OpenRouter `error.code`, `error.metadata.provider_name`, a truncated and redacted `error.message`, and the `Retry-After` and `X-RateLimit-*` headers.
- Never store the prompt.
- Optionally use `max(30 s, Retry-After)` as the cooldown.

**Effects check:**

| Concern | Effect |
|---|---|
| Retry delays and attempt limits | Unchanged and still in propose(): 30 s cooldown, max 2 calls per generation, then handover. Proven offline for the broad patch. |
| Restart and concurrent workers | The ledger's `expected_seq`, lease and `ModelBusy` are unchanged. **Not tested concurrently.** |
| Newer messages and corrections | `plan()` supersedes stale requests before any model call. Proven offline (cancelled, no call, no duplicate). |
| Stale or duplicate replies | `turn.recheck()` and delivery idempotency are unchanged. One probe showed no duplicate. |
| Stop, takeover, account isolation | Unchanged gates in `turn()`/`plan()`/`_send`. **Not tested with a pending retry.** |
| Unknown model outcomes and costs | Still held, never retried (timeout probe unchanged). |
| Refusals and content failures | They are `settled`, not `released`, so unaffected: still `_reject` and handover. |
| Exhausted retries | `handover_pending` (proven). Operator visibility needs R3. |

**Deploy note (when authorised):** after R1, the 3 stuck requests become eligible. Each has newer inbound from its fan, so `plan()` should cancel them as superseded with no model call and no send. This is read from code, not tested on production-shaped data. Prove it on a dump copy first.

## 7. Acceptance tests required before calling this fixed

1. The existing `test_claude_429_scheduler_repro_pg.py` passes in fix mode, **using the narrow R1** (rewrite the experiment clause to `^(not_sent|http_(408|429|5[0-9]{2}))$`).
2. New: wired `not_sent` → one retry after cooldown → reply.
3. New: http_400/401/403 → handover_pending (R2), with 0 extra provider calls.
4. New: two workers ticking concurrently over one 429'd request → exactly 1 retry call and 1 delivery.
5. New: worker restart between the 429 and the cooldown expiring → exactly 1 retry.
6. New: stop/takeover/handback applied during the cooldown → 0 retry calls and 0 sends.
7. New: second failure → handover_pending, and the operator surface shows it.
8. New (R3): a timeout/unknown hold older than N minutes raises a supervisor incident and does not retry.
9. New (R4): a 429 with a JSON error body and Retry-After → evidence fields recorded, no secret or prompt stored. Malformed or huge bodies are bounded.
10. Copy of the live pilot DB (`pg_dump` into an isolated container): run ticks with the mocked provider. The 3 stuck requests must become `cancelled/superseded_by_newer_input` with **0 model calls and 0 sends**.
11. The full existing Plan C PG suites and test_engine_model_pg.py stay green on the exact candidate image.

Only after 1-11 pass on the exact image can the repair be called proven offline. A real-provider 429 cannot be safely forced, so live confirmation will only come from the next natural 429. No claim of zero future errors is made.

## 8. Missing evidence for the original 429

- Response body (OpenRouter `error.code/message/metadata.provider_name/raw`): discarded.
- Headers (Retry-After, X-RateLimit-*): discarded.
- Provider generation id: none issued for a 429. The `/generation` lookup is impossible.
- Worker logs: none.
- OpenRouter account activity/logs for 08:00:58Z (6:00:58 pm Brisbane): needs owner dashboard access. This was not checked, and failed requests may not be listed there.
- Other workloads on the same `pilot-worker-openrouter-reused-v1` key at that minute: not visible.

## 9. Not dealt with here

- Request 63c74441 / intent 6d566ed7, `unknown / invalid_receipt` (6:25 pm), needs its own reconciliation check before more sends to that fan.
- Several older requests sit `open` with next_step `review_changed_conversation` / `review_delivery_capacity`. Whether a person sees them is a question for R3.
