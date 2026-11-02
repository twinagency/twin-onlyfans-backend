# Recovered Twin adapter source

Recovered for Ronan on 3 November 2026. Source preservation and test dependency handoff only.

## Why GitHub search missed it

The requested files were found in Jesse's Windows checkout:

`C:\Users\conta\Documents\ChatGPT\TWIN CLONE CREATION\twin-sexbot`

The checkout records:
- Configured origin: `jesse98/twin-sexbot` (historical repository identity, not a verified current clone URL).
- Local branch: `codex/onlyfans-integration`.
- Base HEAD: `7b0036bf4d1a58413c57d19434fa4bcad3a1c4f4`.

`src/channels/identity.ts`, `src/channels/http.ts` and `src/channels/store.ts` exist in the working directory but are absent from its version-2 Git index. The base commit is therefore provenance, not a commit containing the recovered adapter. An October 16 audit snapshot also contains these paths.

## Package

[Download the source archive](Twin-Adapter-Recovery-20261103.zip).

SHA-256: `263901934b825aba89981658d63410fa5bb05b65a72132123f450293366a2805`

26 files, 99,017 archive bytes. Includes:
- Original `src/` tree, including all seven channel modules and their source dependencies.
- `package.json`, `package-lock.json`, `tsconfig.json`.
- `scripts/check-channel.ts`, `scripts/check-http.ts`, `scripts/check-recovery.ts`.
- Recovery provenance notes.

Source is copied as recovered, without implementation edits. The archive excludes `.env`, Git metadata, installed dependencies, runtime data, logs and credential files. Historical creator source modules are present because the adapter imports the creator registry at runtime. They are not approved Moxy creator sheets or replacement Moxy instructions.

## Use with the backend tests

The backend's two external-adapter test modules are:
- `service/tests/test_twin_bridge_contract.py`
- `service/tests/test_twin_bridge_roundtrip.py`

Their helper is `service/tests/helpers/twin_bridge_server.mjs`. It imports this recovered source through `TWIN_ROOT`; without that variable it expects a sibling folder called `twin-sexbot`. The separate `test_twin_bridge_client.py` uses Python mock transport and does not need this TypeScript checkout.

Retrieve this review branch into a separate checkout or download this folder. Extract the ZIP into a fresh test directory, install its pinned dependencies, and set `TWIN_ROOT` to that extracted directory, which must directly contain `src/` and `package.json`.

Example from the repository root, using a fresh sibling destination:

```sh
python -m zipfile -e handoffs/twin-adapter-recovery-20261103/Twin-Adapter-Recovery-20261103.zip ../twin-adapter-recovered
npm ci --prefix ../twin-adapter-recovered
```

From the backend candidate being qualified, with its normal Python test dependencies installed:

```sh
TWIN_ROOT=/absolute/path/to/twin-adapter-recovered python -m pytest -q service/tests/test_twin_bridge_contract.py service/tests/test_twin_bridge_roundtrip.py
```

Use Node 22 and an isolated test environment without production credentials or an existing `.env`. The Python helper supplies synthetic creator/model/transport behaviour and starts a temporary loopback adapter. Do not start the application's `npm start` or bridge service. The original default `npm test` references other historical scripts that are not part of this recovery package; use the targeted checks above.

## Scope and verification

Verified during recovery:
- Archive SHA-256 equals the locally prepared Windows archive.
- Required adapter files are present.
- Relative source imports resolve within the recovered source tree.
- No credential patterns were found by the limited scan; credential files and runtime records were excluded by selection.

Not verified during recovery:
- These two adapter tests have not been rerun.
- No claim that this historical source is production-ready or matches a deployed Twin service.
- No Moxy end-to-end or live provider qualification.

This review commit only adds the recovery package and documentation. It is based on the preserved Moxy handoff commit `db6d92086088ad80b3a43ab742d1a925e178f7c4`, solely to publish the dependency. Ronan's later `claude/moxy-completion-20261103` branch was independently confirmed at `66aa6bc1e96dbfb17d6040b3e7006a3216e2ed2f` during this handoff. Do not reset that work to this older parent, merge the entire recovery branch over it, or reinstate legacy sending to make tests pass. Retrieve the package and use it as an isolated test dependency.

## Maile onboarding clarification

No approved Maile creator sheet was located in the supplied materials and searches. Maile history is evidence, not approval. A history-derived sheet must remain a draft, cite its evidence, mark unknowns/conflicts, and receive Jesse's approval before activation. The Moxy launch creator sheets supplied for this project are Amelia and Cristina. The missing Maile sheet does not establish a missing sheet for either launch creator.

No merge, deployment, activation, customer message or live test was performed as part of this recovery.
