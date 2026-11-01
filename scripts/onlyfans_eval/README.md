# Private model evaluation

Use reviewed private cases and the configured DeepSeek/OpenRouter route. Keep outputs outside the repository. Synthetic results test the harness and guards; they cannot qualify model quality.

The real transport requires `--allow-spend --max-usd 2 --budget-state /private/eval-budget.json` and the configured key environment variable. Use that **same journal path and cap for both routes, repair calls and every resume**. Run routes sequentially. Use a dedicated test credential with the same provider-side $2 limit as the final billing safeguard. No creator sending credential or customer transport is needed.

The journal reserves a conservative text-input and capped-output amount before HTTP, then settles the provider's `usage.cost`. It preserves production model/provider selection and adds a provider price ceiling. The planning estimate still uses approximate token counts; it is not spend authority or a guarantee of completing every planned generation under the cap. Missing/invalid cost, transport failure or a crash retains the reservation and blocks further spending. Reconcile that exact request with provider billing before changing an unresolved journal or lock. Do not delete it to obtain a fresh allowance.

Record source commit, case hashes/cohorts, route, generations, model/provider, prompts, raw and post-guard replies, usage/cost, held cases and independent rubric scores. Report inappropriate replies and excessive suppression separately. The journal contains no prompts or credentials and is local evaluation bookkeeping, not PostgreSQL integration evidence.
