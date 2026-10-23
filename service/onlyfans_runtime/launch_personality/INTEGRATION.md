# OnlyFans personality workflow — integration handoff

Branch: `codex/onlyfans-personality-20261023`.
Backend parent: `dbd09b06dace2c24829ded6bc778d7e6d2ded4f0`.
Dashboard reference: `927b2d75105f6180c37f2759ed119f89521b979d`.
Both repositories were checked for AGENTS.md at those pins; none exist. Backend
CLAUDE.md was read. The proposed personality directory and dedicated tests did
not exist at the pin, and no ownership register was present. This assignment
designates their ownership; shared files remain with Claude.

## Implemented behavior

`GET empty → import → draft → edit → draft → approve → saved AccountAiConfig`.
Each successful command appends an immutable revision with actor, binding,
command digest, prior revision, evidence document and configuration version.
Later edits create a new draft; the last saved configuration continues until
another explicit approval. Historical revisions remain reviewable to the same
current binding only. An idempotent retry returns its recorded revision even
when newer revisions exist; clients must refresh current state afterward.

The standard import reads canonical outgoing history already in this database.
It does not call OnlyFans, a model, or any credential endpoint. It first bounds
provenance to the latest 10,000 applied ingress records for the current binding,
then joins at most 5,000 outgoing messages by primary key. It caps
the serialized export budget, deduplicates repeated copy, and selects up to 40
examples across authors, fans and time. Coverage explicitly states what was
excluded. Legacy messages lacking current-binding provenance are excluded.

Profile facts come from existing effective saved creator facts. This is not a
claim that a fresh OnlyFans profile or its free-text bio was fetched. The
`import_draft()` Python seam also accepts a trusted, scoped export captured by
Claude; it is intentionally not exposed as an arbitrary browser upload API.

Facts, writing observations and operational sales instructions are separate.
Suggestions use measured reply length/case/emoji/question rates, with evidence
links and an authorship warning. They never include raw message quotes or
biography. All suggestions start unselected. Unknown authorship stays unknown;
employee-attributed messages are labelled agency, automation is excluded from
tone inference. The reviewer explicitly saves the complete tone and chosen
facts. Quarantined sales instructions never compile into the persona.

Conflicts cover disclosure suppression, payment/attention gates, fixed-price
overrides/free content, forced sales cadence, off-platform redirects, unsupported
custom/live-content promises and prompt overrides. This is a conservative
linter, not complete semantic interpretation; approval requires explicit
authorship/fact/runtime-rule acknowledgements. Creator personal limits are
retained as supported facts rather than silently discarded.

Changing creator ownership invalidates old evidence. Saved facts without
current-creator provenance are not relabelled as new profile evidence. Before
approval, every prior-creator fact key must be explicitly replaced or cleared.

## Exact changes Claude must integrate

1. **Migration and registration, owned by Claude.** Import
   `onlyfans_runtime.launch_personality.models` in `db/schema.py::_metadata()`.
   Both owned tables already carry `onlyfans_since=0089_offer_import` metadata.
   Fold `of_personality_head` and `of_personality_revision` into the unpublished
   0089 migration and frozen SQL snapshot using the definitions in `models.py`.
   Do not make a competing migration head. Their FK order is runtime/binding →
   head → revision. Retain the revision sequence/state checks and unique
   `(account_id,command_key)` constraint.
2. Install the two exact inert statements in
   `schema_contract.IMMUTABLE_EVIDENCE_SQL` in that migration. Add this tuple to
   `db/schema.py::check_schema()`'s append-only checks:
   `(89, "of_personality_revision", "of_personality_immutable", "personality evidence is append-only")`.
   The head is mutable; revision evidence must reject UPDATE and DELETE.
3. **API mounting.** In `service/onlyfans_app.py`, import the router from
   `onlyfans_runtime.launch_personality.api` and call
   `app.include_router(personality_router)` alongside `configuration_router`.
   Existing configuration-purpose token/tenant initialization creates the store;
   no new credential, role, scheduler or account-activation setting is needed.
   The existing agency namespace allowlist already permits these subroutes.
4. **Credential readiness projection.** Optionally set
   `app.state.personality_credential_status_reader` to a trusted synchronous
   function `(sqlalchemy_session, account_id, binding_id) → metadata | None`.
   Metadata must be exactly `{accountId,modelId,configured,expiresAt}`.
   It must validate the existing connection's binding, verified status, expiry
   and encrypted model selection using the existing connection authority; no
   provider request, key logging, client construction or environment fallback.
   Keep decryption inside that existing authority. `configured=true` is only
   appropriate for usable, matching account-bound credentials. With no reader,
   readiness remains unverified (`metadataAvailable=false`, credential blocker).
5. **Dashboard wiring.** Give `dashboard-contract.ts` and the API section below
   to the dashboard owner. Reuse current BFF identity/membership and configuration
   purpose transport. Claude alone mounts navigation or changes shared files.
   New frontend code must parse the versioned response rather than cast an
   unvalidated body. Reuse the current model catalogue/configuration GET.
6. Add the four dedicated personality suites to the combined integration checks.
   Rehearse the populated 0088→0089 migration with these tables/trigger and run
   the combined regressions after integrating all branches. These are shared
   integration tasks, not additional approval checkpoints.

**Prompt integration requires no shared prompt change.** Approval joins the
existing `configuration.update_configuration()` writer to its external database
transaction, retaining validation, shared-fact override merging, model selection,
fingerprints, `OfConfigurationChange` audit and configuration revision bump.
The actual existing path is:

`AccountAiConfig → native_context.versions → native_planning._native_messages →
compose_persona → ai_chatter._build_messages`.

The dedicated test reads the approved saved row through `versions()` and calls
that real native builder, proving the approved tone and typed facts are in the
assembled prompt. It does not mock the composer, call a paid model, prepare a
live send, or bypass egress/version invalidation. Approval and configuration
audit roll back together if subsequent personality evidence insertion fails.
An unchanged configuration can still receive an approval revision without
inventing a configuration change/audit row.

## API and dashboard contract

Prefix: `/onlyfans/configuration/v1/accounts/{accountId}/personality`.
Use server-to-server `Authorization: Bearer <existing configuration-purpose token>`
and `X-OnlyFans-Configuration-Version: of-configuration.v3`.
Responses retain `schemaVersion: of-configuration.v3` and add
`personalityVersion: of-personality.v1`. All responses are private/no-store.

| Method / suffix | Request / result |
| --- | --- |
| GET root | Current draft/approval; `state=empty, document=null` when none |
| GET `/history?before=N` | At most 20 revision summaries and `nextBefore` |
| GET `/revisions/N` | Exact same-binding historical revision |
| POST `/import` | `{expectedRevision,commandKey,actor}`; bounded canonical import |
| POST `/review` | Same command fields plus `review`; full replacement review |
| POST `/approve` | Same command fields plus `acknowledgements`; atomic config apply |

`expectedRevision` is the **personality** revision; never substitute the
configuration revision. GET root returns the current counter even after
rebinding without exposing the previous binding's document. Version, binding,
fingerprint, full review and actor identity are rechecked before approval.

`review` permits only `persona`, `creatorFacts`, `defaultModel`, and
`replyModelOverride`. Persona is explicit nonempty text ≤4,096 characters.
Facts use the existing nine-key contract; values are nonempty text ≤240 chars
or null to clear. Omit `creatorFacts` when no fact changes are wanted (an empty
object is invalid). A null model override removes `ai_chatter` from the existing
purpose map. Unrelated facts, purposes, sales settings and automation authority
are preserved. No credential, arbitrary JSON, sales switch or budget writes are
accepted by this workflow.

Approval acknowledgements are exactly
`{mixedAuthorship:true,runtimeRules:true,factsVerified:true}`.
The BFF derives `actor` from authenticated `ctx.actor` and derives `commandKey`
from a canonical digest of action/account/binding/revision/review/actor. Browser
bodies must not choose the actor or receive the configuration-purpose token.
Keep the existing origin/content-type/size/agency-write checks from the pinned
configuration route. Retrying a lost acknowledgement uses the same command.

Error statuses: 400 invalid request/configuration, 401 purpose credential, 403
agency middleware denial, 404 unavailable account/binding/revision, 409 stale
version or reused command, 413 body budget, 503 unconfigured purpose/store,
500 outcome unconfirmed. The existing 403 agency middleware envelope does not
include schemaVersion; other personality route responses do. A 500/network failure must not be reported as a failed
save: refresh or retry the identical command. Duplicate JSON keys/nonfinite
values/unknown fields are rejected. Body limit is 65,536 bytes.

Recommended review UI: facts with explicit choices and old/new comparisons;
style plus attribution/evidence; excluded operational examples and conflict
codes; editable tone preview; model and readiness; revision history. Evidence
is plain escaped text, never HTML. Empty/blocked/history/account-switch/read-only
states are distinct. Readiness is false until approval, matching current config,
fresh required shared facts, a known model, usable credentials and positive
budget. Readiness does not grant delivery permission or verify a live provider.

## Model, credential, budget and tone configuration

| Concern | Existing authority and requirement |
| --- | --- |
| Model | `AccountAiConfig.model` plus `model_by_purpose.ai_chatter`; explicit catalogue choice; no fallback |
| Credential | Existing encrypted connection `model={modelId,apiKey}`; unexpired, current binding, exact selected model |
| Budget | Existing `daily_cost_cap_cents`, integer 0..1,000,000; zero prevents paid calls; existing native reservation remains authoritative |
| Tone/facts | Approved `persona` and supported `persona_facts_json`/shared-fact overrides |
| Reasoning | Existing saved `reasoning_effort`; stale choices report the registry fallback without changing model |

Budget, reasoning, language and voice are read-only here because the existing
configuration editor excludes them. If the combined UI must edit them, Claude
must extend `configuration.validate_patch`, `update_configuration`, `editable`
and the corresponding dashboard parser with strict allowlists, audit evidence,
revision checks and native-budget regression tests. Do not put these fields in
personality prose or unrelated JSON. Credentials continue through the existing
connection flow; this branch never stores keys in review evidence.

## Validation and remaining integration work

Dedicated synthetic suites cover extraction/evidence, strict HTTP authority,
policy/readiness, durable editing/approval, idempotency, tenant/creator access,
rebind isolation, configuration fingerprints, rollback, real prompt assembly,
concurrent PostgreSQL edits/retries and the exact append-only trigger.

Run with the repository's pinned dependencies and an explicitly disposable
local PostgreSQL database named `personality_test_*`:

```bash
ONLYFANS_PERSONALITY_TEST_DATABASE_URL=postgresql+psycopg2://synthetic@127.0.0.1:55473/personality_test_workflow \
PYTHONPATH=service python -m pytest -q service/tests/test_onlyfans_launch_personality*.py
```

The fixture creates/drops only its synthetic tables in that explicit test DB;
never use a deployment database. Without this variable, unit/HTTP/SQLite checks
run but the PostgreSQL concurrency/trigger proofs skip. Final delivery reports
the full PostgreSQL run with no required skips. Tests create owned ORM tables
and the provided trigger contract; the combined production migration rehearsal
remains Claude's integration responsibility.

No shared runtime, model, migration, navigation, dependency or CI file was edited.
No merge, deployment, production migration, account activation, live provider
diagnostic, content upload or fan message occurred. No real personality import
was executed; production authorship/coverage remains to be reviewed after
Claude's coordinated source import. This implementation is ready for the single
combined checkpoint and integration, not a claim of deployed launch readiness.
