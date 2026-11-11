# Handover ask-list (send to the team — only what we cannot find ourselves)

1. Push all current work to `handover/<date>` — not `main` (deploys prod), not the branches the workflows pin.
2. Known bugs + workarounds, one line each.
3. In-flight work: what is half done, what you would finish first.
4. Environment inventory: every box, container, cron, bot, scheduled job; who restarts it; where its `.env` lives.
5. Every place a credential lives (box, CI secret, laptop, password manager, chat) — names, not values.
6. Deploy + rollback steps as you actually do them, including the "do not do this" parts.
7. Third-party accounts and who owns the billing email.

Everything else (how to run locally, where logs are, architecture) prompt 00 derives from the code.
