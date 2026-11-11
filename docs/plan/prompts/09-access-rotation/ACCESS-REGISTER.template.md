# Access register — <org>

Names only. Never values. One row per system; one system per row.

| system | holders (people/machines) | revoke how | rotate how | new value lives | rotated-on | verified-by |
|---|---|---|---|---|---|---|
| GitHub org `<org>` | | remove member; `gh api -X DELETE /orgs/<org>/members/<user>` | PATs: user settings; deploy keys: repo settings | n/a | | |
| Supabase `<prod ref 1>` (live) | | console → Settings → API → deactivate legacy keys | `sb_secret_*` per component; DB pwd reset | GitHub secret `SUPABASE_SERVICE_KEY`; box `.env` | | |
| Supabase `<prod ref 2>` (old prod) | | same | same | | | |
| AWS account `<id>` / profile `<profile>` | | IAM → delete access key; disable user | new key; update GH secrets `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` | GH secrets | | |
| EC2 `<instance>` SSM | | IAM policy | n/a | | | |
| VPS boxes (Tailscale only) | | `authorized_keys`, sudoers, Tailscale ACL | new keypair | | | |
| BrowserBase | | dashboard → API keys | key per env | `.env` `BROWSERBASE_API_KEY` | | |
| Telegram bots | | BotFather `/revoke` | new token | `BOT_TOKEN`, `ALERT_TELEGRAM_*` | | |
| OnlyFans creator sessions | | creator logs out all devices | re-login | relay store | | |
| OpenRouter / ElevenLabs / Deepgram / DeepSeek / Grok | | dashboard | new key + spend cap | `.env` | | |
| DNS / registrar | | | | | | |
| Docker registry / ECR | | | | | | |
| Password manager vault | | | | | | |
