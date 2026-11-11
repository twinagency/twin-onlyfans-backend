# handover/ — what the team gave us (project-specific, not in generic/)

- `handover/TEAM-BRIEFING.md` — their agent briefing, condensed ~55 %; §7 maps every rule to a test we build.
- `OWNER-ANSWERS-20261111.md` — owner's 7 short answers: team priority = reply recovery, then one proven sale (their Claude builds it, we review/test); dev on Work Server, live + pilot on AWS; sending stopped during deploys.
- `HANDOVER-ANSWERS-20261111.md` — their long answers to our 7-point ask-list, verbatim: goal + roadmap (steps 2→3→1→sales→staff/tips→qualification), people/roles (Ronan owner, Jesse partner, Jaka = us), reading order, code map, known bugs, in-flight work, environment inventory, where credentials live (names/places only), deploy + rollback as done, third-party accounts. Items marked V verified live that day, U unknown, OWNER TO FILL open.

Prompt 00 reads both before the code. Open items from the answers (owner to fill, plaintext key file risk, billing owners, FansAPI trial pricing ending ~13 Nov) go into `docs/RISKS.md` in prompt 00.
