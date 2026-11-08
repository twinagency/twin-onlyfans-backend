> Historical record. Its provisional fan/product/approval settings are superseded for the current pilot. Do not use this document to configure or activate a worker. See [CURRENT-PILOT.md](CURRENT-PILOT.md) and the exact source/image qualification record.

# Plan C test configuration (decision register) - 2026-11-07
Ronan (owner) instruction 2026-11-07 evening: test only, reuse the already-connected OnlyFans account, invent any missing test details, choose the model, finish fast. This is a TEST configuration, not a production pilot allowlist.

- Creator/account: the existing connected test account (Maile) via FansAPI. Real ids come ONLY from env/secret files on the Work Server (LIVE_TEST_CREATOR_OF_ID, LIVE_TEST_FAN_CHAT). Never commit real ids; tests use numeric placeholders.
- Fan allowlist: exactly one test fan chat (Ronan test fan, already named in earlier approval). Nothing else.
- Product (invented): one product "Plan C test set": 3 media items, fixed price 6.00 USD (600 cents). Quantity in text must equal 3. No discount, no ladder, no second product.
- Approval: operator approves EVERY reply and EVERY card. No automatic approval, no automatic handback. Approval expiry 15 minutes after approval; pending draft expires after 60 minutes.
- Model: single model, no fallback, via OpenRouter transport. Chosen: deepseek/deepseek-v4.1-flash via OpenRouter (the writer already used by the live Moxy and V2 bots; Haiku rejected by Ronan because it will not do NSFW). Per-token prices must be read from the provider models list at run time and stored in config; if the id or prices cannot be confirmed, stop and report. Caps: AI total <= 5.00 USD (earlier Ronan cap), per turn bounded by max input/output tokens and total attempts 2. FansAPI cap: <= 300 credits total (earlier Ronan cap).
- Identity wording: none approved => any identity question goes to a human (handover), never answered by the model.
- Creator facts for the writer (invented, neutral): first name only, friendly, no location, no meet-ups, no off-platform contact, no customs promised.
- Live steps need Ronan present: only ONE agent/process sends live, only after offline qualification + independent review. No deploy to production; test runs from the Work Server release image only.

## Update (Ronan, owner, same evening)
- Fallback model: x-ai/grok-4.3 (grok-4.1-fast no longer listed on OpenRouter 2026-11-07; 4.3 is the cheapest Grok chat model, 1.25/2.50 USD per Mtok) as an OPT-IN fallback after deepseek/deepseek-v4.1-flash. Used only for provider-availability failures, never after an empty/refused/rejected output. Default OFF in release config; Ronan enables it for his test. This departs from the handoff line "disable alternate-model fallback" - recorded as a Ronan decision, Jesse decision not yet recorded.
- Simulation (when the joined build is ready): run the SAME inputs, catalogue (6.00 USD, 3 media) and fan script through DeepSeek and through Grok and compare writing. Real-worker path, fake delivery, human approval auto-simulated ONLY inside the labelled simulation, never in the product path.
