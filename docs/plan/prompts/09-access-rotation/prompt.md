# 09 · Access register + MFA (human wizard)

The team stays and keeps coding, so this is a register, not a purge. Goal: every credential is listed with holder + location; MFA and spend caps are on; only leaked keys (gitleaks/trufflehog hits) get rotated. Agents are denied `aws`, `ssh`, prod Supabase; this section is a **human-run wizard**: AI writes the script and the register, you run the steps.
GAPS.md rows: 1, 39, 40, 41.

AI does:
1. From `CONNECT.env.example`, `docs/TAKEOVER-*.md`, `.github/workflows/**` (secret names only), `twin-ops`, and the team's `.env.example` files, produce `docs/ACCESS-REGISTER.md` from `ACCESS-REGISTER.template.md`: one row per system (GitHub org, Supabase projects ×2, AWS account/IAM/SSM, VPS boxes + Tailscale, BrowserBase, Telegram bots, OnlyFans creator sessions, OpenRouter, ElevenLabs, Deepgram, DeepSeek, Grok, DNS/registrar, domain mail, Docker registry/ECR, any SaaS in invoices). Columns: who has it · how to revoke · how to rotate · where the new value lives (GitHub secret / local .env / SSM) · rotated-on · verified-by.
2. Generate `scripts/access-wizard.sh` with the `mattpocock-skills:wizard` skill: one step per row; each step prints the exact console path or CLI, waits for `done`, and appends the date to the register. Never executes a rotation itself.
3. Write `HANDOVER-ASK.md` (what to ask the departing devs): known bugs, in-flight work, env inventory (every box, cron, bot), recorded 30-min walkthrough per repo, 2-week Q&A clause, IP assignment confirmation, list of every place a key lives.

You do (order matters: lock the doors, then change the locks):
1. GitHub: org 2FA enforced; remove departed members/outside collaborators; review deploy keys + PATs (`gh api orgs/<org>/...`); rulesets (section 10) before rotation so nobody re-pushes.
2. AWS: root MFA + billing alarm; IAM credential report; delete departed users' keys; CloudTrail: any API call from old keys after the departure date = incident. Rotate the GH Actions deploy keys last (workflows break until updated).
3. Supabase (both projects, console only — never from a dev shell): migrate to `sb_secret_*`/publishable keys, one secret per component; deactivate legacy JWT-based keys after every consumer switched; reset DB password; org MFA; network restrictions; SSL enforce.
4. VPS/boxes: `authorized_keys` + sudoers per box; Tailscale ACL: remove old devices; rotate `.env` on box via the team's `twin-pull.sh` path.
5. Telegram: BotFather `/revoke` per bot → update secret → restart via tick path. Creators: re-login OF sessions (sessions are credentials).
6. AI/paid APIs: new key per service, old deleted, spend caps set.
7. `.env` escrow: password manager, two holders; `prod-ops` profile for the one human who touches prod (separate shell user, MFA, `script` logging of the session).

Done when: every register row has `rotated-on` and `verified-by`; `gitleaks`/`trufflehog --only-verified` on all repos find 0 live keys; CloudTrail/Supabase logs show no old-key use for 7 days (add this to the 11h sweep WATCH for 7 d).
