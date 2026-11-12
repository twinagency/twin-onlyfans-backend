# Browserbase login capture
- What: `POST /onlyfans/connection/v1/accounts/{id}/browserbase-login` starts a Browserbase browser; human logs in via live view; job returns `{capture,xHash,ownerId}` (same as capture-from-curl). Poll `GET .../{jobId}`, stop with `POST .../{jobId}/cancel`.
- Why: link an account without the extension or DevTools, still no password typed into Fastt. Writes nothing; dashboard runs normal save+verify.
- Verify by hand: set `BROWSERBASE_API_KEY`/`BROWSERBASE_PROJECT_ID`, start a job, open `liveViewUrl`, log in, poll until `done`, check `ownerId` matches the account.
- Breaks: OF rolls web revision (interceptor misses static_param -> falls back to a stored one or fails); Browserbase API changes; `loginExtension/interceptor.js` missing from the image.
- Concept: CDP `connect_over_cdp` + init scripts let a remote browser publish the same signed request the extension captures.
