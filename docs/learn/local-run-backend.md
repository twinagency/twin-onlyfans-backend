# Local offline run of the OnlyFans engine (measured 2026-11-11, commit d10e065)
Prereqs: Docker Desktop, python3.12, no `service/.env`. Loopback only; no real key anywhere.
## One-shot: `./init.sh`
Starts/reuses PG container `twin-of-test-pg` (127.0.0.1:55432, trust), creates `.venv` from `requirements-onlyfans-test.txt`
(= requirements.txt + pytest), creates db `twin_of_app`, runs `python -m db.schema bootstrap` (head `0133_preparation_retry_state`;
the PG api role uses `db.schema`, CLAUDE.md's "no alembic" rule is for the legacy SQLite relay).
## Tests (from `service/tests`; every test clones its own throwaway database)
```
. .venv/bin/activate; cd service/tests
export ONLYFANS_TEST_PG_URL=postgresql+psycopg2://postgres@127.0.0.1:55432/postgres ONLYFANS_TEST_PG_EMPTY_TEMPLATE=1 PYTHONPATH=../..:..:.
nice -n 10 python -m pytest -x -q --durations=10 <file>      # test_concurrent_delivery_pg.py is a plain-assert script: run `python <file>`
```
Measured: commercial_preparation 14 passed/31 s · conversation_holds 40/106 s · concurrent_delivery 15 checks/4 s (script; pytest rc=5, 0 collected)
· plan_c_model_retry_due 21/50 s · plan_c_supervised 19/35 s · plan_c_length_replay 29/33 s. Full suite (~1 h) not run.
## Boot the OF API (legacy `service.server:app` refuses when ONLYFANS_PROCESS_ROLE is set; use `onlyfans_app`)
```
cd service && env -i PATH="$PATH" HOME="$HOME" PYTHONPATH=.:.. ONLYFANS_PROCESS_ROLE=api TWIN_FASTT_CONFIG_ISOLATED=1 \
 DATABASE_URL=postgresql://postgres@127.0.0.1:55432/twin_of_app ONLYFANS_{API,CONTROL,CONFIGURATION,CAMPAIGN,CONTENT,CONNECTION}_TENANT=local-test \
 ONLYFANS_<same six>_TOKEN=<six distinct dummies> ONLYFANS_CONNECTION_KEY=<64 hex dummy> nice -n 10 ../.venv/bin/python -m uvicorn onlyfans_app:app --port 8787
curl -s localhost:8787/healthz   # measured {"ok":true,"role":"api"} 200
curl -s -H 'Authorization: Bearer <api token>' -H 'X-OnlyFans-Schema-Version: of-preview.v2' localhost:8787/onlyfans/api/v1/ready  # 200 schemaReady
```
`/openapi.json` is disabled (`openapi_url=None`); `app.routes` = 119, 118 under `/onlyfans/*` (configuration 29, content 26, control 16,
api 14, content-sync 14, offer-import 8, connection 6, campaign 4, webhooks 1).
## Mocked / why nothing leaves the box
Measured: `lsof -a -p <uvicorn> -i` = LISTEN :8787 + two conns to 127.0.0.1:55432 only. Code gates: real FansAPI sends need
`TWIN_ENGINE_FANSAPI_REAL=1`+`TWIN_ENGINE_FANSAPI_ENV_FILE`; real preparation needs `TWIN_FANSAPI_PREPARATION_REAL=1`+`TWIN_FANSAPI_PREPARATION_REGISTRY`;
model calls need `ENGINE_MODEL_*` (PROVIDER, PRIMARY, KEY_ENV|KEY_FILE, UPSTREAM, BASE_URL) else `ModelConfig.enabled=False`; draft pilot needs
`TWIN_PLAN_C_DRAFT_ONLY=1` else `None`. Tests use `httpx.MockTransport`/synthetic transport; `run-isolated.py` also pins `socket.connect` to loopback.
Not offline: `docker run postgres:16` pulled the image once. Not run: legacy SQLite relay, worker role, dashboard. Docker Desktop was hung
(backend up 3 d, VM unreachable) → quit + relaunch fixed it.
## To link a test account you need
Native path, `/onlyfans/connection/v1` (bearer `ONLYFANS_CONNECTION_TOKEN`, header `X-OnlyFans-Connection-Version: of-connections.v1`):
`POST /accounts` {creatorId, creatorName, ownerId, handle, actor, commandKey} → `POST /accounts/{id}` action=save payload {ownerId, capture, xHash,
proxyUrl, expiresAt, model{modelId, apiKey}|null} → verify → activate {limitUnits, throttleSeconds, mediaEnabled}; or `POST /enrollments`
{creatorId, creatorName, handle, session, actor, commandKey[, initialProxyUrl]}. Env: `ONLYFANS_CONNECTION_KEY` + six tokens/tenants.
FansAPI path: `POST /onlyfans/configuration/v1/accounts/{id}/preparation` action=configure config {contract_version:"preparation.v1", provider_account,
creator_id, qualification{mode,reference}, limits{provider_requests, provider_credit_budget, provider_credits_per_request}}; real wire = per-account
0600 env file outside git with `FANSAPI_KEY`, `FANSAPI_ACCT`, `FANSAPI_BASE` (app.onlyfansapi.com|api.fansapi.com), listed in the registry JSON
[{tenant, account, provider_account, creator_id, env_file, limits}] + TWIN_* flags; webhooks `POST /onlyfans/webhooks/plan-c-draft`
(`TWIN_PLAN_C_WEBHOOK_SECRET_FILE`). Two accounts = two registry rows, two env files, distinct provider_account/creator_id.
