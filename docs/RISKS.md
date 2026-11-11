# RISKS — twin-onlyfans-backend (reviewed weekly, CHECKLIST §D)

L/I = likelihood/impact (H/M/L). Owner "?" = OWNER TO FILL. Seeded 2026-11-11 from docs/TAKEOVER-20261111.md §6 + handover open items.

| id | risk | L/I | owner | mitigation | trigger | review |
|---|---|---|---|---|---|---|
| R1 | Local relay boot goes live: `service/sessions/` files present, automations send to real fans unless `CHATTERLY_TEST_MODE` | M/H | Jaka | always set `CHATTERLY_TEST_MODE`; never boot `service.server` with sessions present | relay started locally | 2026-11-18 |
| R2 | 12 tests in `service/tests/conversation_engine/` never run by `run-service-tests.py` | H/M | team | add recursive glob or document separate run | any engine change | 2026-11-18 |
| R3 | Host ≠ git: guard early stop 400 on host vs 300 in git; pilot pin script host-only | H/H | Ronan | owner reconciles host → git | any trial deploy | 2026-11-18 |
| R4 | Migration head/rank pins fragile across trial (≤0129b) and commercial (0133→0136) lines | M/H | team | features.json R06/R07 tests; pins listed per commit | new migration | 2026-11-18 |
| R5 | Drop keeps moving (step2-transition, commercial-integration branches) → windows stale | H/M | Jaka | pin windows by SHA; check refs before each window | new origin push | 2026-11-18 |
| R6 | Plaintext key list `~/twin/API-KEYS-2026-11-06.txt` on owner laptop; keys maybe pasted in chats | M/H | Ronan | move to Vaultwarden, rotate if shared | — | 2026-11-18 |
| R7 | Third-party billing owners unrecorded (FansAPI, OpenRouter, AWS, DO, GitHub, Cloudflare, ...) | M/M | ? | owner records billing email per account | renewal / card failure | 2026-11-18 |
| R8 | FansAPI trial pricing ends ~2026-11-13 | H/M | Ronan | confirm plan + cost before then | 2026-11-13 | 2026-11-13 |
| R9 | Live trial: unconfirmed delivery (handover bug #2) + 429-stranded requests until R0 deployed | H/M | Ronan | reconcile before more sends; owner yes for R0 deploy | trial sends | 2026-11-18 |
| R10 | No snapshot on Work Server; restart owner unknown for non-pilot services | M/H | ? | owner configures snapshots, names restart owner | host loss | 2026-11-18 |
| R11 | `ops/plan-c-supervision` README/code carry live account/binding ids + tunnel URL | M/M | team | move ids to host config | repo made more public | 2026-11-18 |
| R12 | PRs fire ~8 old-pinned workflows; creating backend `main` fires ci.yml | M/L | team | decide before first PR / `main` | first PR | 2026-11-18 |
