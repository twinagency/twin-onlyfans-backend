# Owner's short answers to the 7-point ask-list (2026-11-11)

1. **Pushed work**: on the start branches (`handover/TEAM-BRIEFING.md` §2).
2. **Known bugs**: see `HANDOVER-ANSWERS-20261111.md` §2.
3. **Priority**: sales and onboarding are partly built. **Fix reply recovery first, then prove one full sale works.** → the team's Claude builds this; our job is review windows + the tests in TEAM-BRIEFING §7, not the fix itself.
4. **Environments**: development runs on the **Work Server** (DigitalOcean droplet); the **live CRM and the pilot run on AWS**.
5. **Credentials**: in protected server config files, incl. the pilot's `config/` folder on the AWS box. Other copies (laptop files, plaintext key list) and GitHub secrets **still need checking** → `docs/ACCESS-REGISTER.md` open rows.
6. **Deploy**: tested image + backup, **sending stopped during changes** → the kill switch (`OUTBOUND_PAUSED`) is already a deploy step; keep it, test it (TEAM-BRIEFING §7 row 1).
7. **Third parties**: GitHub, AWS, DigitalOcean, FansAPI, OpenRouter, Cloudflare. Billing emails still to confirm → `docs/RISKS.md`.
