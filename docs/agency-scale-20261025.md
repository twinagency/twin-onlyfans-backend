# Agency connections and capacity rollout

Connecting a creator reserves its proxy and automatically starts imports after verification. AI remains off until an authorized agency manager manually enables delivery. Public agency signup is out of scope.

## Required operator inputs
- Supply unused fixed HTTP/HTTPS/SOCKS5 proxies to the backend API as ONLYFANS_FIXED_PROXY_POOL_JSON, using a secure environment/secret store. Entries are objects with id and url. Never put real proxy credentials in Git.
- Supply a monthly AWS budget and a daily/per-agency AI budget before purchasing capacity or increasing live spending.
- Ten creator sessions must be signed in by their owners/managers, including any login challenges.
- Provider throughput must be measured independently; synthetic tests cannot demonstrate OnlyFans or OpenRouter capacity.

## Reservations
A transaction and deployment-wide advisory lock reserve one proxy per tenant and creator. Reservations and network fingerprints are unique. URLs are encrypted with the existing connection key, private to backend/sign-in service calls, and never returned to browsers. Retries use the stored reservation even if inventory changes. Existing connected proxies are excluded. No capacity returns a visible error instead of using the server IP. Reservations persist after cancelled logins; operators must reconcile abandoned reservations explicitly.

## Coordinated deployment
The candidate advances the application schema to 0092_proxy_assignments and preserves current limits/usage while allowing fractional throttle settings. Schema checks are strict: API/worker/supervisor must be released together after tests. Do not independently deploy an older image after migration. Preserve live Moxy scope and import changes when composing the final release.

FASTT_MAX_CONCURRENT_FANS defaults to 1 and permits 1..64. Increase only after validating worker resources, model quota, DB connections, delivery and takeover behavior. Turns are keyed by fan. Existing leases/claims and send admission remain authoritative. Engine activation is task-local. In-flight tasks drain before transport changes and shutdown.

Manual delivery controls accept minimum intervals from 0.1 seconds and total allowances up to 1000000. Existing settings, used units and cooldowns are retained. Allowances remain finite and never renew themselves; model spend limits still apply. These values do not prove upstream provider capacity.

## Capacity evidence
CI tests ten accounts with 50/500/1000 fans each (500/5000/10000 total), deterministic offered traffic of 100000 inbound messages/hour, a six-second synthetic model and 0.2-second fake provider. Reports include delivered/unanswered messages over 30 seconds. The real runner/ledger/PostgreSQL are exercised, but full prompt/media CPU work and live model/provider performance are not measured. Run an unscaled production-like benchmark and approved delivery canary before claiming the SLA.
