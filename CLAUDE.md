# CLAUDE.md — orientation for an AI agent working in this repo

You're in **Fastt** (repo name `inflowwkiller`): a self-hosted creator dashboard
for OnlyFans. A FastAPI relay holds a captured browser session, replays signed
requests to OnlyFans via `curl_cffi`, and serves a Next.js UI for messaging,
mass-messaging, vault, queue, posts, stats, realtime events, and per-account
automations. SQLite store + an in-process automation executor that boots inside
the relay.

```
Next.js UI (app/) --fetch /api/relay/...--> FastAPI relay (service/server.py)
  --OFClient signs each call--> curl_cffi(impersonate=chrome) --> onlyfans.com
```

## Layout

| path | what |
|---|---|
| `service/` | relay: `server.py`, `of_client.py`, `of_signer.py`, `of_ws.py`, `session_bootstrap.py`, `automations/`, `db/` (SQLAlchemy models + alembic migrations) |
| `app/` | Next.js dashboard (chat, inbox, vault, setup, automations) |
| `loginExtension/`, `noTeleportLoginExtension/` | Chrome extensions that capture the OF session (no password typed into Fastt) |
| `scripts/` | ops: `deploy-vps.sh`, `start.sh`/`stop.sh`, `run-tests.sh`, `diag.sh`, `setup-caddy.sh` |
| `Dockerfile`, `docker-compose.yml` | container build + cloudflared tunnel sidecar (`--profile tunnel`) |

## Deploying to a VPS (the common ask)

Two entry points, both clone this public repo and bring up the Docker stack (no
fork needed unless deploying modified code). By **default** the dashboard is
exposed as plain http on the server's own IP (`http://<ip>:3000`) — zero config.
A prefix switches how it's exposed: `DOMAIN=you.duckdns.org` (or your own domain)
routes through the box's Traefik with a real Let's Encrypt cert (needs the
Hostinger **n8n plan**, which ships Traefik); `TUNNEL=1` gives a throwaway
trycloudflare URL with no DNS/Traefik; `PORT=` picks a different host port.

- **On the VPS itself** (simplest — Hostinger browser terminal, no laptop/SSH):
  `scripts/deploy-here.sh`. Runs non-interactively — session secrets are
  auto-generated, no proxy is needed for a single account, and the DeepSeek key
  is optional (the dashboard boots without it). One-liner:
  `bash <(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)`
- **From a laptop over SSH:** `scripts/deploy-vps.sh` — the user edits one line
  (`SSH_TARGET` → `root@<ip>`); repo URL + branch `main` are pre-filled. Use this
  path when copying up an existing database.

A **fresh single account needs no secrets and no proxies** to get running.
DeepSeek is only required to switch on AI auto-messaging; proxies only matter
once multiple OF accounts share one server. Full exposure options live in
**deploy/README.md**; read **DEPLOY.md** (illustrated) and **DEPLOY_HOWTO.md**
(dense reference) before deploying. Redeploy = re-run the same script (idempotent:
pulls, rebuilds, preserves `.env` + data).

## Rules you must not break

- **Never commit secrets or state.** `.env`, `service/.session_secret`,
  `service/.chatter_secret`, `credentials.json`, `token.json`, every `*.db`,
  `service/sessions/`, and `service/proxies.json` are gitignored and contain
  live cookies / proxy creds / fan PII. They are written on the host only. Do
  not add them to a commit, a PR, or any doc — and never paste real account IDs,
  fan IDs, proxy creds, or session blobs into this repo (it is **public**).
- **Schema heals itself on boot — do NOT run alembic on a fresh DB.** `init_db`
  (`service/db/engine.py`) runs `create_all` + an additive nullable-column
  catch-up. A brand-new `chatterly.db` comes up fully formed. Running
  `alembic upgrade` on a self-healed DB can fail with duplicate-column. Run
  alembic **only** when bringing an *existing* DB that has data and is missing
  new columns — and note alembic reads **`CHATTERLY_DB_URL`**, not
  `DATABASE_URL`; point both at the same file.
- **Fan-messaging automations go live the instant they're enabled.** Auto-reply,
  welcome, follow-up, nudge, and funnel automations send to real fans on enable.
  Never enable one without confirming which account it targets. Posting/unsend
  automations are safe to leave on.
- **One stack per box by default.** Container names (`chatterly-relay`,
  `chatterly-app`, `chatterly-tunnel`) and host ports (`8787`/`3001`) are fixed —
  two copies on one VPS collide. Use a fresh VPS, or rename containers + ports in
  `docker-compose.yml` first.

## Gotchas

- With `TUNNEL=1`, the `trycloudflare.com` URL **rotates on every restart** — fetch
  the current one from the tunnel logs (the deploy script prints the command). The
  default IP URL and `DOMAIN=` (Traefik) URLs are stable.
- **Adding a relay route needs a matching rewrite** in `app/next.config.ts`, or
  Next 404s before the request reaches the relay.
- In containers the relay runs **without `--reload`** — restart it
  (`docker compose restart relay`, or rebuild) after editing relay code.
- A captured session goes stale when OF rolls its web revision — one extension
  re-capture (~30s) fixes it. The relay never falls back to direct egress if an
  assigned proxy is down (`/health` shows the proxy block).

## Dev & tests

```bash
# native dev
./venv/bin/uvicorn service.server:app --reload --port 8787   # relay
cd app && pnpm dev                                           # UI on :3001

# tests (plain-assert scripts, not pytest)
./scripts/run-tests.sh
```

<!-- twin-ai-kit: appended by install.sh; edit freely, do not delete marker -->
## AI operating rules (twin-ai-kit)
- Read `handover/TEAM-BRIEFING.md` (scope, start branches, hard rules, engine model) and `REVIEW.md` before any review or fix (root canonical, `.claude/REVIEW.md` identical mirror). Read `.claude/orchestration.md` before delegating. Two levels only.
- Local Supabase only (`127.0.0.1:54321/54322`). Prod project refs are denied in `.claude/settings.json`; a denial is a stop, not a workaround. Never prod, never paid APIs, never real sends.
- Branches: start from `handover/TEAM-BRIEFING.md` §2; own branch `jaka/<topic>-<date>`; never push `main` (deploys prod), never force-push `claude/*`/`codex/*`, never base on `codex/onlyfans-of-b1`. PRs only. Say which files you will touch before starting (team's Claude edits the same areas).
- Every task: own branch `jaka/<topic>-<date>` from the current working branch; commit/push there; before PR squash to one commit per topic, `git rebase origin/<working>`, re-test, `git push --force-with-lease` (own branch only). PR → working branch, never `main`.
- Tests: throwaway Postgres on 127.0.0.1:55432 only; failing test first; report suites run/not run with counts.
- Volume/concurrency change → scenario in the ONE `slop/` harness; `slop/run --changed` green; quote the evidence folder by name, never `latest`; FAULT pick → `--break` CAUGHT.
- Scheduled things live in T3 on the owner's Mac (`schedules/`), never as new cron in this repo (`tools/tick.ts` is the only cron; bot schedules in `tracker_bots.every_minutes`).
- Post-deploy watch: for every shipped behaviour append `- <id> | added YYYY-MM-DD | expires +3d | <WATCH_CHECKS name> | <PR url>` to `docs/health/WATCH.md`; no fitting check → add one in the same PR.
- Review windows (W1, W2, …): `.scratch/rwl-W<N>/` COMMON/PROMPTS/STATE/HANDOFF; lanes are top-level threads; deploy after the window.
- Reports to the team's tracker use `LOW LEVEL` / `HIGH LEVEL` (team CLAUDE.md); the operator cannot open files.
- After each task write `docs/learn/<slug>.md` (≤10 lines: what, why, verify by hand, what breaks it, concept to learn).
- Report ends: `Ship: PR <url> · merged <sha|no + why> · deployed <yes|no + why|n/a>`.
