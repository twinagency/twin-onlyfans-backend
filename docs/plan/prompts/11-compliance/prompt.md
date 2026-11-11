# 11 · Compliance + data handling (not legal advice — produces the questions and the stubs)

Goal: the legal/data exposures of an AI-chat product on OnlyFans for an EU team are written down, owned, and have a decision date. AI drafts; counsel and the owner decide. Nothing here is legal advice.
GAPS.md rows: 11, 12, 13, 14, 15, 43.

AI does (branch `kit/compliance`, docs only):
1. `docs/compliance/RISK-OF-TOS.md`: OnlyFans terms forbid AI chatbots writing chats/DMs (Reuters, 2025); `curl_cffi` browser impersonation is evasion → account bans; Oct 2026 C.D. Cal. ruling kept VPPA + UCL claims alive against agencies that disclosed fan PII to chatters. Sections: what we do · which term it touches · ban-recovery plan · disclosure stance options · questions for counsel · owner decision + date.
2. `docs/compliance/AI-ACT.md`: EU AI Act Art. 50 (transparency, in force 2026-08-02): people must be told they interact with AI. Applicability test (deployer in EU? users in EU?), disclosure design options, legal-opinion request text.
3. `docs/compliance/DPIA.md` (template, ICO structure): fan chats in an adult context reveal sex life/orientation → Art. 9 special category; large scale → DPIA mandatory (Art. 35(3)(b)). Data map (what, where, who sees, retention), risks, mitigations, residual risk, sign-off.
4. `docs/compliance/PROCESSORS.md`: one row per processor (Supabase, AWS, BrowserBase, OpenRouter, ElevenLabs, Deepgram, DeepSeek, Grok, Telegram, GitHub, Anthropic/OpenAI via T3): region, DPA signed?, transfer mechanism, data categories sent, kill date.
5. **DeepSeek lens gate** (gap 14): `scripts/review/scrub-diff.sh` — the diff handed to `delegate_task` DeepSeek is code-only: strips fixtures, `.sql` seed data, `*.json` test data, anything matching gitleaks or a PII regex (emails, phone numbers, @handles, names in `fixtures/`). Exit non-zero = DeepSeek lens skipped and reported unavailable, never "scrubbed by hand". Add the rule to `REVIEW.md`.
6. **Evidence hygiene** (gap 15): `slop/README.md` rule: screenshots only from seeded synthetic data; `slop/evidence/**` scanned with gitleaks + the PII regex before any upload; PR bodies carry the folder name, never the files.
7. **Retention** (gap 43): `docs/compliance/RETENTION.md` schedule per table; nightly purge job spec; slop invariant `deleted fan → 0 rows anywhere` (list every table with a fan id).
8. `docs/RISKS.md` register gets one row per item above (owner, likelihood/impact, trigger, review date).

Human: pick counsel, book the review, decide disclosure stance, sign DPAs. Dates into `docs/RISKS.md`.

Proof: scrub gate fails on a planted fake email in a fixture (break-it-once) and passes clean code; `docs/compliance/*` exists with owner + date fields filled or marked OPEN. `Ship:` line (docs PR, owner merges).
